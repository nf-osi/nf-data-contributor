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
  2. Update resourceStatus → 'approved' on: project and all Dataset entities (not files)
  3. Add project to Studies source ProjectView (studies_source_view_id in config)
     → auto-populates the portal Studies MaterializedView (studies_table_id in config)
  4. Add project numeric ID to portal Files FileView (files_table_id in config) scope
  5. Populate study long-text table (long_text_table_id) with standard access requirements
     and acknowledgement statements
  6. Upsert publication record into portal publications table (publications_table_id)
     — fetches full author list, journal, and year from PubMed via NCBI efetch
  7. Add project's Dataset entities to portal DatasetCollection (dataset_collection_id)
  8. Update NADIA state table (NF_DataContributor_ProcessedStudies) → status = 'approved'
  9. Post a summary comment on the GitHub issue and close the issue

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

def step1_update_resource_status(syn, project_id, metadata):
    """
    Set resourceStatus=approved on the project and all Dataset entities.
    Also ensures Dataset entities carry the portal-facing annotations
    (studyId, title, creator) required for display in the Dataset Collection.
    Files do not carry resourceStatus — it is a project-level annotation only.
    """
    updated = {"project": 0, "datasets": 0, "errors": 0}

    study_name  = metadata.get("study_name", "")
    study_leads = metadata.get("study_leads", [])

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
            raw = syn.restGET(f"/entity/{ds['id']}/annotations2")
            ann = raw.get("annotations", {})
            ann["resourceStatus"] = {"type": "STRING", "value": ["approved"]}
            ann["studyId"]  = {"type": "STRING", "value": [project_id]}
            ann["title"]    = {"type": "STRING", "value": [study_name]}
            ann["creator"]  = {"type": "STRING", "value": study_leads}
            raw["annotations"] = ann
            syn.restPUT(f"/entity/{ds['id']}/annotations2", json.dumps(raw))
            updated["datasets"] += 1
        except Exception as e:
            print(f"  WARN: dataset {ds['id']}: {e}", file=sys.stderr)
            updated["errors"] += 1

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


ACCESS_REQUIREMENTS = (
    "Data in this study are hosted in one or more external repositories. "
    "Depending on the specific dataset, data may be openly accessible or may require "
    "registration and approval at the originating repository. "
    "Please review and comply with the access requirements of the source repository "
    "before downloading or using these data."
)

ACKNOWLEDGEMENT_STATEMENTS = (
    "If you use these data in a publication or presentation, please cite the source "
    "data publication (listed above). Additionally, please include the following "
    "statement in your acknowledgements to help us track the usage and impact of the "
    "NF Data Portal:\n\n"
    "> \"Data were identified through the NF Data Portal "
    "(http://www.nf.synapse.org, RRID:SCR_021683).\""
)


def step4_upsert_long_text(syn, project_id, metadata, long_text_table_id):
    """
    Insert or update a row in the Portal Study Long Text table (syn16787123).

    Columns: studyId (ENTITYID), summary (LARGETEXT), accessRequirements (LARGETEXT),
             acknowledgementStatements (LARGETEXT)

    The summary is built from available metadata. accessRequirements and
    acknowledgementStatements use the standard NADIA boilerplate.
    """
    import pandas as pd

    study_name  = metadata.get("study_name", project_id)
    accessions  = metadata.get("accessions", [])
    pmid        = metadata.get("pmid", "")
    doi         = metadata.get("doi", "")

    # Build a concise summary from available metadata
    acc_str = ", ".join(accessions) if accessions else "an external repository"
    summary_parts = [
        f'Data from the publication "{study_name}" are hosted externally ({acc_str}).',
    ]
    if pmid:
        summary_parts.append(f"Source publication: https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
    elif doi:
        summary_parts.append(f"Source publication: https://doi.org/{doi}")
    summary = " ".join(summary_parts)

    try:
        # Check for existing row
        existing = syn.tableQuery(
            f"SELECT * FROM {long_text_table_id} WHERE studyId = '{project_id}'"
        )
        df_existing = existing.asDataFrame()

        new_row = {
            "studyId": project_id,
            "summary": summary,
            "accessRequirements": ACCESS_REQUIREMENTS,
            "acknowledgementStatements": ACKNOWLEDGEMENT_STATEMENTS,
        }

        if not df_existing.empty:
            # Update in place — preserve any existing summary if curator wrote one
            df_existing["accessRequirements"]    = ACCESS_REQUIREMENTS
            df_existing["acknowledgementStatements"] = ACKNOWLEDGEMENT_STATEMENTS
            if not df_existing["summary"].iloc[0]:
                df_existing["summary"] = summary
            syn.store(synapseclient.Table(long_text_table_id, df_existing))
            print(f"  Updated existing long-text row for {project_id}")
        else:
            df_new = pd.DataFrame([new_row])
            syn.store(synapseclient.Table(long_text_table_id, df_new))
            print(f"  Inserted long-text row for {project_id}")

        return True
    except Exception as e:
        print(f"  WARN: long-text table update failed: {e}", file=sys.stderr)
        return False


def _fetch_pubmed_details(pmid):
    """
    Fetch full publication metadata from PubMed for a given PMID.
    Returns a dict with keys: title, year, journal, authors (full list), doi.
    Falls back gracefully on any error.
    """
    try:
        import urllib.request as _req
        import xml.etree.ElementTree as ET

        url = (
            f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
            f"?db=pubmed&id={pmid}&rettype=xml&retmode=xml"
        )
        ncbi_key = os.environ.get("NCBI_API_KEY", "")
        if ncbi_key:
            url += f"&api_key={ncbi_key}"

        with _req.urlopen(url, timeout=20) as resp:
            xml_bytes = resp.read()

        root = ET.fromstring(xml_bytes)
        article = root.find(".//MedlineCitation/Article")
        if article is None:
            return {}

        # Title
        title_el = article.find("ArticleTitle")
        title = "".join(title_el.itertext()) if title_el is not None else ""

        # Journal
        journal_el = article.find("Journal/Title")
        journal = journal_el.text if journal_el is not None else ""

        # Year
        year = None
        pub_date = article.find("Journal/JournalIssue/PubDate")
        if pub_date is not None:
            yr_el = pub_date.find("Year")
            if yr_el is not None:
                try:
                    year = int(yr_el.text or "")
                except (ValueError, TypeError):
                    pass

        # Authors — full list
        authors = []
        for author in article.findall("AuthorList/Author"):
            last     = author.findtext("LastName", "")
            initials = author.findtext("Initials", "")
            if last:
                # "Smith J" format used by portal
                name = f"{last} {initials}" if initials else last
                authors.append(name)

        # DOI from ArticleIdList
        doi = ""
        for aid in root.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = aid.text or ""
                break

        return {"title": title, "year": year, "journal": journal,
                "authors": authors, "doi": doi}

    except Exception as e:
        print(f"  WARN: PubMed fetch for PMID {pmid} failed: {e}", file=sys.stderr)
        return {}


def step5_upsert_publication(syn, project_id, metadata, publications_table_id):
    """
    Insert a row into the Portal Publications table (syn16857542) if the
    publication (matched by PMID or DOI) is not already present.

    Fetches full author list, journal, and year from PubMed when a PMID is available.

    Columns: doi, diseaseFocus, journal, title, year, pmid (prefix 'PMID:'),
             author (STRING_LIST), manifestation (STRING_LIST),
             fundingAgency (STRING_LIST), studyId (STRING_LIST), studyName (STRING_LIST)
    """
    import datetime
    import pandas as pd

    pmid          = metadata.get("pmid", "")
    doi           = metadata.get("doi", "")
    study_name    = metadata.get("study_name", "")
    study_leads   = metadata.get("study_leads", [])   # fallback if PubMed unavailable
    disease_focus = metadata.get("disease_focus", [])
    manifestation = metadata.get("manifestation", [])

    if not pmid and not doi:
        print("  No PMID or DOI in metadata — skipping publications table", file=sys.stderr)
        return False

    try:
        # Check for existing row by PMID or DOI
        pmid_formatted = f"PMID:{pmid}" if pmid else ""
        existing = None

        if pmid_formatted:
            res = syn.tableQuery(
                f"SELECT * FROM {publications_table_id} WHERE pmid = '{pmid_formatted}'"
            )
            df_check = res.asDataFrame()
            if not df_check.empty:
                existing = df_check

        if existing is None and doi:
            res = syn.tableQuery(
                f"SELECT * FROM {publications_table_id} WHERE doi = '{doi}'"
            )
            df_check = res.asDataFrame()
            if not df_check.empty:
                existing = df_check

        if existing is not None and not existing.empty:
            print("  Publication already in portal publications table — skipping insert")
            return True

        # Enrich from PubMed if PMID available
        pub_details = _fetch_pubmed_details(pmid) if pmid else {}

        title   = pub_details.get("title") or study_name
        year    = pub_details.get("year") or datetime.date.today().year
        journal = pub_details.get("journal") or None
        authors = pub_details.get("authors") or study_leads or None
        doi     = pub_details.get("doi") or doi or None
        disease_focus_val = disease_focus[0] if disease_focus else None

        new_row = {
            "title":         title,
            "pmid":          pmid_formatted or None,
            "doi":           doi,
            "year":          year,
            "author":        authors,
            "journal":       journal,
            "diseaseFocus":  disease_focus_val,
            "manifestation": manifestation if manifestation else None,
            "studyId":       [project_id],
            "studyName":     [study_name[:200]] if study_name else None,
            "fundingAgency": [],
        }

        df_new = pd.DataFrame([new_row])
        syn.store(synapseclient.Table(publications_table_id, df_new))
        n_authors = len(authors) if authors else 0
        print(f"  Inserted publication: '{title[:80]}' "
              f"({year}, {journal or 'journal unknown'}, {n_authors} authors)")
        return True

    except Exception as e:
        print(f"  WARN: publications table update failed: {e}", file=sys.stderr)
        return False


def step6_add_to_dataset_collection(syn, project_id, collection_id):
    """
    Add each Dataset entity in the project to the portal DatasetCollection
    (dataset_collection_id in config).

    The DatasetCollection uses optimistic concurrency: GET to read current etag
    + items, append new items, then PUT back. Each item is
    {"entityId": "syn...", "versionNumber": N} where N is the dataset's current
    stable version.
    """
    try:
        # Find Dataset entities that are direct children of the project
        datasets = get_children_rest(syn, project_id, types=["dataset"])
        if not datasets:
            print(f"  No Dataset entities found in {project_id} — skipping")
            return True

        # GET current DatasetCollection (need etag + existing items)
        collection = syn.restGET(f"/entity/{collection_id}")
        existing_ids = {item["entityId"] for item in collection.get("items", [])}
        items = list(collection.get("items", []))

        added = 0
        for ds in datasets:
            ds_id = ds["id"]
            if ds_id in existing_ids:
                print(f"  Dataset {ds_id} already in collection — skipping")
                continue
            # Get current version number
            ds_entity = syn.restGET(f"/entity/{ds_id}")
            version = ds_entity.get("versionNumber", 1)
            items.append({"entityId": ds_id, "versionNumber": version})
            added += 1
            print(f"  Queued {ds_id} (version {version}) for collection")

        if added == 0:
            return True

        # PUT updated collection back (etag required for optimistic concurrency)
        collection["items"] = items
        syn.restPUT(f"/entity/{collection_id}", json.dumps(collection))
        print(f"  Added {added} dataset(s) to DatasetCollection {collection_id} "
              f"({len(items)} total)")
        return True

    except Exception as e:
        print(f"  WARN: dataset collection update failed: {e}", file=sys.stderr)
        return False


def step7_update_state_table(syn, cfg, project_id):
    """Update NADIA state table rows for this project to status='approved'."""
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
                         studies_view_added, files_view_added,
                         long_text_updated, pub_upserted,
                         collection_updated, state_updated):
    synapse_url = f"https://www.synapse.org/Synapse:{project_id}"

    def status_icon(ok):
        return "✅" if ok else "⚠️ manual step needed"

    body_parts = [
        "## ✅ Provisioning Complete\n",
        "The study has been provisioned to the NF Data Portal.\n",
        "| Step | Result |",
        "|------|--------|",
        f"| `resourceStatus` → `approved` | ✅ Project + {stats['datasets']} dataset(s) |",
        f"| Added to portal Studies view | {status_icon(studies_view_added)} |",
        f"| Added to portal Files FileView | {status_icon(files_view_added)} |",
        f"| Study long-text populated | {status_icon(long_text_updated)} |",
        f"| Publication record upserted | {status_icon(pub_upserted)} |",
        f"| Added to Dataset Collection | {status_icon(collection_updated)} |",
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
    studies_source_view_id  = dedup["studies_source_view_id"]
    files_table_id          = dedup["files_table_id"]
    long_text_table_id      = dedup["long_text_table_id"]
    publications_table_id   = dedup["publications_table_id"]
    dataset_collection_id   = dedup["dataset_collection_id"]

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
        stats = step1_update_resource_status(syn_portal, project_id, metadata)
        print(f"  Done: project={stats['project']}, datasets={stats['datasets']}, "
              f"errors={stats['errors']}")
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

    # Step 4 — Populate study long-text (summary, access requirements, acknowledgements)
    print(f"\nStep 4: Populating study long-text table ({long_text_table_id})...")
    long_text_updated = step4_upsert_long_text(syn_portal, project_id, metadata, long_text_table_id)

    # Step 5 — Insert publication record if not already present
    print(f"\nStep 5: Upserting publication record ({publications_table_id})...")
    pub_upserted = step5_upsert_publication(syn_portal, project_id, metadata, publications_table_id)

    # Step 6 — Add datasets to portal DatasetCollection
    print(f"\nStep 6: Adding datasets to DatasetCollection ({dataset_collection_id})...")
    collection_updated = step6_add_to_dataset_collection(
        syn_portal, project_id, dataset_collection_id
    )

    # Step 7 — Update NADIA state table
    print("\nStep 7: Updating NADIA state table...")
    if syn_nadia:
        state_updated = step7_update_state_table(syn_nadia, cfg, project_id)
    else:
        state_updated = False
        print("  Skipped (SYNAPSE_AUTH_TOKEN not available)")

    # Step 8 — Post completion comment and close issue
    print("\nStep 8: Posting completion comment and closing issue...")
    post_success_comment(
        issue_number, project_id, stats,
        studies_view_added, files_view_added,
        long_text_updated, pub_upserted,
        collection_updated, state_updated,
    )

    print("\nProvisioning complete.", flush=True)
    if stats["errors"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
