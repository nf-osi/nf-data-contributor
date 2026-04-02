"""
fix_dataset_collection_issues.py — One-off fix for issues #9 and #10.

Problems:
  1. Dataset entities in the DatasetCollection have the wrong title
     (all show the publication title instead of the accession-specific name).
  2. The DatasetCollection references versions that don't exist / show 0 Bytes.

Fix applied:
  For each affected project:
    a. Rename Dataset entity `name` to the accession-specific name if it was
       clobbered (detected by checking whether the current name equals the
       study name).
    b. Set the `title` annotation on each Dataset to match its entity `name`
       (not the publication title).
    c. Mint a new stable snapshot of each Dataset entity.
    d. Update the DatasetCollection item to reference the new snapshot version
       (remove old item, add new one with correct versionNumber).

Usage (run in GitHub Actions or locally with SERVICE_TOKEN set):
    python scripts/fix_dataset_collection_issues.py

Environment variables required:
    SERVICE_TOKEN         — portal service account with write access
    AGENT_REPO_ROOT       — repo root (defaults to parent of scripts/)
"""

import json
import os
import sys

try:
    import synapseclient
except ImportError:
    print("ERROR: synapseclient not installed", file=sys.stderr)
    sys.exit(1)

DATASET_COLLECTION_ID = "syn50913342"

# Projects affected by issues #9 and #10
# Format: {project_id: [(dataset_id, expected_name_hint), ...]}
# expected_name_hint is used to detect/restore a clobbered entity name.
AFFECTED_PROJECTS = {
    # Issue #10 — single-cell RNA-seq, two ENA accessions
    "syn74288246": {
        "study_name": (
            "Deciphering cellular and molecular signatures of malignant "
            "progression in Neurofibromatosis type 1 using single-cell "
            "transcriptomic analysis"
        ),
        "datasets": [
            # (dataset_id, accession, repo)
            ("syn74301107", "PRJEB77277", "ENA"),
            ("syn74288411", "ERP161739",  "ENA"),
        ],
    },
    # Issue #9 — RNA-seq, one ENA accession
    "syn74288412": {
        "study_name": (
            "Triple Combination of MEK, BET, and CDK Inhibitors Significantly "
            "Reduces Human Malignant Peripheral Nerve Sheath Tumors in Mouse Models"
        ),
        "datasets": [
            ("syn74288449", "PRJEB83680", "ENA"),
        ],
    },
}


def get_client():
    token = os.environ.get("SERVICE_TOKEN", "")
    if not token:
        raise RuntimeError("SERVICE_TOKEN not set")
    return synapseclient.login(authToken=token, silent=True)


def fix_dataset(syn, ds_id, accession, repo):
    """
    1. Derive the correct dataset name from assay annotation + accession.
    2. Rename entity if it was clobbered to the study name.
    3. Fix the `title` annotation to match the entity name.
    4. Mint a new stable snapshot.
    Returns the new stable versionNumber.
    """
    ds_entity = syn.restGET(f"/entity/{ds_id}")
    current_name = ds_entity.get("name", "")

    # Derive expected name base: try to get assay from existing annotations.
    # Dataset entities often don't carry the assay annotation themselves; if
    # absent, fall back to sampling the first file in the Dataset's items list.
    ann_raw = syn.restGET(f"/entity/{ds_id}/annotations2")
    ann = ann_raw.get("annotations", {})
    assay_vals = ann.get("assay", {}).get("value", [])
    assay_label = assay_vals[0] if assay_vals else ""
    if not assay_label:
        # Try to read assay from one of the Dataset's items
        try:
            ds_body = syn.restGET(f"/entity/{ds_id}")
            items = ds_body.get("items", [])
            if items:
                file_ann = syn.restGET(f"/entity/{items[0]['entityId']}/annotations2")
                file_assay = file_ann.get("annotations", {}).get("assay", {}).get("value", [])
                assay_label = file_assay[0] if file_assay else ""
        except Exception:
            pass
    assay_label = assay_label or "Data"
    correct_name = f"{assay_label} ({repo} {accession})"

    # If the entity name was clobbered (equals the study name or is clearly wrong),
    # rename it. We detect "wrong" as: name does not contain the accession string.
    if accession not in current_name:
        print(f"  Renaming {ds_id}: '{current_name}' → '{correct_name}'")
        ds_entity["name"] = correct_name
        syn.restPUT(f"/entity/{ds_id}", json.dumps(ds_entity))
    else:
        correct_name = current_name  # already correct — keep as-is
        print(f"  Name OK: '{current_name}'")

    # Fix title annotation to match entity name (not the publication title)
    ann_raw2 = syn.restGET(f"/entity/{ds_id}/annotations2")
    ann2 = ann_raw2.get("annotations", {})
    current_title = ann2.get("title", {}).get("value", [""])[0]
    if current_title != correct_name:
        print(f"  Fixing title annotation: '{current_title}' → '{correct_name}'")
        ann2["title"] = {"type": "STRING", "value": [correct_name]}
        ann_raw2["annotations"] = ann2
        syn.restPUT(f"/entity/{ds_id}/annotations2", json.dumps(ann_raw2))
    else:
        print(f"  Title annotation OK: '{current_title}'")

    # Mint new stable snapshot using the Python client's async transaction endpoint.
    try:
        new_version = syn.create_snapshot_version(
            ds_id, comment="fix-dataset-collection-issues-9-10"
        )
        print(f"  Minted stable snapshot v{new_version} for {ds_id}")
    except Exception as e:
        print(f"  WARN: snapshot mint failed for {ds_id}: {e}", file=sys.stderr)
        # Fall back to current versionNumber
        ds_entity2 = syn.restGET(f"/entity/{ds_id}")
        new_version = ds_entity2.get("versionNumber", 1)
        print(f"  Using current version {new_version}")

    return new_version


def update_collection(syn, dataset_ids_and_versions):
    """
    Remove old entries for the given dataset IDs from the DatasetCollection
    and re-add them with the correct new version numbers.
    """
    collection = syn.restGET(f"/entity/{DATASET_COLLECTION_ID}")
    items = list(collection.get("items", []))

    ds_id_set = {ds_id for ds_id, _ in dataset_ids_and_versions}

    # Remove stale entries
    before_count = len(items)
    items = [item for item in items if item["entityId"] not in ds_id_set]
    removed = before_count - len(items)
    print(f"  Removed {removed} stale item(s) from DatasetCollection")

    # Add updated entries
    for ds_id, version in dataset_ids_and_versions:
        items.append({"entityId": ds_id, "versionNumber": version})
        print(f"  Adding {ds_id} v{version} to DatasetCollection")

    collection["items"] = items
    syn.restPUT(f"/entity/{DATASET_COLLECTION_ID}", json.dumps(collection))
    print(f"  DatasetCollection now has {len(items)} item(s)")


def main():
    print("Connecting to Synapse...")
    syn = get_client()

    for project_id, info in AFFECTED_PROJECTS.items():
        study_name = info["study_name"]
        datasets = info["datasets"]
        print(f"\n{'='*60}")
        print(f"Project {project_id}: {study_name[:80]}...")

        updated_pairs = []
        for ds_id, accession, repo in datasets:
            print(f"\n  Dataset {ds_id} ({repo} {accession})")
            new_version = fix_dataset(syn, ds_id, accession, repo)
            updated_pairs.append((ds_id, new_version))

        print(f"\n  Updating DatasetCollection ({DATASET_COLLECTION_ID})...")
        update_collection(syn, updated_pairs)

    print("\nDone.")


if __name__ == "__main__":
    main()
