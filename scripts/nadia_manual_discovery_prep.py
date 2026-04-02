"""
nadia_manual_discovery_prep.py — Prepare the Claude Code prompt for a manual discovery run.

Reads context from environment variables, parses accession IDs from the issue body
(rendered from the YAML form template), and writes a prompt file for claude -p.

Called by manual_discovery.yml before invoking claude.
Exit 1 if no accessions can be parsed.
"""
import datetime
import json
import os
import re
import sys
import urllib.request


def github_request(method, path, payload=None):
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPOSITORY", "")
    url   = f"https://api.github.com/repos/{repo}/{path}"
    data  = json.dumps(payload).encode() if payload is not None else None
    req   = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "NADIA/1.0",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def parse_accessions(issue_body):
    """
    Extract accession IDs from the rendered YAML form body.
    The form section heading is '### Accession IDs / DOIs / PMIDs'.
    """
    # Find the accessions section (everything between the heading and the next heading or end)
    m = re.search(
        r'###\s+Accession IDs / DOIs / PMIDs\s*\n(.*?)(?=\n###|\Z)',
        issue_body, re.DOTALL | re.IGNORECASE
    )
    if not m:
        return []
    raw = m.group(1).strip()
    accessions = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and line != '_No response_':
            accessions.append(line)
    return accessions


def parse_notes(issue_body):
    """Extract optional notes from the rendered form body."""
    m = re.search(
        r'###\s+Notes.*?\n(.*?)(?=\n###|\Z)',
        issue_body, re.DOTALL | re.IGNORECASE
    )
    if not m:
        return ""
    notes = m.group(1).strip()
    return "" if notes == "_No response_" else notes


def main():
    issue_number  = os.environ.get("NADIA_ISSUE_NUMBER", "")
    issue_body    = os.environ.get("NADIA_ISSUE_BODY", "")
    workspace_dir = os.environ.get("NADIA_WORKSPACE_DIR", "/tmp/nf_agent")
    today         = datetime.date.today().isoformat()

    accessions = parse_accessions(issue_body)
    if not accessions:
        print("ERROR: No accessions found in issue body.", file=sys.stderr)
        print("Issue body (first 500 chars):", issue_body[:500], file=sys.stderr)
        # Post a comment explaining the problem
        try:
            github_request("POST", f"issues/{issue_number}/comments", {
                "body": (
                    "Could not parse any accession IDs from this issue. "
                    "Please check the format — one accession per line — and re-open."
                )
            })
        except Exception:
            pass
        sys.exit(1)

    notes = parse_notes(issue_body)
    accession_list = "\n".join(f"- {a}" for a in accessions)
    notes_section  = f"\n**Submitter notes:** {notes}\n" if notes else ""

    os.makedirs(workspace_dir, exist_ok=True)

    prompt = f"""\
# NADIA — Manual Discovery Run

You are NADIA, running in manual discovery mode triggered by GitHub issue #{issue_number}.

**Today's date:** {today}
**Mode:** Manual — processing user-specified accessions only (no date-range PubMed search)

## Accessions to Process

{accession_list}
{notes_section}
## Your Task

Follow the NADIA daily run workflow (`prompts/daily_task_template.md` for step details), with these modifications:

**Skip Steps 2 and 3** (date-range PubMed and secondary repository discovery). Instead:

Write and run `{workspace_dir}/seed_lookup.py`:
- For each accession in the list above, resolve full metadata from its source repository
- For each, look up the associated PMID/DOI if not already known (PubMed search by DOI or title)
- Group accessions that belong to the same paper into one publication group
- Write `{workspace_dir}/publication_groups.json`

Then continue from **Step 4** (dedup → score → create Synapse projects → self-audit → GitHub review issues → state update).

## Final Requirement — Post Summary to This Issue

After Step 9, post a comment on GitHub issue #{issue_number} summarising the run:
- Each accession processed, with outcome: created / added to existing / rejected (reason) / error
- Synapse project IDs and GitHub study-review issue links for created projects
- Any warnings

Use `scripts/github_issue.py`'s `post_issue_comment()` or POST directly to the GitHub API.
The workflow will close this issue automatically after your run completes.

Work in `{workspace_dir}`. Read CLAUDE.md and config/ before writing any code. Follow all safety rules.
"""

    prompt_path = os.path.join(workspace_dir, "nadia_manual_prompt.md")
    with open(prompt_path, "w") as f:
        f.write(prompt)

    print(f"Accessions ({len(accessions)}): {', '.join(accessions)}")
    print(f"Prompt written to {prompt_path}")


if __name__ == "__main__":
    main()
