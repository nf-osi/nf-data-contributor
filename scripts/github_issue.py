"""
github_issue.py — Create a GitHub issue for NADIA study review.

Called by the agent after a Synapse project is successfully created or a dataset
is added to an existing project.  Replaces the prior Jira notification pattern.

Usage (from agent-generated scripts):
    python scripts/github_issue.py \
        --synapse-project-id syn12345678 \
        --study-name "My NF1 Mouse Study" \
        --accessions "geo:GSE123456" "insdc.sra:SRP654321" \
        --study-leads "Smith J" "Doe A" \
        --assay-types "RNA-seq" \
        --file-count 42 \
        --outcome new   # or "added"
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error


def _default_team_mention():
    """Read team_mention from config/settings.yaml; fall back to empty string."""
    try:
        import yaml
        repo_root = os.environ.get("AGENT_REPO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cfg_path  = os.path.join(repo_root, "config", "settings.yaml")
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("notifications", {}).get("github", {}).get("team_mention", "")
    except Exception:
        return ""


def _github_request(method, path, payload=None):
    """Make a GitHub REST API v3 request."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPOSITORY", "")   # owner/repo
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set")
    if not repo:
        raise RuntimeError("GITHUB_REPOSITORY not set")

    url  = f"https://api.github.com/repos/{repo}/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "NADIA/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"GitHub API {method} {url} → {e.code}: {body[:300]}")


def _get_team_members(org, team_slug):
    """Return list of GitHub login strings for a team (best-effort)."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return []
    url  = f"https://api.github.com/orgs/{org}/teams/{team_slug}/members"
    req  = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "NADIA/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            members = json.loads(resp.read().decode())
            return [m["login"] for m in members]
    except Exception:
        return []


def _ensure_labels(labels):
    """Create labels in the repo if they don't already exist."""
    existing = {l["name"] for l in _github_request("GET", "labels?per_page=100")}
    defaults = {
        "study-review":  {"color": "0075ca", "description": "NADIA study awaiting data manager review"},
        "approved":      {"color": "0e8a16", "description": "Study approved — triggers portal provisioning"},
        "needs-changes": {"color": "e4e669", "description": "Changes requested before approval"},
        "dataset-added": {"color": "d93f0b", "description": "Dataset added to existing approved study"},
        "automated":     {"color": "cccccc", "description": "Created by automated workflow"},
    }
    for name in labels:
        if name not in existing and name in defaults:
            try:
                _github_request("POST", "labels", {
                    "name": name,
                    "color": defaults[name]["color"],
                    "description": defaults[name]["description"],
                })
            except Exception:
                pass  # label may already exist; non-fatal


def build_issue_body(
    synapse_project_id,
    study_name,
    accessions,
    study_leads,
    assay_types,
    file_count,
    outcome,
    disease_focus=None,
    manifestation=None,
    pmid=None,
    doi=None,
):
    synapse_url = f"https://www.synapse.org/Synapse:{synapse_project_id}"
    acc_str     = ", ".join(accessions) if accessions else "—"
    leads_str   = ", ".join(study_leads) if study_leads else "—"
    assay_str   = ", ".join(assay_types) if assay_types else "Unknown"
    df_str      = ", ".join(disease_focus) if disease_focus else "—"
    mfst_str    = ", ".join(manifestation) if manifestation else "—"
    outcome_label = "🆕 New project created" if outcome == "new" else "➕ Dataset added to existing project"

    # Embed machine-readable metadata for the provisioning script to parse
    metadata = {
        "synapse_project_id": synapse_project_id,
        "accessions": accessions,
        "study_name": study_name,
        "study_leads": study_leads,
        "assay_types": assay_types,
        "file_count": file_count,
        "outcome": outcome,
        "disease_focus": disease_focus or [],
        "manifestation": manifestation or [],
        "pmid": pmid or "",
        "doi": doi or "",
    }

    metadata_json = json.dumps(metadata, indent=2, ensure_ascii=False)

    parts = [
        "## NADIA Study Review\n",
        f"{outcome_label}\n",
        "| Field | Value |",
        "|-------|-------|",
        f"| **Synapse Project** | [{synapse_project_id}]({synapse_url}) |",
        f"| **Study Name** | {study_name} |",
        f"| **External Accessions** | {acc_str} |",
        f"| **Study Leads** | {leads_str} |",
        f"| **Assay Types** | {assay_str} |",
        f"| **Disease Focus** | {df_str} |",
        f"| **Manifestation** | {mfst_str} |",
        f"| **File Count** | {file_count:,} |",
        f"| **PMID** | {pmid or '—'} |",
        f"| **DOI** | {doi or '—'} |",
        "\n---\n",
        "## Review Checklist\n",
        "Please check each item before approving:\n",
        "- [ ] Study name is accurate and meaningful",
        "- [ ] Correct disease focus and manifestation values",
        "- [ ] Study leads (first + last/corresponding author) are correct",
        "- [ ] Assay type(s) are correct",
        "- [ ] File annotations look reasonable (spot-check a few files)",
        "- [ ] Wiki summary is informative",
        "- [ ] No sensitive or controlled-access data exposed unintentionally",
        "\n---\n",
        "## Actions\n",
        "**To request annotation fixes**, comment with:",
        "```",
        "/nadia fix: <describe what needs to be changed>",
        "```",
        "Examples:",
        "- `/nadia fix: disease focus should be NF2 not NF1`",
        "- `/nadia fix: manifestation is missing — this is a plexiform neurofibroma study`",
        "- `/nadia fix: study lead should be Jane Doe, not John Smith`\n",
        "**To recheck the project status**, comment with:",
        "```",
        "/nadia status",
        "```\n",
        "**To approve and trigger portal provisioning**, apply the `approved` label to this issue.",
        "This will automatically:",
        "1. Set `resourceStatus = approved` on the Synapse project and all files",
        "2. Add the project to the portal file view",
        "3. Update the NADIA state table",
        "4. Post a summary comment here\n",
        "> ⚠️  Only apply `approved` when the study is ready for the public portal.",
        "> Once provisioned, changes require manual curator action in Synapse.",
        "\n---\n",
        "<details>",
        "<summary>🔧 NADIA Metadata (used by automated provisioning — do not edit)</summary>\n",
        "```json",
        "NADIA_METADATA_JSON",
        metadata_json,
        "NADIA_METADATA_JSON",
        "```\n",
        "</details>",
    ]
    body = "\n".join(parts)
    return body


def create_study_review_issue(
    study_name,
    synapse_project_id,
    accessions,
    study_leads,
    assay_types,
    file_count,
    outcome="new",
    disease_focus=None,
    manifestation=None,
    pmid=None,
    doi=None,
    team_mention=None,  # kept for backwards compatibility, no longer used
):
    """Create (or update) a GitHub issue for study review. Returns issue URL."""
    issue_labels = ["study-review", "automated"]
    if outcome == "added":
        issue_labels.append("dataset-added")

    _ensure_labels(issue_labels)

    title = f"[NADIA Review] {study_name[:180]}"
    body  = build_issue_body(
        synapse_project_id=synapse_project_id,
        study_name=study_name,
        accessions=accessions,
        study_leads=study_leads,
        assay_types=assay_types,
        file_count=file_count,
        outcome=outcome,
        disease_focus=disease_focus,
        manifestation=manifestation,
        pmid=pmid,
        doi=doi,
    )

    issue = _github_request("POST", "issues", {
        "title": title,
        "body": body,
        "labels": issue_labels,
    })

    issue_number = issue["number"]
    issue_url    = issue["html_url"]
    print(f"  GitHub issue created: {issue_url}")

    return issue_number, issue_url


def post_issue_comment(issue_number, comment_body):
    """Post a comment on an existing issue."""
    result = _github_request("POST", f"issues/{issue_number}/comments", {"body": comment_body})
    return result.get("html_url", "")


def main():
    parser = argparse.ArgumentParser(description="Create a NADIA study review GitHub issue")
    parser.add_argument("--synapse-project-id",  required=True)
    parser.add_argument("--study-name",          required=True)
    parser.add_argument("--accessions",          nargs="+", default=[])
    parser.add_argument("--study-leads",         nargs="+", default=[])
    parser.add_argument("--assay-types",         nargs="+", default=[])
    parser.add_argument("--file-count",          type=int, default=0)
    parser.add_argument("--outcome",             default="new", choices=["new", "added"])
    parser.add_argument("--disease-focus",       nargs="+", default=[])
    parser.add_argument("--manifestation",       nargs="+", default=[])
    parser.add_argument("--pmid",                default="")
    parser.add_argument("--doi",                 default="")
    parser.add_argument("--team-mention",        default=_default_team_mention())
    args = parser.parse_args()

    issue_number, issue_url = create_study_review_issue(
        study_name=args.study_name,
        synapse_project_id=args.synapse_project_id,
        accessions=args.accessions,
        study_leads=args.study_leads,
        assay_types=args.assay_types,
        file_count=args.file_count,
        outcome=args.outcome,
        disease_focus=args.disease_focus,
        manifestation=args.manifestation,
        pmid=args.pmid,
        doi=args.doi,
        team_mention=args.team_mention,
    )
    # Output for capture by calling scripts
    print(json.dumps({"issue_number": issue_number, "issue_url": issue_url}))


if __name__ == "__main__":
    main()
