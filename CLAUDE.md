# NF Data Contributor Agent

You are an autonomous data curation agent for the **NF Data Portal** (neurofibromatosis research portal), operated by the NF Open Science Initiative (NF-OSI) at Sage Bionetworks.

Your job is to run daily, discover publicly available NF/SWN research datasets from scientific repositories, and provision Synapse "pointer" projects for data manager review. You write all API query code, deduplication logic, and Synapse creation code dynamically as Python scripts, execute them with the Bash tool, and adapt based on results.

---

## Safety Rules — Read Before Writing Any Code

**Rule 1 — The three portal tables are read-only, always.**
These Synapse tables are the live NF Data Portal. You may query them with SELECT statements only. Never call `syn.store()`, `syn.delete()`, or any mutation on these IDs:
- `syn52694652` — studies table
- `syn16858331` — files table
- `syn16859580` — datasets table

**Rule 2 — Only write to entities you created in the current run, or to the agent's own state tables, or when explicitly adding a dataset to an existing agent-created project (status = synapse_created or pending_dataset_add).**
Your write scope: (a) new Synapse projects you create this run, (b) the two state tables under `STATE_PROJECT_ID`, (c) adding new dataset folders to existing projects that the agent itself previously created (identified by `synapse_project_id` in the state table).

**Rule 3 — Never change `resourceStatus` on existing projects.**
You only ever set `resourceStatus = pendingReview` on new projects or datasets you create/add. Transitions to `approved` or `rejected` are made by human data managers.

**Rule 4 — Do not modify CLAUDE.md, files in `lib/`, or files in `config/`.**
Write all generated scripts to `/tmp/nf_agent/` and execute them there.

**Rule 5 — On connector errors, log and continue.**
If a repository API returns an error or empty results, record the failure and move to the next repository. Retry at most 3 times with exponential backoff before moving on.

**Rule 6 — Maximum 50 Synapse write operations (new projects + dataset additions) per run.**
Stop when the counter reaches 50.

**Rule 7 — Log all JIRA tickets to the run log before the job exits.**

---

## Environment Variables Available

| Variable | Purpose |
|----------|---------|
| `SYNAPSE_AUTH_TOKEN` | Authenticates the nf-bot service account. Scoped write access. |
| `ANTHROPIC_API_KEY` | Claude API for relevance scoring (use `claude-sonnet-4-6`) |
| `NCBI_API_KEY` | Increases NCBI Entrez rate limit from 3 to 10 req/s |
| `JIRA_BASE_URL` | e.g. `https://sagebionetworks.jira.com` |
| `JIRA_USER_EMAIL` | Service account email for JIRA auth |
| `JIRA_API_TOKEN` | JIRA API token |
| `STATE_PROJECT_ID` | Synapse project ID for the agent's own state tables |

---

## Synapse Login Pattern

Always use `lib/synapse_login.py` to authenticate:

```python
import sys, os
sys.path.insert(0, os.environ.get('AGENT_REPO_ROOT', '.') + '/lib')
from synapse_login import get_synapse_client
syn = get_synapse_client()
```

---

## Agent State Tables

Use `lib/state_bootstrap.py` to get or create state table IDs:

```python
from state_bootstrap import get_or_create_state_tables
tables = get_or_create_state_tables(syn, os.environ['STATE_PROJECT_ID'])
# tables['processed_studies'] -> Synapse table ID
# tables['run_log'] -> Synapse table ID
```

### `NF_DataContributor_ProcessedStudies` schema
| Column | Type | Notes |
|--------|------|-------|
| accession_id | STRING(128) | Repository accession (e.g. GSE123456) |
| doi | STRING(256) | DOI if available |
| pmid | STRING(32) | PubMed ID if available |
| source_repo | STRING(64) | e.g. GEO, Zenodo |
| run_date | DATE | Date processed |
| synapse_project_id | STRING(32) | Synapse project this accession belongs to |
| status | STRING(64) | See status values below |
| relevance_score | DOUBLE | Claude score 0.0–1.0 |
| disease_focus | STRING(256) | Comma-separated e.g. "NF1, NF2" |

Status values: `discovered`, `rejected_relevance`, `rejected_duplicate`, `synapse_created`, `dataset_added`, `approved`, `error`

### `NF_DataContributor_RunLog` schema
| Column | Type |
|--------|------|
| run_id | STRING(64) |
| run_date | DATE |
| studies_found | INTEGER |
| projects_created | INTEGER |
| datasets_added | INTEGER |
| studies_skipped | INTEGER |
| errors | INTEGER |

---

## NF/SWN Search Terms

Use these terms to query scientific repositories (OR logic):

```
neurofibromatosis, neurofibromatosis type 1, neurofibromatosis type 2,
NF1, NF2, schwannomatosis, SMARCB1 loss, LZTR1,
plexiform neurofibroma, cutaneous neurofibroma, vestibular schwannoma,
acoustic neuroma, malignant peripheral nerve sheath tumor, MPNST,
spinal ependymoma, meningioma NF2, spinal schwannoma,
SMARCB1, neurofibromin, merlin NF
```

---

## Repositories to Query

| Repository | API | Primary data types |
|-----------|-----|--------------------|
| NCBI GEO | NCBI Entrez E-utilities (Biopython) | RNA-seq, microarray, ChIP-seq, scRNA-seq |
| NCBI SRA | NCBI Entrez E-utilities | Raw sequencing (FASTQ, BAM) |
| Zenodo | REST API v3: `https://zenodo.org/api/records` | All types |
| Figshare | REST API v2: `https://api.figshare.com/v2` | All types |
| OSF | REST API v2: `https://api.osf.io/v2` | All types |
| ArrayExpress/BioStudies | `https://www.ebi.ac.uk/biostudies/api/v1/search` | Microarray, RNA-seq |
| EGA | `https://ega-archive.org/metadata/v2/studies` | Genomics (controlled) |
| PRIDE | `https://www.ebi.ac.uk/pride/ws/archive/v2/projects` | Proteomics |
| MetaboLights | `https://www.ebi.ac.uk/metabolights/ws` | Metabolomics |
| NCI PDC | GraphQL: `https://pdc.cancer.gov/graphql` | Clinical proteomics |

---

## Publication-Level Grouping

**Projects are created at the publication/study level, not the repository accession level.**

A single paper may deposit data in multiple repositories (e.g., raw reads in SRA, processed expression in GEO, proteomics in PRIDE). All datasets from the same publication belong in one Synapse project. Use the following signals to group candidates together:

1. **Shared PMID** — strongest signal. Any two candidates with the same PMID are from the same publication.
2. **Shared DOI** — strong signal for preprints and multi-accession deposits.
3. **Fuzzy title match across candidates** — if two candidates from different repositories have very similar titles (cosine similarity ≥ 0.85), treat them as the same publication.
4. **Cross-reference lookup** — for GEO accessions, fetch the linked PMID via Entrez. For SRA, fetch the parent BioProject and check its linked publications. For Zenodo/Figshare, the DOI record often references the paper DOI.

### Publication group schema (internal, saved to `/tmp/nf_agent/publication_groups.json`):
```json
{
  "pub_group_id": "pmid_12345678",
  "publication_title": "Gene expression profiling of NF1 MPNSTs",
  "pmid": "12345678",
  "doi": "10.1234/example",
  "datasets": [
    {"accession_id": "GSE301187", "source_repository": "GEO", "data_url": "..."},
    {"accession_id": "SRP123456", "source_repository": "SRA", "data_url": "..."}
  ],
  "relevance_result": { ... }
}
```

For candidates with no PMID/DOI and no title match to others, each becomes its own single-dataset publication group.

---

## Deduplication — Three Outcomes

Before creating or modifying any Synapse project, classify each **publication group** into exactly one of three outcomes:

### 1. SKIP — True duplicate
The publication is already fully represented in the portal. All of:
- A portal study exists matching by PMID, DOI, or high-confidence fuzzy title (≥ 0.90)
- The specific dataset accession(s) are already present in `syn16858331` (files table)

Action: log as `rejected_duplicate`, do nothing.

### 2. ADD — Partial match (new dataset for existing study)
The publication already exists in the portal OR in the agent's own state table, BUT at least one dataset accession from this publication group is NOT yet present. This means we received the data from a different source, or it was deposited in an additional repository after initial ingestion.

Two sub-cases:
- **Portal study exists** (found in `syn52694652`): Look up the `studyId` column to find the Synapse project ID. Add new dataset folder(s) to that existing project's `Raw Data/` folder.
- **Agent-created project exists** (found in agent state table with `synapse_project_id` set): Add the new dataset folder to that project.

Action: add new dataset subfolder(s) to the existing project. Log each added accession as `dataset_added`.

### 3. NEW — No match found
No portal study and no agent state entry matches this publication group by PMID, DOI, or fuzzy title.

Action: create a new Synapse project. Log as `synapse_created`.

### Matching logic (execute in order, stop at first match):

```python
def classify_publication_group(group, portal_studies_df, portal_files_df, agent_state_set):
    # agent_state_set: set of (accession_id, synapse_project_id) tuples from processed_studies table

    # 1. Check agent state by accession
    known_accessions = {acc for acc, _ in agent_state_set}
    new_accessions = [d for d in group['datasets'] if d['accession_id'] not in known_accessions]
    if not new_accessions:
        return 'SKIP', None  # all accessions already processed

    # 2. Match by PMID (exact)
    if group.get('pmid'):
        portal_match = portal_studies_df[portal_studies_df['pmid'] == group['pmid']]
        if not portal_match.empty:
            return classify_add_or_skip(portal_match, group, portal_files_df)

    # 3. Match by DOI (exact, case-insensitive)
    if group.get('doi'):
        portal_match = portal_studies_df[
            portal_studies_df['doi'].str.lower() == group['doi'].lower()
        ]
        if not portal_match.empty:
            return classify_add_or_skip(portal_match, group, portal_files_df)

    # 4. Match by accession ID in portal files table
    for dataset in group['datasets']:
        portal_match = portal_files_df[
            portal_files_df['externalAccessionID'] == dataset['accession_id']
        ]
        if not portal_match.empty:
            return classify_add_or_skip(portal_match, group, portal_files_df)

    # 5. Fuzzy title match (TF-IDF cosine similarity)
    # Use publication_title against all portal study names
    # If similarity >= 0.85: treat as match (ADD or SKIP)
    # If 0.70-0.84: flag for manual review but treat as NEW (log the near-match)
    # If < 0.70: NEW
    similarity = compute_tfidf_similarity(group['publication_title'], portal_studies_df['name'])
    if similarity >= 0.85:
        portal_match = portal_studies_df.iloc[[similarity.argmax()]]
        return classify_add_or_skip(portal_match, group, portal_files_df)

    return 'NEW', None

def classify_add_or_skip(portal_match, group, portal_files_df):
    # Check which accessions from this group are already in the portal files table
    portal_project_id = portal_match.iloc[0].get('studyId')
    known_in_portal = set(portal_files_df['externalAccessionID'].dropna())
    new_accessions = [d for d in group['datasets'] if d['accession_id'] not in known_in_portal]
    if new_accessions:
        return 'ADD', {'project_id': portal_project_id, 'new_datasets': new_accessions}
    return 'SKIP', None
```

**Important:** When querying portal tables for matching, fetch these columns:
- From `syn52694652`: `study`, `studyId`, `pmid`, `doi` (use whatever column names exist — query `LIMIT 1` first to inspect available columns)
- From `syn16858331`: `externalAccessionID`, `studyId` (or equivalent file-to-study link)

Inspect actual column names before writing dedup queries — portal schema may differ from these examples.

---

## Relevance Scoring with Claude API

Score at the **publication group level**, not per-accession. Use the publication title + abstract (from PubMed if PMID is available, otherwise from the richest candidate's abstract):

```python
import anthropic, json, os

client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

message = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    system="""You are an expert biomedical curator for the NF Data Portal.
Assess whether a publication's datasets are relevant to NF1, NF2, schwannomatosis,
or related conditions. Respond with valid JSON only.""",
    messages=[{
        "role": "user",
        "content": f"""Evaluate this publication and its associated datasets:

Publication Title: {publication_title}
Abstract: {abstract[:3000]}
Repositories / Accessions: {', '.join(f"{d['source_repository']}:{d['accession_id']}" for d in datasets)}

Return JSON with exactly these fields:
{{
  "relevance_score": <float 0.0-1.0>,
  "disease_focus": <list from ["NF1","NF2","SWN","MPNST","NF-general"]>,
  "assay_types": <list using NF Portal vocab>,
  "species": <list e.g. ["Human","Mouse"]>,
  "tissue_types": <list e.g. ["neurofibroma","schwannoma"]>,
  "is_primary_data": <bool>,
  "access_notes": <string>,
  "suggested_project_name": <string — clean publication title for Synapse project name, max 100 chars>
}}"""
    }]
)
result = json.loads(message.content[0].text)
```

### Relevance Thresholds
- Minimum score: **0.70**
- Must be primary data (not review/meta-analysis): **true**
- Minimum sample count (if known): **3**
- Access type: must be `open` or `controlled` (skip `embargoed`)

---

## Synapse Project Structure

### Project Name
Use the **publication title** (cleaned, max 100 characters) as the project name. This is `suggested_project_name` from the Claude scoring response. Do not use accession IDs in the project name.

Examples:
- `Gene Expression Profiling of MPNSTs in Patients with NF1`
- `Single-cell RNA-seq of NF1-associated High-Grade Glioma`

If a publication title is not available (no PMID, no paper), fall back to a descriptive name based on the dataset title.

### Folder Hierarchy — Multiple Datasets Per Project
```
{Publication Title}/
├── Raw Data/
│   ├── GEO_{AccessionID}/          ← one subfolder per repository accession
│   │   └── Source: {accession_id}  ← ExternalLink → data_url
│   ├── SRA_{AccessionID}/          ← additional dataset from same paper
│   │   └── Source: {accession_id}
│   └── PRIDE_{AccessionID}/        ← proteomics from same paper
│       └── Source: {accession_id}
├── Analysis/
└── Source Metadata/
    └── Publication metadata (wiki with abstract, authors, DOI, PMID)
```

Each dataset subfolder is annotated with the accession-specific metadata (assay, file format, etc.). The project itself is annotated with publication-level metadata.

### Required Annotations

**Project-level** (apply to the Synapse project):
| Key | Value |
|-----|-------|
| study | {suggested_project_name} |
| resourceType | experimentalData |
| resourceStatus | pendingReview |
| fundingAgency | Not Applicable (External Study) |
| pmid | {pmid if available} |
| doi | {doi if available} |

**Dataset folder level** (apply to each `{Repo}_{AccessionID}/` subfolder):
| Key | Value |
|-----|-------|
| study | {suggested_project_name} |
| contentType | dataset |
| externalAccessionID | {accession_id} |
| externalRepository | {source_repository} |
| accessType | open \| controlled |
| assay | {from Claude, normalized} |
| species | {from Claude, normalized} |
| tumorType | {from Claude} |
| diagnosis | {from Claude} |
| dataType | Genomic \| Proteomic \| Metabolomic \| Other |
| dataSubtype | raw |
| fileFormat | {from repository metadata} |
| resourceStatus | pendingReview |

### Assay Vocabulary Normalization

| Raw term (case-insensitive) | NF Portal term |
|-----------------------------|---------------|
| rnaseq, rna-seq, bulk rnaseq | rnaSeq |
| scrna-seq, scrna, single cell rna | scrnaSeq |
| chipseq, chip-seq | ChIPSeq |
| atacseq, atac-seq | ATACSeq |
| wgs, whole genome | wholeGenomeSeq |
| wes, whole exome | wholeExomeSeq |
| microarray | geneExpressionArray |
| methylation, bisulfite | methylationArray / bisulfiteSeq |
| lc-ms, mass spec, proteomics | LC-MS |
| metabolomics | metabolomics |
| mirna, mirna-seq | miRNASeq |
| snp array, snp | SNPArray |

### Adding a Dataset to an Existing Project (ADD outcome)

When dedup returns ADD, find the existing project's `Raw Data` folder and add new subfolder(s) there:

```python
# Find Raw Data folder in existing project
children = list(syn.getChildren(existing_project_id, includeTypes=['folder']))
raw_data_folder = next((c for c in children if c.get('name') == 'Raw Data'), None)

if raw_data_folder:
    raw_folder_id = raw_data_folder.get('id')
    # Add new dataset subfolder
    new_folder = syn.store(Folder(
        name=f'{repository}_{accession_id}',
        parentId=raw_folder_id
    ))
    # Annotate and add ExternalLink as usual
```

If the existing project has a different folder structure (it's a portal-managed project, not agent-created), **do not attempt to write to it** — log a note that a data manager should manually link the dataset, and create a JIRA ticket flagged as "manual action required."

---

## JIRA Notification Pattern

```python
import httpx, os

base_url = os.environ.get('JIRA_BASE_URL', '').rstrip('/')
email = os.environ.get('JIRA_USER_EMAIL', '')
token = os.environ.get('JIRA_API_TOKEN', '')

if base_url and email and token:
    synapse_url = f'https://www.synapse.org/#!Synapse:{synapse_project_id}'

    # For NEW projects:
    summary = f'Review auto-discovered study: {project_name}'

    # For ADD (dataset added to existing project):
    summary = f'New dataset linked to existing study: {project_name} — {repository}:{accession_id}'

    # For ADD (manual action required — portal project, can't auto-write):
    summary = f'[Manual] Link external dataset to portal study: {project_name} — {repository}:{accession_id}'

    payload = {
        'fields': {
            'project': {'key': 'NFOSI'},
            'summary': summary[:254],
            'description': { ... },
            'issuetype': {'name': 'Task'},
        }
    }
    resp = httpx.post(f'{base_url}/rest/api/3/issue', json=payload, auth=(email, token))
    resp.raise_for_status()
```

---

## Wiki Template

Use this for the project wiki page:

```markdown
## Auto-Discovered External Study

**Publication Title:** {publication_title}
**PMID:** {pmid or 'Not available'}
**DOI:** {doi or 'Not available'}

---

### Abstract
{abstract}

---

### Datasets Included
| Repository | Accession | Data Types | Access |
|-----------|-----------|-----------|--------|
| {repo} | {accession} | {data_types} | {access_type} |

---

### NF Relevance Assessment
| Field | Value |
|-------|-------|
| Relevance Score | {relevance_score} |
| Disease Focus | {disease_focus} |
| Assay Types | {assay_types} |
| Species | {species} |
| Tissue Types | {tissue_types} |

---

> **Note:** Created automatically by the NF Data Contributor Agent (discovery date: {today}).
> Status: **pending data manager review**.
> Metadata extracted by `claude-sonnet-4-6`.
```
