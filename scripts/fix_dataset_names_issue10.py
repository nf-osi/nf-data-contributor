"""
fix_dataset_names_issue10.py — Fix dataset entity names for issue #10.

The previous fix run set syn74301107 and syn74288411 to "Data (ENA ...)"
because the assay annotation wasn't on the Dataset entity itself. This script
renames them to the correct assay-based names and re-syncs the title annotation.

Usage:
    SERVICE_TOKEN=xxx python scripts/fix_dataset_names_issue10.py
"""
import json
import os
import sys
from typing import Any

try:
    import synapseclient
except ImportError:
    print("ERROR: synapseclient not installed", file=sys.stderr)
    sys.exit(1)

# Descriptive names: assay + biological context + accession for traceability
FIXES = [
    # (dataset_id, correct_name)
    ("syn74301107", "Single-cell RNA-seq of NF1 neurofibroma-to-MPNST progression (ENA PRJEB77277)"),
    ("syn74288411", "Single-cell RNA-seq of NF1 neurofibroma-to-MPNST progression (ENA ERP161739)"),
]


def main():
    token = os.environ.get("SERVICE_TOKEN") or os.environ.get("SYNAPSE_AUTH_TOKEN", "")
    if not token:
        print("ERROR: SERVICE_TOKEN or SYNAPSE_AUTH_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    syn = synapseclient.login(authToken=token, silent=True)

    for ds_id, correct_name in FIXES:
        entity: Any = syn.restGET(f"/entity/{ds_id}")
        current = entity.get("name", "")
        if current == correct_name:
            print(f"{ds_id}: name already correct — '{correct_name}'")
        else:
            print(f"{ds_id}: '{current}' → '{correct_name}'")
            entity["name"] = correct_name
            syn.restPUT(f"/entity/{ds_id}", json.dumps(entity))

        # Sync title annotation to match entity name
        ann_raw: Any = syn.restGET(f"/entity/{ds_id}/annotations2")
        ann = ann_raw.get("annotations", {})
        ann["title"] = {"type": "STRING", "value": [correct_name]}
        ann_raw["annotations"] = ann
        syn.restPUT(f"/entity/{ds_id}/annotations2", json.dumps(ann_raw))
        print(f"  title annotation → '{correct_name}'")

    print("Done.")


if __name__ == "__main__":
    main()
