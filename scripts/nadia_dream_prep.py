"""
nadia_dream_prep.py — Collect /nadia fix: commands from the past week and
write a self-improvement prompt for Claude Code.

Fetches all study-review issue comments containing '/nadia fix:' from the
last N days, aggregates them, and writes a structured prompt that Claude Code
will use to improve NADIA's annotation logic and instructions.

Called by nadia_dream.yml before invoking claude.
"""
import datetime
import json
import os
import re
import urllib.request
import urllib.parse


def github_request(method, path, payload=None, params=None):
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPOSITORY", "")
    url   = f"https://api.github.com/repos/{repo}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "NADIA-dream/1.0",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def get_paginated(path, params=None):
    """Fetch all pages of a GitHub list endpoint."""
    results = []
    page = 1
    while True:
        p = dict(params or {})
        p["per_page"] = 100
        p["page"] = page
        batch = github_request("GET", path, params=p)
        if not batch:
            break
        results.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return results


def collect_fix_commands(lookback_days=7):
    """
    Return a list of dicts, one per /nadia fix: comment found in the last
    lookback_days across all study-review issues.
    """
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=lookback_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # Get all study-review issues (open and closed)
    issues = get_paginated("issues", {"labels": "study-review", "state": "all"})
    print(f"  Found {len(issues)} study-review issues to scan")

    fix_commands = []
    for issue in issues:
        issue_number = issue["number"]
        issue_title  = issue["title"]

        comments = get_paginated(f"issues/{issue_number}/comments", {"since": since})
        for comment in comments:
            body = comment.get("body", "")
            if "/nadia fix:" in body.lower():
                # Extract the fix description
                m = re.search(r'/nadia fix:\s*(.+?)(?:\n|$)', body, re.IGNORECASE)
                if m:
                    fix_text = m.group(1).strip()
                    fix_commands.append({
                        "issue_number": issue_number,
                        "issue_title":  issue_title,
                        "fix_text":     fix_text,
                        "comment_url":  comment.get("html_url", ""),
                        "created_at":   comment.get("created_at", ""),
                    })

    return fix_commands


def summarise_fix_commands(fix_commands):
    """
    Group fix commands by rough theme using keyword matching.
    Returns a dict: {theme: [fix_text, ...]}
    """
    THEMES = {
        "disease focus / manifestation":  r"disease.?focus|manifestation|nf1|nf2|schwannomatos|mpnst",
        "study leads / authors":          r"study.?lead|author|pi |principal.?investigator|correspond",
        "assay type":                     r"\bassay\b|rna.?seq|scrna|single.?cell|wgs|wes|chip|atac|proteom",
        "species":                        r"\bspecies\b|mouse|human|murine|homo.?sapien|mus.?muscul",
        "tumor type / diagnosis":         r"tumor.?type|tumortype|diagnosis|cancer|glioma|neurofibroma",
        "file format":                    r"file.?format|fastq|bam|vcf|csv|tsv|gz\b",
        "specimen / individual ID":       r"specimeni?d|individuali?d|sample.?id",
        "annotations missing / wrong":   r"missing|wrong|incorrect|should be|needs? to be|update|fix",
        "schema / validation":            r"schema|valid|bind|template",
        "resource status":                r"resource.?status|pending|approved",
        "accession / repository":         r"accession|repository|geo|sra|ena|pride|zenodo",
    }

    groups = {theme: [] for theme in THEMES}
    groups["other"] = []

    for cmd in fix_commands:
        text  = cmd["fix_text"].lower()
        found = False
        for theme, pattern in THEMES.items():
            if re.search(pattern, text, re.IGNORECASE):
                groups[theme].append(cmd)
                found = True
                break
        if not found:
            groups["other"].append(cmd)

    # Remove empty themes
    return {k: v for k, v in groups.items() if v}


def main():
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "7"))
    workspace_dir = os.environ.get("NADIA_WORKSPACE_DIR", "/tmp/nf_agent")
    today         = datetime.date.today().isoformat()
    since_date    = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()

    print(f"Collecting /nadia fix: commands since {since_date}...")
    fix_commands = collect_fix_commands(lookback_days)
    print(f"  Total fix commands found: {len(fix_commands)}")

    if not fix_commands:
        print("No fix commands found this week — writing minimal prompt.")
        summary_text = "No /nadia fix: commands were issued in the past week."
        detailed_text = ""
    else:
        groups = summarise_fix_commands(fix_commands)

        # Build summary table
        summary_lines = ["| Theme | Count | Example fixes |", "|-------|-------|---------------|"]
        for theme, cmds in sorted(groups.items(), key=lambda x: -len(x[1])):
            examples = "; ".join(c["fix_text"][:80] for c in cmds[:3])
            summary_lines.append(f"| {theme} | {len(cmds)} | {examples} |")
        summary_text = "\n".join(summary_lines)

        # Build detailed log
        detail_lines = []
        for cmd in fix_commands:
            detail_lines.append(
                f"- Issue #{cmd['issue_number']} ({cmd['issue_title'][:80]}): "
                f"`{cmd['fix_text']}` [{cmd['created_at'][:10]}]({cmd['comment_url']})"
            )
        detailed_text = "\n".join(detail_lines)

    os.makedirs(workspace_dir, exist_ok=True)

    # Save raw data for reference
    with open(os.path.join(workspace_dir, "dream_fix_commands.json"), "w") as f:
        json.dump(fix_commands, f, indent=2)

    branch_name = f"nadia-dream-{today}"

    prompt = f"""\
# NADIA Dream — Weekly Self-Improvement Run

**Date:** {today}
**Review window:** {since_date} to {today} ({lookback_days} days)
**Total /nadia fix: commands found:** {len(fix_commands)}

---

## Fix Command Summary

{summary_text}

## Full Fix Log

{detailed_text if detailed_text else '_No fix commands this week._'}

---

## Your Task

You are NADIA, reviewing your own recent annotation errors to improve future runs.

### Step 1 — Analyse the patterns

Read the fix command summary above. For each theme with ≥ 1 fix:
- What is the likely root cause in the current instructions or code?
- Is it a missing or ambiguous instruction in CLAUDE.md?
- Is it a schema enum value that's hard to infer?
- Is it a field where the data source is unreliable (e.g. wrong studyLeads from ENA submitter)?
- Is it something the self-audit step (Step 7) could automatically catch and fix?

Think through root causes before touching any files.

### Step 2 — Make targeted improvements

Read the current state of these files before editing:
- `CLAUDE.md` — core agent instructions
- `prompts/daily_task_template.md` — step-by-step task
- `prompts/synapse_workflow.md` — Dataset creation, audit, wiki template
- `prompts/repo_apis.md` — repository API patterns
- `config/settings.yaml` — annotation vocabulary

Make surgical, focused changes that directly address the patterns you identified.
Good targets include:
- Adding clearer rules or examples to CLAUDE.md annotation sections
- Strengthening the self-audit checklist (Step 7) to auto-detect newly seen error patterns
- Adding explicit "gotcha" warnings for fields that are frequently wrong
- Updating the audit script template to auto-fix additional fields
- Improving the wiki or annotation templates

**Do not** make speculative refactors or changes unrelated to the observed errors.
**Do not** change `config/settings.yaml` annotation vocabulary unless a specific enum was wrong.

### Step 3 — Create a pull request

After making changes:
1. Create a new git branch: `{branch_name}`
2. Stage and commit all modified files with a descriptive message
3. Push the branch and open a PR to `main` with:
   - Title: `[NADIA Dream] Weekly self-improvement {today}`
   - Body: summary of what patterns were found, what was changed, and why

Use the `git` and `gh` CLI tools for this. The `GITHUB_TOKEN` is available.

If no fix commands were found this week, or all patterns are already well-addressed by existing
instructions, create a PR with a brief note explaining that no changes were needed.

Work in the repository root (`$AGENT_REPO_ROOT`). Commit only files you intentionally changed.
"""

    prompt_path = os.path.join(workspace_dir, "nadia_dream_prompt.md")
    with open(prompt_path, "w") as f:
        f.write(prompt)

    print(f"Prompt written to {prompt_path}")
    print(f"Branch will be: {branch_name}")


if __name__ == "__main__":
    main()
