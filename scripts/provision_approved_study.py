"""
provision_approved_study.py — Code-only provisioning of an approved NADIA study.

Triggered by the `provision_study.yml` GitHub Actions workflow when a data manager
applies the `approved` label to a study-review issue.

Two Synapse clients are used:
  - syn_portal (SERVICE_TOKEN): has write access to portal assets and admin
    rights on NADIA-created projects (via team membership). Used for steps 1–4.
  - syn_nadia  (SYNAPSE_AUTH_TOKEN): NADIA service account that owns the state tables.
    Used for step 5 only.

Steps performed (all code, no LLM):
  1. Parse issue body to extract Synapse project ID and metadata
  2. Update resourceStatus → 'approved' on: project, all File entities, all Dataset entities
  3. Add project to Studies source ProjectView (studies_source_view_id in config)
     → auto-populates the portal Studies MaterializedView (studies_table_id in config)
  4. Add project numeric ID to portal Files FileView (files_table_id in config) scope
  5. Update NADIA state table (NF_DataContributor_ProcessedStudies) → status = 'approved'
  6. Post a summary comment on the GitHub issue and close the issue

Usage:
    python scripts/provision_approved_study.py --issue-number 42
"""
import argparse
import json
import os
import re
import sys
import urllib.request

# ── Synapse client setup ───────────────────────────────────────────────────────
try:
    import synapseclient
except ImportError:
    print("ERROR: synapseclient not installed — run: pip install synapseclient", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed — run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_config():
    repo_root = os.environ.get("AGENT_REPO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg_path  = os.path.join(repo_root, "config", "settings.yaml")
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def get_portal_client():
    """Synapse client using the portal service account (write access to portal assets)."""
    token = os.environ.get("SERVICE_TOKEN", "")
    if not token:
        raise RuntimeError("SERVICE_TOKEN not set")
    return synapseclient.login(authToken=token, silent=True)


def get_nadia_client():
    """Synapse client using the NADIA service account (write access to NADIA state tables)."""
    token = os.environ.get("SYNAPSE_AUTH_TOKEN", "")
    if not token:
        raise RuntimeError("SYNAPSE_AUTH_TOKEN not set")
    return synapseclient.login(authToken=token, silent=True)


def github_request(method, path, payload=None):
    token = os.environ.get("GITHUB_TOKEN", "")
    repo  = os.environ.get("GITHUB_REPOSITORY", "")
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
            "User-Agent": "NADIA-provisioner/1.0",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def get_issue_body(issue_number):
    issue = github_request("GET", f"issues/{issue_number}")
    return issue.get("body", ""), issue.get("title", "")


def parse_nadia_metadata(issue_body):
    """Extract the embedded JSON metadata block from the issue body."""
    m = re.search(
        r'NADIA_METADATA_JSON\s*(.*?)\s*NADIA_METADATA_JSON',
        issue_body, re.DOTALL
    )
    if not m:
        raise ValueError("Could not find NADIA_METADATA_JSON block in issue body")
    return json.loads(m.group(1).strip())


def get_children_rest(syn, parent_id, types=None):
    """Paginate through entity children using the REST API."""
    if types is None:
        types = ["folder", "file", "dataset", "table", "entityview", "link"]
    children, next_token = [], None
    while True:
        body = {"parentId": parent_id, "includeTypes": types}
        if next_token:
            body["nextPageToken"] = next_token
        resp = syn.restPOST("/entity/children", body=json.dumps(body))
        children.extend(resp.get("page", []))
        next_token = resp.get("nextPageToken")
        if not next_token:
            break
    return children


def update_resource_status(syn, entity_id, new_status="approved"):
    """Set resourceStatus annotation on a single entity (preserves other annotations)."""
    raw = syn.restGET(f"/entity/{entity_id}/annotations2")
    raw["annotations"]["resourceStatus"] = {"type": "STRING", "value": [new_status]}
    syn.restPUT(f"/entity/{entity_id}/annotations2", json.dumps(raw))


# ── Core provisioning steps ────────────────────────────────────────────────────

def step1_update_resource_status(syn, project_id):
    """
    Set resourceStatus=approved on project, all dataset entities, and all files.
    Uses syn_portal which has admin rights on NADIA-created projects.
    """
    updated = {"project": 0, "files": 0, "datasets": 0, "errors": 0}

    # Project
    try:
        update_resource_status(syn, project_id)
        updated["project"] = 1
        print(f"  Project {project_id}: resourceStatus → approved")
    except Exception as e:
        print(f"  WARN: could not update project {project_id}: {e}", file=sys.stderr)
        updated["errors"] += 1

    # Dataset entities (direct children of the project)
    datasets = get_children_rest(syn, project_id, types=["dataset"])
    for ds in datasets:
        try:
            update_resource_status(syn, ds["id"])
            updated["datasets"] += 1
        except Exception as e:
            print(f"  WARN: dataset {ds['id']}: {e}", file=sys.stderr)
            updated["errors"] += 1

    # Files inside Raw Data subfolders
    project_children = get_children_rest(syn, project_id, types=["folder"])
    raw_folder = next((c for c in project_children if c["name"] == "Raw Data"), None)
    if raw_folder:
        subfolders = get_children_rest(syn, raw_folder["id"], types=["folder"])
        for sf in subfolders:
            file_children = list(syn.getChildren(sf["id"], includeTypes=["file"]))
            for fc in file_children:
                try:
                    update_resource_status(syn, fc["id"])
                    updated["files"] += 1
                    if updated["files"] % 50 == 0:
                        print(f"    ... {updated['files']} files updated", flush=True)
                except Exception as e:
                    print(f"  WARN: file {fc['id']}: {e}", file=sys.stderr)
                    updated["errors"] += 1
                    if updated["errors"] > 20:
                        print("  Too many errors — stopping file updates", file=sys.stderr)
                        break

    return updated


def step2_add_to_studies_view(syn, project_id, studies_source_view_id):
    """
    Add this project to the Studies source ProjectView scope so it appears in the
    portal Studies MaterializedView.

    The ProjectView (studies_source_view_id) has a JOIN with syn16787123 (text table)
    to form the MaterializedView (studies_table_id). Adding the project to the
    ProjectView's scope is what surfaces it in the portal Studies page.
    """
    try:
        view = syn.get(studies_source_view_id)
        scope_ids = list(getattr(view, "scopeIds", []) or [])

        # scopeIds in ProjectView store the numeric ID without "syn" prefix
        numeric_id = project_id.replace("syn", "")
        if numeric_id in scope_ids or project_id in scope_ids:
            print(f"  Project {project_id} already in Studies ProjectView scope")
            return True

        scope_ids.append(numeric_id)
        view.scopeIds = scope_ids
        syn.store(view)
        print(f"  Added {project_id} to Studies ProjectView {studies_source_view_id} "
              f"({len(scope_ids)} total)")
        return True
    except Exception as e:
        print(f"  WARN: could not update Studies ProjectView scope: {e}", file=sys.stderr)
        return False


def step3_add_to_files_fileview(syn, project_id, files_table_id):
    """
    Add this project's numeric ID to the portal Files FileView scope so its
    file annotations appear in the portal file browser.

    The Files FileView (files_table_id) uses NUMERIC scopeIds (no 'syn' prefix).
    """
    try:
        fv = syn.get(files_table_id)
        scope_ids = list(getattr(fv, "scopeIds", []) or [])

        # FileView scopeIds are numeric strings (e.g. '74288412' not 'syn74288412')
        numeric_id = project_id.replace("syn", "")
        if numeric_id in scope_ids:
            print(f"  Project {project_id} already in Files FileView scope")
            return True

        scope_ids.append(numeric_id)
        fv.scopeIds = scope_ids
        syn.store(fv)
        print(f"  Added {project_id} (numeric: {numeric_id}) to Files FileView "
              f"{files_table_id} ({len(scope_ids)} total)")
        return True
    except Exception as e:
        print(f"  WARN: could not update Files FileView scope: {e}", file=sys.stderr)
        return False


def step4_update_state_table(syn, cfg, project_id):
    """
    Update NADIA state table rows for this project to status='approved'.
    Uses syn_nadia (SYNAPSE_AUTH_TOKEN) which owns the state tables.
    """
    try:
        state_project_id = os.environ.get("STATE_PROJECT_ID", "")
        if not state_project_id:
            print("  WARN: STATE_PROJECT_ID not set — skipping state table update", file=sys.stderr)
            return False

        prefix     = cfg["agent"]["state_table_prefix"]
        table_name = f"{prefix}_ProcessedStudies"

        children    = get_children_rest(syn, state_project_id, types=["table"])
        state_table = next((c for c in children if c["name"] == table_name), None)
        if not state_table:
            print(f"  WARN: state table '{table_name}' not found in {state_project_id}",
                  file=sys.stderr)
            return False

        table_id = state_table["id"]
        results  = syn.tableQuery(
            f"SELECT * FROM {table_id} WHERE synapse_project_id = '{project_id}'"
        )
        df = results.asDataFrame()
        if df.empty:
            print(f"  WARN: no state rows found for project {project_id}", file=sys.stderr)
            return False

        df["status"] = "approved"
        syn.store(synapseclient.Table(table_id, df))
        print(f"  Updated {len(df)} state table row(s) → status=approved")
        return True

    except Exception as e:
        print(f"  WARN: state table update failed: {e}", file=sys.stderr)
        return False


def post_success_comment(issue_number, project_id, stats,
                         studies_view_added, files_view_added, state_updated):
    synapse_url = f"https://www.synapse.org/Synapse:{project_id}"

    def status_icon(ok):
        return "✅" if ok else "⚠️ manual step needed"

    body_parts = [
        "## ✅ Provisioning Complete\n",
        "The study has been provisioned to the NF Data Portal.\n",
        "| Step | Result |",
        "|------|--------|",
        f"| `resourceStatus` → `approved` | ✅ Project + {stats['files']:,} files + {stats['datasets']} dataset(s) |",
        f"| Added to portal Studies view | {status_icon(studies_view_added)} |",
        f"| Added to portal Files FileView | {status_icon(files_view_added)} |",
        f"| NADIA state table updated | {status_icon(state_updated)} |",
        f"| Errors | {'⚠️ ' + str(stats['errors']) if stats['errors'] else '✅ None'} |",
        "",
        f"**Synapse project:** [{project_id}]({synapse_url})\n",
        "The study should now appear in the NF Data Portal. "
        "If the portal does not reflect the change within 24 hours, please ping the data infrastructure team.",
    ]
    body = "\n".join(body_parts)
    try:
        github_request("POST", f"issues/{issue_number}/comments", {"body": body})
        github_request("PATCH", f"issues/{issue_number}", {
            "state": "closed",
            "state_reason": "completed",
        })
    except Exception as e:
        print(f"  WARN: could not post completion comment: {e}", file=sys.stderr)


def post_failure_comment(issue_number, project_id, error_msg):
    run_url = (
        f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', '')}"
        f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    body_parts = [
        "## ❌ Provisioning Failed\n",
        f"An error occurred during automated provisioning for `{project_id}`.\n",
        f"**Error:** `{error_msg[:500]}`\n",
        f"**Run logs:** {run_url}\n",
        "Please investigate and re-trigger provisioning by removing and re-applying "
        "the `approved` label, or provision manually via Synapse.",
    ]
    body = "\n".join(body_parts)
    try:
        github_request("POST", f"issues/{issue_number}/comments", {"body": body})
    except Exception as e:
        print(f"  WARN: could not post failure comment: {e}", file=sys.stderr)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Provision an approved NADIA study")
    parser.add_argument("--issue-number", required=True, type=int)
    args = parser.parse_args()

    issue_number = args.issue_number
    print(f"Provisioning issue #{issue_number}...", flush=True)

    cfg = load_config()
    dedup = cfg["deduplication"]
    studies_source_view_id = dedup["studies_source_view_id"]
    files_table_id         = dedup["files_table_id"]

    # Two Synapse clients: portal token for portal assets, NADIA token for state tables
    print("Connecting to Synapse (portal account)...")
    syn_portal = get_portal_client()

    print("Connecting to Synapse (NADIA account)...")
    try:
        syn_nadia = get_nadia_client()
    except RuntimeError:
        print("  WARN: SYNAPSE_AUTH_TOKEN not set — state table update will be skipped",
              file=sys.stderr)
        syn_nadia = None

    # Parse issue
    try:
        issue_body, _ = get_issue_body(issue_number)
        metadata = parse_nadia_metadata(issue_body)
    except Exception as e:
        msg = f"Could not parse issue #{issue_number}: {e}"
        print(f"ERROR: {msg}", file=sys.stderr)
        post_failure_comment(issue_number, "unknown", msg)
        sys.exit(1)

    project_id = metadata["synapse_project_id"]
    accessions  = metadata.get("accessions", [])
    print(f"  Project: {project_id}")
    print(f"  Accessions: {accessions}")

    # Step 1 — Update resourceStatus on project, datasets, files
    print("\nStep 1: Updating resourceStatus → approved...")
    try:
        stats = step1_update_resource_status(syn_portal, project_id)
        print(f"  Done: project={stats['project']}, files={stats['files']}, "
              f"datasets={stats['datasets']}, errors={stats['errors']}")
    except Exception as e:
        msg = f"Step 1 failed: {e}"
        print(f"ERROR: {msg}", file=sys.stderr)
        post_failure_comment(issue_number, project_id, msg)
        sys.exit(1)

    # Step 2 — Add to Studies source ProjectView
    print(f"\nStep 2: Adding to Studies source ProjectView ({studies_source_view_id})...")
    studies_view_added = step2_add_to_studies_view(syn_portal, project_id, studies_source_view_id)

    # Step 3 — Add to Files FileView
    print(f"\nStep 3: Adding to Files FileView ({files_table_id})...")
    files_view_added = step3_add_to_files_fileview(syn_portal, project_id, files_table_id)

    # Step 4 — Update NADIA state table
    print("\nStep 4: Updating NADIA state table...")
    if syn_nadia:
        state_updated = step4_update_state_table(syn_nadia, cfg, project_id)
    else:
        state_updated = False
        print("  Skipped (SYNAPSE_AUTH_TOKEN not available)")

    # Step 5 — Post completion comment and close issue
    print("\nStep 5: Posting completion comment and closing issue...")
    post_success_comment(
        issue_number, project_id, stats,
        studies_view_added, files_view_added, state_updated
    )

    print("\nProvisioning complete.", flush=True)
    if stats["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
