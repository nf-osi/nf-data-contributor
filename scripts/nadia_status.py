"""
nadia_status.py — Code-only status check for a NADIA study-review issue.

Reads the Synapse project's current annotation state and posts a status
comment on the GitHub issue.  No LLM calls.

Usage:
    python scripts/nadia_status.py --issue-number 42
"""
import argparse
import json
import os
import re
import sys
import urllib.request

try:
    import synapseclient
except ImportError:
    print("ERROR: synapseclient not installed", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr)
    sys.exit(1)


def load_config():
    repo_root = os.environ.get("AGENT_REPO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg_path  = os.path.join(repo_root, "config", "settings.yaml")
    with open(cfg_path) as f:
        return yaml.safe_load(f)


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
            "User-Agent": "NADIA-status/1.0",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def ga(ann, k):
    v = (ann.get(k) or {}).get("value", [])
    return v if isinstance(v, list) else [v]


def get_children_rest(syn, parent_id, types=None):
    if types is None:
        types = ["folder", "file", "dataset"]
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


def check_project(syn, project_id):
    """Return a dict summarising annotation health for the project."""
    result = {
        "project_id": project_id,
        "project_url": f"https://www.synapse.org/Synapse:{project_id}",
        "required_fields": {},
        "datasets": [],
        "file_sample": [],
        "resource_status": "",
        "schema_bound": [],
        "validation": [],
        "errors": [],
    }

    REQUIRED_PROJECT = [
        "studyName", "studyStatus", "dataStatus", "diseaseFocus", "manifestation",
        "dataType", "studyLeads", "institutions", "fundingAgency", "resourceStatus",
        "alternateDataRepository",
    ]
    REQUIRED_FILE = [
        "study", "assay", "species", "tumorType", "diagnosis",
        "fileFormat", "resourceType", "resourceStatus",
        "specimenID", "individualID", "dataSubtype",
        "externalAccessionID", "externalRepository",
    ]

    try:
        raw  = syn.restGET(f"/entity/{project_id}/annotations2")
        ann  = raw.get("annotations", {})
        result["resource_status"] = (ga(ann, "resourceStatus") or [""])[0]
        result["study_name"]      = (ga(ann, "studyName") or [""])[0]

        for field in REQUIRED_PROJECT:
            vals = ga(ann, field)
            present = bool(vals and vals != [""] and vals != [None])
            result["required_fields"][field] = "✅" if present else "❌ missing"

    except Exception as e:
        result["errors"].append(f"Project annotation read failed: {e}")
        return result

    # Dataset entities
    datasets = get_children_rest(syn, project_id, types=["dataset"])
    for ds in datasets:
        result["datasets"].append({"name": ds["name"], "id": ds["id"]})

    # Raw Data → subfolders → schema + sample files
    proj_children = get_children_rest(syn, project_id, types=["folder"])
    raw_folder = next((c for c in proj_children if c["name"] == "Raw Data"), None)
    if raw_folder:
        subfolders = get_children_rest(syn, raw_folder["id"], types=["folder"])
        for sf in subfolders:
            # Schema binding
            try:
                binding = syn.restGET(f"/entity/{sf['id']}/schema/binding")
                schema_id = binding.get("jsonSchemaVersionInfo", {}).get("$id", "unknown")
                result["schema_bound"].append(f"{sf['name']}: {schema_id.split('/')[-1]}")
            except Exception:
                result["schema_bound"].append(f"{sf['name']}: ❌ no schema bound")

            # Validation stats
            try:
                vstats = syn.restGET(f"/entity/{sf['id']}/schema/validation/statistics")
                total   = vstats.get("totalNumberOfChildren", 0)
                valid   = vstats.get("numberOfValidChildren", 0)
                invalid = vstats.get("numberOfInvalidChildren", 0)
                result["validation"].append(
                    f"{sf['name']}: {valid}/{total} valid, {invalid} invalid"
                )
            except Exception:
                pass

            # Sample 1 file
            file_children = list(syn.getChildren(sf["id"], includeTypes=["file"]))[:1]
            for fc in file_children:
                try:
                    fraw = syn.restGET(f"/entity/{fc['id']}/annotations2")
                    fa   = fraw.get("annotations", {})
                    missing = [f for f in REQUIRED_FILE
                               if not ga(fa, f) or ga(fa, f) == [""] or ga(fa, f) == [None]]
                    result["file_sample"].append({
                        "name": fc["name"],
                        "id": fc["id"],
                        "missing": missing,
                        "assay": (ga(fa, "assay") or [""])[0],
                        "resourceStatus": (ga(fa, "resourceStatus") or [""])[0],
                    })
                except Exception:
                    pass

    return result


def format_status_comment(status):
    pid    = status["project_id"]
    url    = status["project_url"]
    name   = status.get("study_name", pid)
    rs     = status.get("resource_status", "")

    lines = [
        f"## 📊 NADIA Status: [{name}]({url})",
        "",
        f"**resourceStatus:** `{rs}`",
        "",
        "### Project Annotations",
        "| Field | Status |",
        "|-------|--------|",
    ]
    for field, status_str in status["required_fields"].items():
        lines.append(f"| `{field}` | {status_str} |")

    lines += ["", "### Dataset Entities"]
    if status["datasets"]:
        for ds in status["datasets"]:
            lines.append(f"- `{ds['id']}`: {ds['name']}")
    else:
        lines.append("- ❌ No Dataset entities found")

    lines += ["", "### Schema Binding"]
    for s in status["schema_bound"]:
        lines.append(f"- {s}")
    if not status["schema_bound"]:
        lines.append("- ❌ No schema bindings found")

    lines += ["", "### Validation"]
    for v in status["validation"]:
        lines.append(f"- {v}")
    if not status["validation"]:
        lines.append("- (no validation data)")

    if status["file_sample"]:
        lines += ["", "### File Sample (1 file per folder)"]
        for fs in status["file_sample"]:
            miss_str = ", ".join(fs["missing"]) if fs["missing"] else "all present ✅"
            lines.append(
                f"- `{fs['name']}` — assay: `{fs['assay']}`, "
                f"resourceStatus: `{fs['resourceStatus']}`, "
                f"missing: {miss_str}"
            )

    if status["errors"]:
        lines += ["", "### Errors"]
        for e in status["errors"]:
            lines.append(f"- ⚠️ {e}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True, type=int)
    args = parser.parse_args()

    token = os.environ.get("SYNAPSE_AUTH_TOKEN", "")
    syn   = synapseclient.login(authToken=token, silent=True)

    # Get issue body and extract project ID
    issue = github_request("GET", f"issues/{args.issue_number}")
    body  = issue.get("body", "")
    m = re.search(r'NADIA_METADATA_JSON\s*(.*?)\s*NADIA_METADATA_JSON', body, re.DOTALL)
    if m:
        try:
            meta = json.loads(m.group(1).strip())
            project_id = meta.get("synapse_project_id", "")
            if project_id:
                print(f"Checking status of {project_id}...")
                status  = check_project(syn, project_id)
                comment = format_status_comment(status)
                github_request("POST", f"issues/{args.issue_number}/comments", {"body": comment})
                print("Status comment posted.")
                return
        except Exception:
            pass
    m     = re.search(r'"synapse_project_id":\s*"(syn\d+)"', body)
    if not m:
        print("ERROR: Could not find Synapse project ID in issue body", file=sys.stderr)
        github_request("POST", f"issues/{args.issue_number}/comments", {
            "body": "⚠️ Could not parse Synapse project ID from issue body. "
                    "Please check the issue template and try again."
        })
        sys.exit(1)

    project_id = m.group(1)
    print(f"Checking status of {project_id}...")

    status  = check_project(syn, project_id)
    comment = format_status_comment(status)
    github_request("POST", f"issues/{args.issue_number}/comments", {"body": comment})
    print("Status comment posted.")


if __name__ == "__main__":
    main()
