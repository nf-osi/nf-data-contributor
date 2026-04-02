# Data Contributor Agent — Daily Run

**Today's date:** {{TODAY}}
**Search for publications/datasets since:** {{LOOKBACK_DATE}}
**Seed ID (direct processing):** {{SEED_ID}}

---

## Your Task

> **Seed ID:** `{{SEED_ID}}`
>
> - If the Seed ID above is **empty or blank**: run the full discovery pipeline (Steps 2–9).
> - If the Seed ID is a **PMID** (all digits, e.g. `12345678`): skip Steps 2–3. Write and run `{WORKSPACE_DIR}/seed_lookup.py` — fetch the PubMed record, resolve linked datasets via elink + Europe PMC + DataBankList, build `publication_groups.json`, then jump to Step 4.
> - If the Seed ID is a **repository accession** (e.g. `GSE123456`, `PXD012345`, `PRJNA123456`, `E-MTAB-1234`): skip Steps 2–3. Write and run `{WORKSPACE_DIR}/seed_lookup.py` — fetch metadata from the appropriate repository API, look up the associated PMID/DOI if available, build `publication_groups.json`, then jump to Step 4.

Write Python scripts to the workspace directory (`agent.workspace_dir` from `config/settings.yaml`) and execute them. Refer to CLAUDE.md for all API patterns, auth, annotation schemas, and safety rules.

---

## Step 1 — Setup

```
pip install synapseclient httpx biopython pyyaml scikit-learn anthropic --quiet
```

Read `config/settings.yaml` to get `WORKSPACE_DIR = agent.workspace_dir` and `STATE_TABLE_PREFIX = agent.state_table_prefix`, then:

```
mkdir -p {WORKSPACE_DIR}
```

Write and run `{WORKSPACE_DIR}/setup.py`:
1. Read `config/settings.yaml` for all runtime config (workspace_dir, state_table_prefix, state_project_name, etc.)
2. Authenticate with Synapse via `lib/synapse_login.py` — print the logged-in username
3. Get or create state tables via `lib/state_bootstrap.py` passing `table_prefix` from config. If `STATE_PROJECT_ID` is empty, create a Synapse project named per `agent.state_project_name` in config and use it.
4. Load all previously processed accession IDs into a Python set
5. Save table IDs, accession set, and state project ID to `{WORKSPACE_DIR}/state.json`
6. Print: "Setup complete. Previously processed: N accessions"

---

## Step 2 — PRIMARY DISCOVERY: PubMed + elink + Europe PMC

Write and run `{WORKSPACE_DIR}/discover_primary.py`.

This is the main discovery path. The goal is: find all domain-relevant publications from the lookback window (using the PubMed MeSH query from `config/keywords.yaml`), then systematically resolve every data deposit associated with each paper.

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

Write and run `{WORKSPACE_DIR}/discover_secondary.py`.

Query these repositories with keywords from `config/keywords.yaml` (`search_terms`) for datasets published since `{{LOOKBACK_DATE}}`. For each result, check if it has a PMID or DOI that was already found in the primary path — if so, skip it (it's already covered). Only keep datasets with no associated publication yet.

Repositories to query:
- Zenodo (`https://zenodo.org/api/records`) — search `resource_type.type:dataset`
- Figshare (`https://api.figshare.com/v2/articles/search`) — `item_type=3`
- OSF (`https://api.osf.io/v2/nodes/`) — public projects
- ArrayExpress/BioStudies (`https://www.ebi.ac.uk/biostudies/api/v1/search`)
- PRIDE (`https://www.ebi.ac.uk/pride/ws/archive/v2/projects`)
- MetaboLights (`https://www.ebi.ac.uk/metabolights/ws`)
- NCI PDC (GraphQL — filter for disease types from `config/keywords.yaml`)
- **DataCite API** (`https://api.datacite.org/dois?query={term}&resource-type-id=dataset&page[size]=50`) — catches institutional and national repos not otherwise queryable (Science Data Bank, TIB, IFJ PAN, CORA, Dryad, University institutional repos, etc.). Filter `S-EPMC*` accessions as always.
- **MassIVE** (`https://massive.ucsd.edu/ProteoSAFe/QueryDatasets?query={term}`) — proteomics datasets not already found via PRIDE
- **NCI GDC** (`https://api.gdc.cancer.gov/cases?filters=...`) — filter by disease type and primary site from `config/keywords.yaml`
- **Cell Image Library** (`https://cellimagelibrary.org/api/search?term={term}`) — microscopy image datasets

For unpublished results, create publication groups with `pmid: null`, using the repository title as the publication title.

Print: `Secondary discovery: N additional datasets (no associated publication)`

---

## Step 4 — Deduplicate Against Portal

Write and run `{WORKSPACE_DIR}/dedup.py`:

1. Load all publication groups from Steps 2 and 3
2. Remove any group whose accession_ids are all already in the processed accessions set from state.json
3. Inspect portal schema: `SELECT * FROM syn52694652 LIMIT 5` and `SELECT * FROM syn16858331 LIMIT 5` — print actual column names before writing any queries
4. Classify each remaining group as NEW, ADD, or SKIP using the three-outcome logic in CLAUDE.md:
   - **PMID match** (exact) → strongest signal for ADD or SKIP
   - **DOI match** (case-insensitive)
   - **Accession match** in portal files table
   - **Fuzzy title** (TF-IDF cosine ≥ 0.85 = match; 0.70–0.84 = near-match warning, treat as NEW)
5. Save to `{WORKSPACE_DIR}/dedup_results.json`
6. Print:
   ```
   Dedup: N new | M add-to-existing | K skip | J near-match warnings
   Near-matches:
     "Paper title A" (0.76 similar to portal study "Existing Study X")
   ```

---

## Step 5 — Score Relevance

Write and run `{WORKSPACE_DIR}/score.py`:

1. Score all groups in the `new` and `add` lists from dedup_results.json
2. For groups with a PMID, use the PubMed abstract (already fetched in Step 2) — this is the richest scoring input
3. Call `claude-sonnet-4-6` with the publication-level scoring prompt from CLAUDE.md
4. Apply filters: score ≥ 0.70, is_primary_data = true, sample_count ≥ 3 (if known)
5. Save approved + rejected groups to `{WORKSPACE_DIR}/scored.json`
6. Print each result:
   ```
   [NEW][APPROVED]  "Pembrolizumab in MPNSTs" (PMID:41760889) — 0.95 — 2 datasets: GEO:GSE301187, SRA:SRP123
   [ADD][APPROVED]  "NF2 Schwann cell proteomics" (PMID:41234567) — 0.88 — adding PRIDE:PXD012345 to syn12345
   [NEW][REJECTED]  "KRAS plasma biomarkers" (no PMID) — 0.05 — low relevance
   ```

---

## Step 6 — Create / Update Synapse Projects

Write and run `{WORKSPACE_DIR}/synapse_actions.py`:

For each approved group (max 50 write operations total):

**For NEW groups:**
1. Create Synapse project named `suggested_project_name` from Claude scoring
2. Folder hierarchy: `Raw Data/`, `Source Metadata/`
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

Save `{WORKSPACE_DIR}/created_projects.json` with the full schema defined in `prompts/synapse_workflow.md` (project_id, project_name, pmid, doi, abstract, outcome, datasets[]).
Print each action: `Created: "Project Name" (synXXX) — N datasets, M files`

---

## Step 7 — Self-Audit and Remediation

**Read `prompts/synapse_workflow.md` for the full implementation of all three audit phases.**

This step checks every project created in Step 6 against the completion checklist and fixes any issues found. Run it in three sub-steps:

### 7a — Write and run `{WORKSPACE_DIR}/audit.py` (Phase 1)

The audit script (code in `prompts/synapse_workflow.md`):
- Fetches the current state of every project, dataset, and file entity created this run
- **Auto-fixes** all mechanical issues immediately (no reasoning required):
  - `studyStatus` wrong value → `Completed`
  - `dataStatus` missing → `Available`
  - `resourceStatus` missing → `pendingReview`
  - `studyName` missing → set from project name
  - `fundingAgency` missing → `Not Applicable (External Study)`
  - `pmid`/`doi` missing but known → set from project metadata
  - Data manager team permissions (`synapse.team_id` from config) missing → grant
  - Dataset `items` empty → re-link from files folder
  - Dataset `columnIds` missing → create columns
  - Dataset entity annotations missing → set defaults
  - `fileFormat` with compression suffix (`.gz`) → strip to bare extension
  - `resourceType` missing → `experimentalData`
  - `externalAccessionID`/`externalRepository`/`study` missing → set from known metadata
  - `dataSubtype` missing → infer from file extension (`raw` for fastq/bam/vcf, `processed` otherwise)
  - `specimenID`/`individualID` parseable from filename (GSM/SRR/ERR prefix) → set
  - Schema binding missing → bind the schema
- **Collects context** for issues that require reasoning (annotation fields that need domain knowledge)
- Prints a structured report and writes `{WORKSPACE_DIR}/audit_results.json`

### 7b — Agent reasoning (Phase 2)

After running `audit.py`, read `{WORKSPACE_DIR}/audit_results.json`. For each project with `reasoning_gaps`:

1. Read the available context: abstract (stored in audit_results), project annotations, wiki
2. If PMID is known and abstract is missing, fetch it from PubMed
3. Reason through each gap:
   - `diseaseFocus`, `manifestation` → infer from disease mentions in title + abstract
   - `dataType` → infer from assay type
   - `studyLeads` → fetch first + last author from PubMed AuthorList if PMID available; otherwise infer from abstract
   - `institutions` → from author affiliations in PubMed record or abstract
   - `alternateDataRepository` → reconstruct from accession_id + REPO_TO_PREFIX
   - `assay`, `species`, `tumorType`, `diagnosis` → infer from abstract + experimental description
   - `platform` → fetch from repository (GEO series platform field, SRA instrument_model in runinfo)
   - `libraryPreparationMethod` → look for kit/method names in abstract ("10x Chromium", "Smart-seq2", "polyA")
   - `specimenID` where auto-parse failed → look at repository sample metadata (GEO GSM table, SRA BioSample)
   - `wiki` missing → create from wiki template (in `prompts/synapse_workflow.md`) using available metadata
4. Write `{WORKSPACE_DIR}/audit_reasoning_fixes.json` with all resolved values

### 7c — Write and run `{WORKSPACE_DIR}/apply_audit_fixes.py` (Phase 3)

The apply script (code in `prompts/synapse_workflow.md`):
- Reads `audit_reasoning_fixes.json`
- Applies all project annotation fixes via `/entity/{id}/annotations2`
- Creates missing wikis
- Updates file annotations with the reasoned values
- Prints a final summary

After Phase 3, print the complete audit report:
```
=== Self-Audit Report ===
Projects audited:   N
Auto-fixes applied: N
Reasoning fixes:    N
Warnings remaining: N
========================
```

---

## Step 8 — JIRA Notifications

Write and run `{WORKSPACE_DIR}/notify.py`. Attempt ticket creation; log 401/placeholder errors as warnings and continue.

---

## Step 9 — Update State Tables

Write and run `{WORKSPACE_DIR}/update_state.py`. Record every accession evaluated. Append run summary row. Print:

```
=== NADIA — Run Complete ===
Date: {{TODAY}}
Publications scanned (PubMed): N
Publications with data: N
Secondary datasets (no paper): N
Publication groups total: N
Dedup — new: N | add: N | skip: N | near-matches: N
After scoring: N approved
Synapse projects created: N
Datasets added to existing: N
Audit auto-fixes: N
Audit reasoning fixes: N
Errors: N
================================================
```

---

## Error Handling

- On any step failure, log the error, continue to the next step where safe
- Always run Step 9 regardless of earlier failures
- If Europe PMC returns nothing for a PMID (paper not in open-access PMC), continue — that's expected
- If NCBI rate-limits you, wait 1 second between elink batches (use `time.sleep(1)`)
- If audit Phase 1 fails for a project, log the error and continue to the next project — do not abort the whole audit
