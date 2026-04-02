"""
backfill_study_review_issues.py — Post GitHub study-review issues for all
NADIA-created projects that do not yet have one.

Queries the NF_DataContributor_ProcessedStudies state table for all projects
with status = synapse_created or dataset_added, checks whether a GitHub issue
already references each project, and creates missing issues.

Usage:
    SYNAPSE_AUTH_TOKEN=... GITHUB_TOKEN=... GITHUB_REPOSITORY=nf-osi/nadia \
    STATE_PROJECT_ID=syn... AGENT_REPO_ROOT=. \
    python scripts/backfill_study_review_issues.py [--dry-run]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.environ.get("AGENT_REPO_ROOT", "."), "lib"))
from synapse_login import get_synapse_client

import yaml


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _github_request(method, path, payload=None, params=None):
    token = os.environ["GITHUB_TOKEN"]
    repo  = os.environ["GITHUB_REPOSITORY"]
    url   = f"https://api.github.com/repos/{repo}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data  = json.dumps(payload).encode() if payload is not None else None
    req   = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "NADIA-backfill/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"GitHub API {method} {path} → {e.code}: {body[:300]}")


def get_existing_review_issues():
    """Return set of Synapse project IDs already referenced in study-review issues."""
    existing = set()
    page = 1
    while True:
        issues = _github_request("GET", "issues", params={
            "labels": "study-review,automated",
            "state": "all",
            "per_page": 100,
            "page": page,
        })
        if not issues:
            break
        for issue in issues:
            body = issue.get("body") or ""
            # Extract project ID from NADIA_METADATA_JSON block
            import re
            m = re.search(r'NADIA_METADATA_JSON\s*(.*?)\s*NADIA_METADATA_JSON', body, re.DOTALL)
            if m:
                try:
                    meta = json.loads(m.group(1).strip())
                    pid = meta.get("synapse_project_id", "")
                    if pid:
                        existing.add(pid)
                except Exception:
                    pass
            # Also scan for bare project IDs in case of older issues
            for match in re.findall(r'\bsyn\d{8,}\b', body):
                existing.add(match)
        if len(issues) < 100:
            break
        page += 1
        time.sleep(0.5)
    return existing


# ---------------------------------------------------------------------------
# Synapse helpers
# ---------------------------------------------------------------------------

def get_state_table_id(syn, state_project_id, prefix="NF_DataContributor"):
    """Find the ProcessedStudies table ID in the state project."""
    target_name = f"{prefix}_ProcessedStudies"
    results = syn.getChildren(state_project_id, includeTypes=["table"])
    for item in results:
        if item["name"] == target_name:
            return item["id"]
    raise RuntimeError(f"State table '{target_name}' not found in {state_project_id}")


def get_all_created_projects(syn, table_id):
    """Query ProcessedStudies for all synapse_created / dataset_added projects."""
    query = (
        f"SELECT synapse_project_id, accession_id, source_repo, disease_focus "
        f"FROM {table_id} "
        f"WHERE status IN ('synapse_created', 'dataset_added', 'approved') "
        f"ORDER BY synapse_project_id"
    )
    df = syn.tableQuery(query).asDataFrame()
    # Group by project, collecting accessions
    projects = {}
    for _, row in df.iterrows():
        pid = row["synapse_project_id"]
        if pid not in projects:
            projects[pid] = {"accessions": [], "disease_focus": row.get("disease_focus", "")}
        acc = row.get("accession_id", "")
        repo = row.get("source_repo", "")
        if acc and repo:
            projects[pid]["accessions"].append((repo, acc))
    return projects


def get_project_annotations(syn, project_id):
    """Fetch project-level annotations."""
    try:
        anns = syn.get_annotations(project_id)
        return dict(anns)
    except Exception as e:
        print(f"  Warning: could not fetch annotations for {project_id}: {e}")
        return {}


def get_dataset_file_count(syn, project_id):
    """Count File entities under a project (best-effort)."""
    try:
        results = list(syn.getChildren(project_id, includeTypes=["file"], recursive=True))
        return len(results)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Issue creation
# ---------------------------------------------------------------------------

REPO_TO_PREFIX = {
    "GEO": "geo", "SRA": "insdc.sra", "ENA": "insdc.sra",
    "BioProject": "bioproject", "dbGaP": "dbgap",
    "EGA": "ega.study", "ArrayExpress": "arrayexpress",
    "PRIDE": "pride.project", "MassIVE": "massive",
    "MetaboLights": "metabolights", "CELLxGENE": "cellxgene.collection",
    "Zenodo": "zenodo.record", "OSF": "osf", "PDC": "pdc.study",
    "cBioPortal": "cbioportal", "Dryad": "dryad",
    "Science Data Bank": "scidb", "TIB": "tib",
    "Cell Image Library": "cil", "NCI GDC": "gdc",
}


def create_issue_for_project(syn, project_id, state_accessions, dry_run=False):
    """Fetch project annotations and create a study-review issue."""
    anns = get_project_annotations(syn, project_id)

    study_name  = anns.get("studyName", [project_id])[0] if isinstance(anns.get("studyName"), list) else anns.get("studyName", project_id)
    study_leads = anns.get("studyLeads", [])
    if isinstance(study_leads, str):
        study_leads = [study_leads]
    disease_focus = anns.get("diseaseFocus", [])
    if isinstance(disease_focus, str):
        disease_focus = [disease_focus]
    manifestation = anns.get("manifestation", [])
    if isinstance(manifestation, str):
        manifestation = [manifestation]
    assay_types = anns.get("dataType", [])
    if isinstance(assay_types, str):
        assay_types = [assay_types]
    pmid = anns.get("pmid", [None])
    pmid = pmid[0] if isinstance(pmid, list) else pmid
    doi  = anns.get("doi", [None])
    doi  = doi[0] if isinstance(doi, list) else doi

    # Build accession list from alternateDataRepository annotation (most reliable)
    alt_repos = anns.get("alternateDataRepository", [])
    if isinstance(alt_repos, str):
        alt_repos = [alt_repos]
    # Fall back to state table accessions
    if not alt_repos:
        for repo, acc in state_accessions:
            prefix = REPO_TO_PREFIX.get(repo)
            if prefix:
                alt_repos.append(f"{prefix}:{acc}")
            else:
                alt_repos.append(acc)

    file_count = get_dataset_file_count(syn, project_id)

    print(f"  Project: {project_id}")
    print(f"  Name:    {study_name[:80]}")
    print(f"  Accessions: {alt_repos}")
    print(f"  Files: {file_count}")

    if dry_run:
        print(f"  [DRY RUN] would create issue for {project_id}")
        return None

    cmd = [
        sys.executable,
        os.path.join(os.environ.get("AGENT_REPO_ROOT", "."), "scripts", "github_issue.py"),
        "--synapse-project-id", project_id,
        "--study-name", study_name,
        "--accessions", *alt_repos,
        "--study-leads", *(study_leads or ["Unknown"]),
        "--assay-types", *(assay_types or ["other"]),
        "--file-count", str(file_count),
        "--outcome", "new",
    ]
    if disease_focus:
        cmd += ["--disease-focus", *disease_focus]
    if manifestation:
        cmd += ["--manifestation", *manifestation]
    if pmid:
        cmd += ["--pmid", str(pmid)]
    if doi:
        cmd += ["--doi", str(doi)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        lines = [l for l in result.stdout.strip().splitlines() if l.startswith("{")]
        if lines:
            issue_data = json.loads(lines[-1])
            url = issue_data.get("issue_url", "")
            print(f"  Issue created: {url}")
            return url
        print(f"  Issue created (no URL parsed)")
        return "ok"
    else:
        print(f"  ERROR: {result.stderr[:300]}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Backfill study-review issues for NADIA projects")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without creating issues")
    args = parser.parse_args()

    state_project_id = os.environ.get("STATE_PROJECT_ID", "")
    if not state_project_id:
        sys.exit("ERROR: STATE_PROJECT_ID not set")

    with open(os.path.join(os.environ.get("AGENT_REPO_ROOT", "."), "config", "settings.yaml")) as f:
        cfg = yaml.safe_load(f)
    prefix = cfg["agent"]["state_table_prefix"]

    print("Connecting to Synapse...")
    syn = get_synapse_client()

    print(f"Looking up state table in {state_project_id}...")
    table_id = get_state_table_id(syn, state_project_id, prefix)
    print(f"  State table: {table_id}")

    print("Fetching created projects from state table...")
    projects = get_all_created_projects(syn, table_id)
    print(f"  Found {len(projects)} project(s)")

    print("Fetching existing study-review issues from GitHub...")
    existing = get_existing_review_issues()
    print(f"  Found {len(existing)} project IDs already in issues")

    missing = {pid: data for pid, data in projects.items() if pid not in existing}
    print(f"  {len(missing)} project(s) need issues\n")

    created = 0
    errors  = 0
    for i, (pid, data) in enumerate(missing.items(), 1):
        print(f"[{i}/{len(missing)}] Processing {pid}...")
        try:
            url = create_issue_for_project(syn, pid, data["accessions"], dry_run=args.dry_run)
            if url:
                created += 1
        except Exception as e:
            print(f"  ERROR: {e}")
            errors += 1
        time.sleep(1)  # avoid GitHub rate limit

    print(f"\nDone. Created: {created}, Errors: {errors}, Already existed: {len(existing)}")
    if args.dry_run:
        print("(dry run — no issues were actually created)")


if __name__ == "__main__":
    main()
