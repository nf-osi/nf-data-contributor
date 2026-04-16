"""
nadia_dream_prep.py — Collect learning signal from the past week and write
a self-improvement prompt for Claude Code.

Signal sources:
  1. /nadia fix: comments — human corrections (negative signal)
  2. /nadia status comments — annotation quality snapshots
  3. Approved studies (issues closed with 'approved' label) — positive signal
  4. Curation comments (annotation choices documented by NADIA post-curation)

Aggregates all signals and writes a structured prompt that Claude Code will
use to improve NADIA's annotation logic, instructions, and skill files.

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


def collect_fix_commands(issues, since):
    """
    Return a list of dicts, one per /nadia fix: comment found since `since`
    across all study-review issues.
    """
    fix_commands = []
    for issue in issues:
        issue_number = issue["number"]
        issue_title  = issue["title"]
        comments = get_paginated(f"issues/{issue_number}/comments", {"since": since})
        for comment in comments:
            body = comment.get("body", "")
            if "/nadia fix:" in body.lower():
                m = re.search(r'/nadia fix:\s*(.+?)(?:\n|$)', body, re.IGNORECASE)
                if m:
                    fix_commands.append({
                        "type":          "fix",
                        "issue_number":  issue_number,
                        "issue_title":   issue_title,
                        "text":          m.group(1).strip(),
                        "comment_url":   comment.get("html_url", ""),
                        "created_at":    comment.get("created_at", ""),
                    })
    return fix_commands


def collect_status_reports(issues, since):
    """
    Return a list of dicts for /nadia status responses (annotation quality snapshots).
    These capture which fields were problematic without an explicit fix command.
    """
    status_reports = []
    for issue in issues:
        issue_number = issue["number"]
        issue_title  = issue["title"]
        comments = get_paginated(f"issues/{issue_number}/comments", {"since": since})
        for comment in comments:
            body = comment.get("body", "")
            # NADIA status reports start with a recognizable header
            if "## NADIA Annotation Status" in body or "/nadia status" in body.lower():
                # Extract the fields-needing-review section if present
                m = re.search(
                    r'(?:Fields requiring review|Missing or approximated)[:\s]*\n((?:[-*].*\n?)+)',
                    body, re.IGNORECASE
                )
                flagged = m.group(1).strip() if m else ""
                status_reports.append({
                    "type":         "status",
                    "issue_number": issue_number,
                    "issue_title":  issue_title,
                    "flagged":      flagged,
                    "comment_url":  comment.get("html_url", ""),
                    "created_at":   comment.get("created_at", ""),
                })
    return status_reports


def collect_approved_studies(lookback_days):
    """
    Return a list of issues that were closed AND have the 'approved' label
    within the lookback window. These represent studies that passed review
    with no major corrections — positive signal for what NADIA did right.
    """
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    closed_issues = get_paginated(
        "issues",
        {"labels": "study-review,approved", "state": "closed", "since": since}
    )

    approved = []
    for issue in closed_issues:
        closed_at = issue.get("closed_at", "")
        if closed_at and closed_at >= since:
            # Grab the last NADIA curation comment to see what was annotated
            comments = get_paginated(f"issues/{issue['number']}/comments")
            curation_comment = ""
            for c in reversed(comments):
                body = c.get("body", "")
                if "## Curation Summary" in body or "annotation" in body.lower():
                    curation_comment = body[:1500]  # first 1500 chars
                    break
            approved.append({
                "type":              "approved",
                "issue_number":      issue["number"],
                "issue_title":       issue["title"],
                "closed_at":         closed_at,
                "curation_summary":  curation_comment,
            })

    return approved


def collect_curation_comments(issues, since):
    """
    Return NADIA's own post-curation comments (the annotation handoff notes).
    These capture what approximations were made and which fields had gaps,
    which is useful signal for identifying recurring vocabulary gaps.
    """
    curation_notes = []
    for issue in issues:
        issue_number = issue["number"]
        issue_title  = issue["title"]
        comments = get_paginated(f"issues/{issue_number}/comments", {"since": since})
        for comment in comments:
            body = comment.get("body", "")
            if "## Curation Summary" in body or "## Annotation Choices" in body:
                # Extract vocabulary gap section if present
                m = re.search(
                    r'(?:Vocabulary gaps?|Controlled vocabulary gaps?)[:\s]*\n((?:[-*].*\n?)+)',
                    body, re.IGNORECASE
                )
                vocab_gaps = m.group(1).strip() if m else ""
                curation_notes.append({
                    "type":         "curation",
                    "issue_number": issue_number,
                    "issue_title":  issue_title,
                    "vocab_gaps":   vocab_gaps,
                    "comment_url":  comment.get("html_url", ""),
                    "created_at":   comment.get("created_at", ""),
                })
    return curation_notes


def main():
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "7"))
    workspace_dir = os.environ.get("NADIA_WORKSPACE_DIR", "/tmp/nf_agent")
    today         = datetime.date.today().isoformat()
    since_date    = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()
    since_iso     = (datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Collecting learning signal since {since_date}...")

    # Fetch all study-review issues once — reuse for multiple signal types
    issues = get_paginated("issues", {"labels": "study-review", "state": "all"})
    print(f"  Found {len(issues)} study-review issues to scan")

    # Collect all signal types
    fix_commands    = collect_fix_commands(issues, since_iso)
    status_reports  = collect_status_reports(issues, since_iso)
    approved        = collect_approved_studies(lookback_days)
    curation_notes  = collect_curation_comments(issues, since_iso)

    print(f"  /nadia fix: commands:  {len(fix_commands)}")
    print(f"  Status reports:        {len(status_reports)}")
    print(f"  Approved studies:      {len(approved)}")
    print(f"  Curation comments:     {len(curation_notes)}")

    # Build signal sections for the prompt
    def fmt_fix(c):
        return (f"- Issue #{c['issue_number']} ({c['issue_title'][:80]}): "
                f"`{c['text']}` ({c['created_at'][:10]})")

    def fmt_status(c):
        flagged = f"\n  Flagged fields: {c['flagged']}" if c['flagged'] else ""
        return f"- Issue #{c['issue_number']} ({c['issue_title'][:80]}){flagged}"

    def fmt_approved(c):
        summary = f"\n  Curation note: {c['curation_summary'][:200]}..." if c['curation_summary'] else ""
        return f"- Issue #{c['issue_number']} ({c['issue_title'][:80]}), closed {c['closed_at'][:10]}{summary}"

    def fmt_vocab_gaps(c):
        gaps = f"\n  Gaps: {c['vocab_gaps']}" if c['vocab_gaps'] else ""
        return f"- Issue #{c['issue_number']} ({c['issue_title'][:80]}){gaps}"

    fix_log    = "\n".join(fmt_fix(c)      for c in fix_commands)    or "_None this week._"
    status_log = "\n".join(fmt_status(c)   for c in status_reports)  or "_None this week._"
    approved_log = "\n".join(fmt_approved(c) for c in approved)      or "_None this week._"
    vocab_log  = "\n".join(fmt_vocab_gaps(c) for c in curation_notes if c['vocab_gaps']) or "_None noted._"

    os.makedirs(workspace_dir, exist_ok=True)

    # Save raw data for reference
    with open(os.path.join(workspace_dir, "dream_signals.json"), "w") as f:
        json.dump({
            "fix_commands": fix_commands,
            "status_reports": status_reports,
            "approved": approved,
            "curation_notes": curation_notes,
        }, f, indent=2)

    branch_name = f"nadia-dream-{today}"

    prompt = f"""\
# NADIA Dream — Weekly Self-Improvement Run

**Date:** {today}
**Review window:** {since_date} to {today} ({lookback_days} days)

**Signal summary:**
- Human corrections (`/nadia fix:`): {len(fix_commands)}
- Annotation status reports: {len(status_reports)}
- Studies approved with no corrections (positive signal): {len(approved)}
- Curation comments with vocabulary gaps: {len([c for c in curation_notes if c['vocab_gaps']])}

---

## Signal 1 — Human Corrections (`/nadia fix:` commands)

{fix_log}

---

## Signal 2 — Annotation Status Reports

{status_log}

---

## Signal 3 — Approved Studies (Positive Signal)

These studies passed data manager review without requesting corrections.
Use these to identify what annotation patterns are working well — confirm
or reinforce existing skill file entries where applicable.

{approved_log}

---

## Signal 4 — Vocabulary Gaps from Curation Comments

{vocab_log}

---

## Your Task

You are NADIA, reviewing your own recent learning signal to improve future runs.

### Step 1 — Identify patterns from ALL signals

Analyse both failures (fix commands, flagged fields) and successes (approved
studies). From failures, identify:
- What is the likely root cause in the current instructions or code?
- Is it a missing or ambiguous instruction in CLAUDE.md or prompts/?
- Is it a schema enum value that's hard to infer reliably?
- Is it a field where the data source is unreliable?
- Is it something the self-audit step (Step 7) could automatically catch?

From successes, identify:
- Which annotation patterns produced no corrections?
- Are there patterns worth adding to the skill file as confirmed rules?
- Are any existing skill file entries outdated or wrong given the approvals?

Think through root causes before touching any files.

### Step 2 — Update `.nadia/skills/annotation_patterns.yaml`

Read the current skill file at `.nadia/skills/annotation_patterns.yaml`.

For each pattern you identified:
- If it's new and clearly supported by multiple signals: add a new entry
- If it confirms an existing entry: update `source: confirmed` and add the issue numbers
- If it contradicts an existing entry: update or remove the entry
- If it's a one-off / ambiguous: do not add it (wait for corroboration)

Each entry must have:
  - `pattern:` — short name (unique, kebab-case)
  - `rule:` — the concrete, actionable rule written as an instruction to NADIA
  - `source:` — "fix" | "confirmed" | "fix+confirmed"
  - `issues:` — list of GitHub issue numbers
  - `added:` or `updated:` — ISO date

Keep the file concise. Prefer updating existing entries over adding new ones.
Do not add speculative rules — only patterns with clear supporting evidence.

### Step 3 — Make targeted improvements to instructions

Read the current state of these files before editing:
- `CLAUDE.md` — core agent instructions
- `prompts/daily_task_template.md` — step-by-step task
- `prompts/synapse_workflow.md` — Dataset creation, audit, wiki template
- `prompts/annotation_gap_fill.md` — 4-tier gap fill algorithm
- `prompts/repo_apis.md` — repository API patterns

Make surgical, focused changes that directly address the patterns you identified.
Good targets include:
- Adding clearer rules or examples to CLAUDE.md annotation sections
- Strengthening the self-audit checklist (Step 7) to auto-detect newly seen patterns
- Adding explicit "gotcha" warnings for fields that are frequently wrong
- Improving the annotation gap-fill tiers for sources that are consistently useful

**Do not** make speculative refactors or changes unrelated to observed errors.
**Do not** change `config/settings.yaml` annotation vocabulary — vocabulary
changes require a separate DCC review process.

### Step 4 — Create a pull request

After making changes:
1. Create a new git branch: `{branch_name}`
2. Stage and commit all modified files with a descriptive message
3. Push the branch and open a PR to `main` with:
   - Title: `[NADIA Dream] Weekly self-improvement {today}`
   - Body: structured summary covering:
     - What patterns were found (failures and successes)
     - What was added/updated in the skill file
     - What was changed in instructions and why
     - Any vocabulary gaps that need DCC attention (do NOT change config for these —
       flag them in the PR body for human review)

Use the `git` and `gh` CLI tools for this. The `GITHUB_TOKEN` is available.

If no actionable signal was found this week, create a PR with a brief note
explaining that no changes were needed and why.

Work in the repository root (`$AGENT_REPO_ROOT`). Commit only files you
intentionally changed — do not stage unrelated files.
"""

    prompt_path = os.path.join(workspace_dir, "nadia_dream_prompt.md")
    with open(prompt_path, "w") as f:
        f.write(prompt)

    print(f"Prompt written to {prompt_path}")
    print(f"Branch will be: {branch_name}")


if __name__ == "__main__":
    main()
