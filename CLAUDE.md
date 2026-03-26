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

**Rule 2 — Only write to entities you created in the current run, or to the agent's own state tables.**
Your write scope: (a) new Synapse projects with `EXT_` prefix names that you create this run, and (b) the two state tables under `STATE_PROJECT_ID`. Any other Synapse entity ID in a `syn.store()` call is a bug.

**Rule 3 — Never change `resourceStatus` on existing projects.**
You only ever set `resourceStatus = pendingReview` on new projects you create. Transitions to `approved` or `rejected` are made by human data managers.

**Rule 4 — Do not modify CLAUDE.md, files in `lib/`, or files in `config/`.**
These are stable infrastructure. Write all generated scripts to `/tmp/nf_agent/` and execute them there.

**Rule 5 — On connector errors, log and continue.**
If a repository API returns an error or empty results, record the failure and move to the next repository. Do not abort the entire run. Retry at most 3 times with exponential backoff before moving on.

**Rule 6 — Maximum 50 new Synapse projects per run.**
Stop creating projects if the counter reaches 50. Log a warning and finish the run normally.

**Rule 7 — Log all JIRA tickets to the run log before the job exits.**

---

## Environment Variables Available

| Variable | Purpose |
|----------|---------|
| `SYNAPSE_AUTH_TOKEN` | Authenticates the nf-bot service account. Scoped write access — can only write to projects it created. |
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
import sys
sys.path.insert(0, '/path/to/repo/lib')
from synapse_login import get_synapse_client
syn = get_synapse_client()
```

---

## Agent State Tables

These tables live in the `STATE_PROJECT_ID` Synapse project. Use `lib/state_bootstrap.py` to get their IDs (creates them on first run):

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
| synapse_project_id | STRING(32) | Set when project created |
| status | STRING(64) | See status values below |
| relevance_score | DOUBLE | Claude score 0.0–1.0 |
| disease_focus | STRING(256) | Comma-separated e.g. "NF1, NF2" |

Status values: `discovered`, `rejected_relevance`, `rejected_duplicate`, `synapse_created`, `approved`, `error`

### `NF_DataContributor_RunLog` schema
| Column | Type |
|--------|------|
| run_id | STRING(64) |
| run_date | DATE |
| studies_found | INTEGER |
| studies_created | INTEGER |
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

## Relevance Scoring with Claude API

For each candidate, call `claude-sonnet-4-6` with this prompt structure. Expect a JSON response:

```python
import anthropic, json, os

client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

message = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    system="""You are an expert biomedical curator for the NF Data Portal.
Assess whether a dataset is relevant to NF1, NF2, schwannomatosis, or related
conditions. Respond with valid JSON only.""",
    messages=[{
        "role": "user",
        "content": f"""Evaluate this dataset:

Title: {title}
Abstract: {abstract[:3000]}
Repository: {repository}
Accession: {accession_id}

Return JSON with exactly these fields:
{{
  "relevance_score": <float 0.0-1.0>,
  "disease_focus": <list from ["NF1","NF2","SWN","MPNST","NF-general"]>,
  "assay_types": <list using NF Portal vocab>,
  "species": <list e.g. ["Human","Mouse"]>,
  "tissue_types": <list e.g. ["neurofibroma","schwannoma"]>,
  "is_primary_data": <bool>,
  "access_notes": <string>,
  "suggested_study_name": <string following NF Portal conventions>
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
```
EXT_{Repository}_{AccessionID}
```
Examples: `EXT_GEO_GSE123456`, `EXT_Zenodo_10.5281_zenodo.1234`

Replace `/` with `_` in accession IDs.

### Folder Hierarchy
```
EXT_{Repository}_{AccessionID}/
├── Raw Data/
│   └── {Repository}_{AccessionID}/    ← dataset subfolder (contentType=dataset)
│       └── Source: {accession_id}     ← ExternalLink to data_url
├── Analysis/
└── Source Metadata/
    └── original_metadata (wiki)
```

### Required Annotations (apply to project AND pointer entities)

| Key | Value |
|-----|-------|
| study | {suggested_study_name from Claude} |
| resourceType | experimentalData |
| resourceStatus | pendingReview |
| fundingAgency | Not Applicable (External Study) |
| accessType | open \| controlled |
| externalAccessionID | {accession_id} |
| externalRepository | {source_repository} |
| dataType | Genomic \| Proteomic \| Metabolomic \| Other |
| dataSubtype | raw |
| assay | {from Claude, normalized — see vocab below} |
| species | {from Claude, normalized} |
| tumorType | {from Claude} |
| diagnosis | {from Claude, e.g. "Neurofibromatosis 1"} |
| fileFormat | {from repository metadata} |
| contentType | dataset (on dataset folders) |

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

### Synapse Project Creation Pattern

```python
import synapseclient
from synapseclient import Project, Folder, Wiki, Activity

project = syn.store(Project(name=project_name))
project_id = project.id

# Annotate project
entity = syn.get(project_id)
entity.annotations.update(project_annotations)
syn.store(entity)

# Create folders
raw_folder = syn.store(Folder(name='Raw Data', parentId=project_id))
analysis_folder = syn.store(Folder(name='Analysis', parentId=project_id))
metadata_folder = syn.store(Folder(name='Source Metadata', parentId=project_id))

# Create dataset subfolder
dataset_folder = syn.store(Folder(
    name=f'{repository}_{accession_id}',
    parentId=raw_folder.id
))
dataset_folder_annotations = {'contentType': 'dataset', ...}

# ExternalLink for data URL
from synapseclient import Link
link = syn.store(Link(
    targetId=data_url,
    name=f'Source: {accession_id}',
    parentId=dataset_folder.id
))

# Provenance
activity = Activity(
    name='NF Data Contributor Agent — auto-discovery',
    description=f'Discovered from {repository}. Metadata extracted by claude-sonnet-4-6.',
    used=[data_url]
)
syn.setProvenance(link.id, activity)

# Wiki
wiki = Wiki(title='Study Overview', owner=project_id, markdown=wiki_content)
syn.store(wiki)
```

---

## JIRA Notification Pattern

For each new project created, open a JIRA ticket:

```python
import httpx, os

base_url = os.environ.get('JIRA_BASE_URL', '').rstrip('/')
email = os.environ.get('JIRA_USER_EMAIL', '')
token = os.environ.get('JIRA_API_TOKEN', '')

if base_url and email and token:
    synapse_url = f'https://www.synapse.org/#!Synapse:{synapse_project_id}'
    payload = {
        'fields': {
            'project': {'key': 'NFOSI'},
            'summary': f'Review auto-discovered study: {study_name} ({repository}:{accession_id})',
            'description': {
                'type': 'doc', 'version': 1,
                'content': [{'type': 'paragraph', 'content': [{'type': 'text',
                    'text': f'New external dataset pending review.\n\n'
                            f'Repository: {repository}\nAccession: {accession_id}\n'
                            f'Relevance: {relevance_score:.2f}\nDisease: {disease_focus}\n'
                            f'Synapse: {synapse_url}'}]}]
            },
            'issuetype': {'name': 'Task'},
        }
    }
    resp = httpx.post(
        f'{base_url}/rest/api/3/issue',
        json=payload,
        auth=(email, token)
    )
    resp.raise_for_status()
```

---

## Deduplication Logic

Before creating any project, check in this order (stop at first match):

1. **Own tracking table**: `SELECT accession_id FROM {processed_table_id}` — load all into a Python set at the start of the run. Skip any candidate whose accession is in this set.
2. **Portal study table accession**: `SELECT study FROM syn52694652` — check if accession_id appears.
3. **Portal DOI**: Query portal tables for matching DOI.
4. **Portal PMID**: Query portal tables for matching PMID.
5. **Fuzzy title match**: Use TF-IDF cosine similarity (scikit-learn) against all portal study titles. Threshold: 0.85. Flag for manual review rather than hard-reject.

---

## Wiki Template

Use this for the project wiki page (fill in all placeholders):

```markdown
## Auto-Discovered External Dataset

**Source Repository:** {repository}
**Accession ID:** {accession_id}
**Data URL:** {data_url}

---

### Abstract
{abstract}

---

### Dataset Details
| Field | Value |
|-------|-------|
| Data Types | {data_types} |
| Access Type | {access_type} |
| Sample Count | {sample_count} |
| Discovery Date | {today} |

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

> **Note:** Created automatically by the NF Data Contributor Agent.
> Status: **pending data manager review**.
> Metadata extracted by `claude-sonnet-4-6`.
```
