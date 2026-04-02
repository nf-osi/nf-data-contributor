"""
provision_approved_study.py — Code-only provisioning of an approved NADIA study.

Triggered by the `provision_study.yml` GitHub Actions workflow when a data manager
applies the `approved` label to a study-review issue.

Steps performed (all code, no LLM):
  1. Parse issue body to extract Synapse project ID and metadata
  2. Update resourceStatus → 'approved' on: project, all File entities, all Dataset entities
  3. Add project to portal FileView scope (syn16858331)
  4. Update NADIA state table (NF_DataContributor_ProcessedStudies) → status = 'approved'
  5. Post a summary comment on the GitHub issue

Usage:
    python scripts/provision_approved_study.py --issue-number 42
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

# ── Synapse client setup ───────────────────────────────────────────────────────
try:
    import synapseclient
    from synapseclient import EntityViewSchema, EntityViewType
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


def get_synapse_client(cfg):
    token = os.environ.get("SYNAPSE_AUTH_TOKEN", "")
    if not token:
        raise RuntimeError("SYNAPSE_AUTH_TOKEN not set")
    syn = synapseclient.login(authToken=token, silent=True)
    return syn


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


def post_comment(issue_number, body):
    github_request("POST", f"issues/{issue_number}/comments", {"body": body})


def get_issue_body(issue_number):
    issue = github_request("GET", f"issues/{issue_number}")
    return issue.get("body", ""), issue.get("title", "")


def parse_nadia_metadata(issue_body):
    """Extract the embedded JSON metadata block from the issue body."""
    m = re.search(
        r'<!--\s*NADIA_METADATA_JSON\s*(.*?)\s*NADIA_METADATA_JSON\s*-->',
        issue_body, re.DOTALL
    )
    if not m:
        raise ValueError("Could not find NADIA_METADATA_JSON block in issue body")
    return json.loads(m.group(1))


def get_children_rest(syn, parent_id, types=None):
    """Paginate through entity children using the REST API (returns correct types)."""
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
    """Set resourceStatus=approved on project, all files, and all dataset entities."""
    updated = {"project": 0, "files": 0, "datasets": 0, "errors": 0}

    # Project
    try:
        update_resource_status(syn, project_id)
        updated["project"] = 1
    except Exception as e:
        print(f"  WARN: could not update project {project_id}: {e}", file=sys.stderr)
        updated["errors"] += 1

    # Dataset entities (direct children)
    datasets = get_children_rest(syn, project_id, types=["dataset"])
    for ds in datasets:
        try:
            update_resource_status(syn, ds["id"])
            updated["datasets"] += 1
        except Exception as e:
            print(f"  WARN: dataset {ds['id']}: {e}", file=sys.stderr)
            updated["errors"] += 1

    # Files (inside Raw Data subfolders)
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


def step2_add_to_portal_fileview(syn, project_id, fileview_id):
    """Add project to the portal FileView scope so its files appear in the portal."""
    try:
        fv = syn.get(fileview_id)
        scope_ids = list(getattr(fv, "scopeIds", []) or [])
        if project_id in scope_ids:
            print(f"  Project {project_id} already in FileView scope")
            return True
        scope_ids.append(project_id)
        fv.scopeIds = scope_ids
        syn.store(fv)
        print(f"  Added {project_id} to FileView {fileview_id} scope ({len(scope_ids)} total)")
        return True
    except Exception as e:
        print(f"  WARN: could not update FileView scope: {e}", file=sys.stderr)
        return False


def step3_update_state_table(syn, cfg, accessions, project_id):
    """Update NADIA state table rows for this project to status='approved'."""
    try:
        state_project_id = os.environ.get("STATE_PROJECT_ID", "")
        if not state_project_id:
            print("  WARN: STATE_PROJECT_ID not set — skipping state table update", file=sys.stderr)
            return False

        prefix = cfg["agent"]["state_table_prefix"]
        table_name = f"{prefix}_ProcessedStudies"

        # Find state table ID
        children = get_children_rest(syn, state_project_id, types=["table"])
        state_table = next((c for c in children if c["name"] == table_name), None)
        if not state_table:
            print(f"  WARN: state table '{table_name}' not found in {state_project_id}", file=sys.stderr)
            return False

        table_id = state_table["id"]

        # Query current rows for this project
        results = syn.tableQuery(
            f"SELECT * FROM {table_id} WHERE synapse_project_id = '{project_id}'"
        )
        df = results.asDataFrame()
        if df.empty:
            print(f"  WARN: no state rows found for project {project_id}", file=sys.stderr)
            return False

        df["status"] = "approved"
        syn.store(synapseclient.Table(table_id, df))
        print(f"  Updated {len(df)} state table row(s) to status=approved")
        return True

    except Exception as e:
        print(f"  WARN: state table update failed: {e}", file=sys.stderr)
        return False


def step4_post_success_comment(issue_number, project_id, stats, fileview_added, state_updated):
    synapse_url = f"https://www.synapse.org/Synapse:{project_id}"
    fv_status   = "✅" if fileview_added else "⚠️ (manual step needed)"
    st_status   = "✅" if state_updated  else "⚠️ (manual step needed)"

    body = f"""\
## ✅ Provisioning Complete

The study has been provisioned to the NF Data Portal.

| Step | Result |
|------|--------|
| `resourceStatus` → `approved` | ✅ Project + {stats['files']:,} files + {stats['datasets']} datasets updated |
| Added to portal FileView | {fv_status} |
| State table updated | {st_status} |
| Errors | {'⚠️ ' + str(stats['errors']) if stats['errors'] else '✅ None'} |

**Synapse project:** [{project_id}]({synapse_url})

The study's files should now appear in the NF Data Portal file browser. \
If the portal does not reflect the change within 24 hours, please ping the data infrastructure team.
"""
    try:
        github_request("POST", f"issues/{issue_number}/comments", {"body": body})
        # Close the issue now that provisioning is done
        github_request("PATCH", f"issues/{issue_number}", {"state": "closed", "state_reason": "completed"})
    except Exception as e:
        print(f"  WARN: could not post completion comment: {e}", file=sys.stderr)


def step4_post_failure_comment(issue_number, project_id, error_msg):
    run_url = (
        f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', '')}"
        f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    )
    body = f"""\
## ❌ Provisioning Failed

An error occurred during automated provisioning for `{project_id}`.

**Error:** `{error_msg[:500]}`

**Run logs:** {run_url}

Please investigate and re-trigger provisioning by removing and re-applying the `approved` label, \
or provision manually via Synapse.
"""
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
    syn = get_synapse_client(cfg)
    fileview_id = cfg["deduplication"]["files_table_id"]

    # Parse issue
    try:
        issue_body, issue_title = get_issue_body(issue_number)
        metadata = parse_nadia_metadata(issue_body)
    except Exception as e:
        msg = f"Could not parse issue #{issue_number}: {e}"
        print(f"ERROR: {msg}", file=sys.stderr)
        step4_post_failure_comment(issue_number, "unknown", msg)
        sys.exit(1)

    project_id = metadata["synapse_project_id"]
    accessions  = metadata.get("accessions", [])
    print(f"  Project: {project_id}")
    print(f"  Accessions: {accessions}")

    # Step 1 — Update resourceStatus
    print("\nStep 1: Updating resourceStatus → approved...")
    try:
        stats = step1_update_resource_status(syn, project_id)
        print(f"  Done: project={stats['project']}, files={stats['files']}, "
              f"datasets={stats['datasets']}, errors={stats['errors']}")
    except Exception as e:
        msg = f"Step 1 failed: {e}"
        print(f"ERROR: {msg}", file=sys.stderr)
        step4_post_failure_comment(issue_number, project_id, msg)
        sys.exit(1)

    # Step 2 — Add to portal FileView
    print(f"\nStep 2: Adding to portal FileView ({fileview_id})...")
    fileview_added = step2_add_to_portal_fileview(syn, project_id, fileview_id)

    # Step 3 — Update state table
    print("\nStep 3: Updating NADIA state table...")
    state_updated = step3_update_state_table(syn, cfg, accessions, project_id)

    # Step 4 — Comment + close issue
    print("\nStep 4: Posting completion comment...")
    step4_post_success_comment(issue_number, project_id, stats, fileview_added, state_updated)

    print("\nProvisioning complete.", flush=True)
    if stats["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
