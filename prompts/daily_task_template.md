# NF Data Contributor Agent — Daily Run

**Today's date:** {{TODAY}}
**Search for publications/datasets since:** {{LOOKBACK_DATE}}

---

## Your Task

Run the full NF dataset discovery pipeline using a **publication-first** approach: find papers first via PubMed, then resolve their data deposits. Write Python scripts to `/tmp/nf_agent/` and execute them. Refer to CLAUDE.md for all API patterns, auth, annotation schemas, and safety rules.

---

## Step 1 — Setup

```
mkdir -p /tmp/nf_agent
pip install synapseclient httpx biopython pyyaml scikit-learn anthropic --quiet
```

Write and run `/tmp/nf_agent/setup.py`:
1. Authenticate with Synapse via `lib/synapse_login.py` — print the logged-in username
2. Get or create state tables via `lib/state_bootstrap.py`. If `STATE_PROJECT_ID` is empty, create a Synapse project named "NF DataContributor Agent State" and use it.
3. Load all previously processed accession IDs into a Python set
4. Save table IDs, accession set, and state project ID to `/tmp/nf_agent/state.json`
5. Print: "Setup complete. Previously processed: N accessions"

---

## Step 2 — PRIMARY DISCOVERY: PubMed + elink + Europe PMC

Write and run `/tmp/nf_agent/discover_primary.py`.

This is the main discovery path. The goal is: find all NF/SWN publications from the lookback window, then systematically resolve every data deposit associated with each paper.

### 2a — Search PubMed

Use the MeSH-based query from CLAUDE.md with date filter `{{LOOKBACK_DATE}}` to `{{TODAY}}`. Fetch up to 200 PMIDs. For each PMID, fetch the full record (title, abstract, authors, DOI, pub date).

Print: `PubMed: found N publications`

### 2b — NCBI elink: resolve linked datasets

For each batch of PMIDs (up to 100 at a time to respect API limits):
- `elink(dbfrom='pubmed', db='gds', ...)` → GEO dataset IDs → fetch GEO metadata via `esummary`
- `elink(dbfrom='pubmed', db='sra', ...)` → SRA study IDs → fetch SRA runinfo metadata
- `elink(dbfrom='pubmed', db='gap', ...)` → dbGaP study IDs → note as controlled access

For each linked accession, record: accession_id, source_repository, data_url, data_types, file_formats, sample_count, access_type, discovery_path="ncbi_elink".

### 2c — Europe PMC annotations: find accessions mentioned in full text

For each PMID, call the Europe PMC annotations API (see CLAUDE.md for the exact pattern). This finds GEO, SRA, EGA, PRIDE, ArrayExpress, Zenodo, MetaboLights, and other accessions mentioned anywhere in the paper — even in supplementary notes or data availability statements.

For each returned accession not already found via elink:
- Identify the repository from the `provider` field
- Fetch basic metadata from that repository's API
- Add to the paper's dataset list with `discovery_path="europepmc_annotations"`

Be resilient: Europe PMC returns 404 or empty for papers not in open-access PMC — that's normal, just skip and continue.

### 2d — Assemble publication groups

Combine elink + Europe PMC results into `publication_groups.json`. Each group has one PMID as its key and all datasets found for that paper. See CLAUDE.md for the full schema.

Print a summary:
```
Primary discovery complete:
  Publications scanned: N
  Publications with linked data: M
  Total dataset accessions found: K
  Breakdown: GEO: N, SRA: N, dbGaP: N, EGA: N, PRIDE: N, other: N
```

---

## Step 3 — SECONDARY DISCOVERY: Repository-direct (unpublished/preprint data)

Write and run `/tmp/nf_agent/discover_secondary.py`.

Query these repositories with NF keywords for datasets published since `{{LOOKBACK_DATE}}`. For each result, check if it has a PMID or DOI that was already found in the primary path — if so, skip it (it's already covered). Only keep datasets with no associated publication yet.

Repositories to query:
- Zenodo (`https://zenodo.org/api/records`) — search `resource_type.type:dataset`
- Figshare (`https://api.figshare.com/v2/articles/search`) — `item_type=3`
- OSF (`https://api.osf.io/v2/nodes/`) — public projects
- ArrayExpress/BioStudies (`https://www.ebi.ac.uk/biostudies/api/v1/search`)
- PRIDE (`https://www.ebi.ac.uk/pride/ws/archive/v2/projects`)
- MetaboLights (`https://www.ebi.ac.uk/metabolights/ws`)
- NCI PDC (GraphQL — filter for NF disease types)

For unpublished results, create publication groups with `pmid: null`, using the repository title as the publication title.

Print: `Secondary discovery: N additional datasets (no associated publication)`

---

## Step 4 — Deduplicate Against Portal

Write and run `/tmp/nf_agent/dedup.py`:

1. Load all publication groups from Steps 2 and 3
2. Remove any group whose accession_ids are all already in the processed accessions set from state.json
3. Inspect portal schema: `SELECT * FROM syn52694652 LIMIT 5` and `SELECT * FROM syn16858331 LIMIT 5` — print actual column names before writing any queries
4. Classify each remaining group as NEW, ADD, or SKIP using the three-outcome logic in CLAUDE.md:
   - **PMID match** (exact) → strongest signal for ADD or SKIP
   - **DOI match** (case-insensitive)
   - **Accession match** in portal files table
   - **Fuzzy title** (TF-IDF cosine ≥ 0.85 = match; 0.70–0.84 = near-match warning, treat as NEW)
5. Save to `/tmp/nf_agent/dedup_results.json`
6. Print:
   ```
   Dedup: N new | M add-to-existing | K skip | J near-match warnings
   Near-matches:
     "Paper title A" (0.76 similar to portal study "Existing Study X")
   ```

---

## Step 5 — Score Relevance

Write and run `/tmp/nf_agent/score.py`:

1. Score all groups in the `new` and `add` lists from dedup_results.json
2. For groups with a PMID, use the PubMed abstract (already fetched in Step 2) — this is the richest scoring input
3. Call `claude-sonnet-4-6` with the publication-level scoring prompt from CLAUDE.md
4. Apply filters: score ≥ 0.70, is_primary_data = true, sample_count ≥ 3 (if known)
5. Save approved + rejected groups to `/tmp/nf_agent/scored.json`
6. Print each result:
   ```
   [NEW][APPROVED]  "Pembrolizumab in MPNSTs" (PMID:41760889) — 0.95 — 2 datasets: GEO:GSE301187, SRA:SRP123
   [ADD][APPROVED]  "NF2 Schwann cell proteomics" (PMID:41234567) — 0.88 — adding PRIDE:PXD012345 to syn12345
   [NEW][REJECTED]  "KRAS plasma biomarkers" (no PMID) — 0.05 — low relevance
   ```

---

## Step 6 — Create / Update Synapse Projects

Write and run `/tmp/nf_agent/synapse_actions.py`:

For each approved group (max 50 write operations total):

**For NEW groups:**
1. Create Synapse project named `suggested_project_name` from Claude scoring
2. Folder hierarchy: `Raw Data/`, `Analysis/`, `Source Metadata/`
3. For each dataset in the group:
   a. Create `{Repository}_{AccessionID}/` subfolder in `Raw Data/`
   b. Enumerate individual file download URLs from the source repository (see CLAUDE.md "How to Get Direct Download URLs Per Repository")
   c. If ≤ 100 files and direct URLs available: create one `File` entity per file with `externalURL=<direct_download_url>`, `synapseStore=False`
   d. If > 100 files or controlled access: create one `ExternalLink` to the landing page
   e. Apply dataset-folder-level annotations (contentType=dataset, externalAccessionID, assay, species, etc.)
   f. Set provenance on each File/Link entity
4. Apply project-level annotations (study, resourceType, resourceStatus=pendingReview, pmid, doi)
5. Create wiki page using CLAUDE.md template — include the full datasets table

**For ADD groups:**
- Agent-created project: add new dataset subfolder to its `Raw Data/` folder
- Portal-managed project: create [Manual] JIRA ticket, skip write

Save `/tmp/nf_agent/created_projects.json`.
Print each action: `Created: "Project Name" (synXXX) — N datasets, M files`

---

## Step 7 — JIRA Notifications

Write and run `/tmp/nf_agent/notify.py`. Attempt ticket creation; log 401/placeholder errors as warnings and continue.

---

## Step 8 — Update State Tables

Write and run `/tmp/nf_agent/update_state.py`. Record every accession evaluated. Append run summary row. Print:

```
=== NF Data Contributor Agent — Run Complete ===
Date: {{TODAY}}
Publications scanned (PubMed): N
Publications with data: N
Secondary datasets (no paper): N
Publication groups total: N
Dedup — new: N | add: N | skip: N | near-matches: N
After scoring: N approved
Synapse projects created: N
Datasets added to existing: N
Errors: N
================================================
```

---

## Error Handling

- On any step failure, log the error, continue to the next step where safe
- Always run Step 8 regardless of earlier failures
- If Europe PMC returns nothing for a PMID (paper not in open-access PMC), continue — that's expected
- If NCBI rate-limits you, wait 1 second between elink batches (use `time.sleep(1)`)
