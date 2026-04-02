"""
nadia_fix_prep.py — Prepare the Claude Code prompt for a /nadia fix: command.

Reads all context from environment variables (safe from shell injection),
extracts the Synapse project ID from the issue body, and writes a prompt
file for claude -p to consume.

Called by nadia_review.yml before invoking claude.
Exit 1 if the Synapse project ID cannot be found.
"""
import json
import os
import re
import sys


def main():
    issue_number    = os.environ.get("NADIA_ISSUE_NUMBER", "")
    fix_description = os.environ.get("NADIA_FIX_DESCRIPTION", "")
    issue_body      = os.environ.get("NADIA_ISSUE_BODY", "")
    workspace_dir   = os.environ.get("NADIA_WORKSPACE_DIR", "/tmp/nf_agent")

    # Extract Synapse project ID from the embedded metadata block
    m = re.search(r'NADIA_METADATA_JSON\s*(.*?)\s*NADIA_METADATA_JSON', issue_body, re.DOTALL)
    synapse_id = ""
    if m:
        try:
            meta = json.loads(m.group(1).strip())
            synapse_id = meta.get("synapse_project_id", "")
        except Exception:
            pass
    if not synapse_id:
        # Fallback: bare regex
        fm = re.search(r'"synapse_project_id":\s*"(syn\d+)"', issue_body)
        if fm:
            synapse_id = fm.group(1)

    if not synapse_id:
        print("ERROR: Could not extract Synapse project ID from issue body.", file=sys.stderr)
        print("Issue body (first 500 chars):", issue_body[:500], file=sys.stderr)
        sys.exit(1)

    os.makedirs(workspace_dir, exist_ok=True)

    prompt = (
        f"A data manager has requested a fix to a NADIA Synapse project "
        f"via GitHub issue #{issue_number}.\n\n"
        f"Synapse project: {synapse_id}\n"
        f"Fix requested: {fix_description}\n\n"
        "Steps:\n"
        "1. Read CLAUDE.md for safety rules and context.\n"
        "2. Check the current state of the project annotations in Synapse "
        "(read project annotations, sample a few files, check dataset entities).\n"
        "3. Apply the requested fix (update annotations, rename entities, etc.). "
        "Follow all NADIA safety rules.\n"
        "4. Verify the fix was applied correctly.\n"
        f"5. Post a GitHub comment on issue #{issue_number} summarising what was "
        "changed. Use scripts/github_issue.py post_issue_comment(), or POST "
        "directly to the GitHub API.\n\n"
        f"Work in {workspace_dir}. Do not modify CLAUDE.md, lib/, config/, or prompts/.\n"
    )

    prompt_path = os.path.join(workspace_dir, "nadia_fix_prompt.md")
    with open(prompt_path, "w") as f:
        f.write(prompt)

    print(f"Synapse project: {synapse_id}")
    print(f"Fix: {fix_description[:120]}")
    print(f"Prompt written to {prompt_path}")


if __name__ == "__main__":
    main()
