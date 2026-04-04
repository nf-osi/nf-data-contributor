"""
nadia_polish_prep.py — Prepare the project queue and prompt for a NADIA Polish run.

Queries the ProcessedStudies state table for projects that have been created but
not yet approved, ordered by curation priority (PMID present first, then by
run_date descending). Writes a prompt file for claude -p.

Called by nadia_polish.yml before invoking claude.
"""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.environ.get("AGENT_REPO_ROOT", "."), "lib"))
from synapse_login import get_synapse_client

import yaml


def get_project_queue(syn, table_id, limit=None):
    """
    Return ordered list of projects to curate.
    Priority: has PMID > no PMID, then most recently created first.
    Excludes already-approved projects.
    """
    query = (
        f"SELECT DISTINCT synapse_project_id, pmid, doi, run_date, disease_focus "
        f"FROM {table_id} "
        f"WHERE status IN ('synapse_created', 'dataset_added') "
        f"ORDER BY run_date DESC"
    )
    df = syn.tableQuery(query).asDataFrame()

    # Deduplicate by project (multiple accessions → one row per project)
    seen = {}
    for _, row in df.iterrows():
        pid = row["synapse_project_id"]
        if pid not in seen:
            seen[pid] = {
                "synapse_project_id": pid,
                "pmid": str(row.get("pmid") or "").strip(),
                "doi": str(row.get("doi") or "").strip(),
                "run_date": str(row.get("run_date") or ""),
                "disease_focus": str(row.get("disease_focus") or ""),
            }

    projects = list(seen.values())

    # Sort: PMID present first, then run_date descending
    projects.sort(key=lambda p: (0 if p["pmid"] and p["pmid"] != "nan" else 1, p["run_date"]), reverse=False)
    projects.sort(key=lambda p: 0 if (p["pmid"] and p["pmid"] != "nan") else 1)

    if limit:
        projects = projects[:limit]

    return projects


def main():
    workspace_dir = os.environ.get("NADIA_WORKSPACE_DIR", "/tmp/nf_agent")
    state_project_id = os.environ.get("STATE_PROJECT_ID", "")
    today = datetime.date.today().isoformat()
    limit = int(os.environ.get("PROJECT_LIMIT", "0")) or None  # 0 = no limit

    if not state_project_id:
        sys.exit("ERROR: STATE_PROJECT_ID not set")

    with open(os.path.join(os.environ.get("AGENT_REPO_ROOT", "."), "config", "settings.yaml")) as f:
        cfg = yaml.safe_load(f)

    prefix = cfg["agent"]["state_table_prefix"]

    print("Connecting to Synapse...")
    syn = get_synapse_client()

    # Find state table
    target_name = f"{prefix}_ProcessedStudies"
    table_id = None
    for item in syn.getChildren(state_project_id, includeTypes=["table"]):
        if item["name"] == target_name:
            table_id = item["id"]
            break
    if not table_id:
        sys.exit(f"ERROR: State table '{target_name}' not found in {state_project_id}")

    print(f"Querying state table {table_id}...")
    projects = get_project_queue(syn, table_id, limit=limit)
    print(f"  {len(projects)} projects queued for curation")

    os.makedirs(workspace_dir, exist_ok=True)

    # Write project queue JSON for agent reference
    queue_path = os.path.join(workspace_dir, "polish_queue.json")
    with open(queue_path, "w") as f:
        json.dump(projects, f, indent=2)

    project_list_md = "\n".join(
        f"- `{p['synapse_project_id']}`"
        + (f" PMID:{p['pmid']}" if p['pmid'] and p['pmid'] != 'nan' else " (no PMID)")
        + (f" DOI:{p['doi']}" if p['doi'] and p['doi'] != 'nan' else "")
        for p in projects
    )

    prompt = f"""\
# NADIA Polish — Deep Curation Run

**Date:** {today}
**Projects queued:** {len(projects)}
**Project queue:** `{queue_path}`

---

## Your Role

You are NADIA acting as a **relentlessly accurate data curator**. Your goal is to produce
the highest-quality annotation possible for each Synapse project — the kind of work a
careful human curator would be proud to sign off on.

**Depth over breadth.** If you thoroughly curate 8 projects rather than superficially
touching 80, that is the correct trade-off. A data manager should be able to approve
each project you finish without having to look anything up themselves.

---

## Projects to Curate

{project_list_md}

Work through these in order. Stop when you run out of turns — do not rush or skip steps
to fit more projects in.

---

## Curation Protocol — One Project at a Time

For each project, execute the following steps in full before moving to the next.

### Step 1 — Load project state

```python
import sys, os
sys.path.insert(0, os.environ.get('AGENT_REPO_ROOT', '.') + '/lib')
from synapse_login import get_synapse_client
syn = get_synapse_client()

project_id = 'syn...'
anns = dict(syn.get_annotations(project_id))
children = list(syn.getChildren(project_id))
```

Fetch:
- Current project-level annotations
- All child entities (Datasets, Raw Data folder, Source Metadata folder)
- For each `{Repo}_{Accession}_files/` folder: list all File entities and their current annotations

### Step 2 — Read the publication

If `pmid` is set, fetch the full PubMed record:
- Title, abstract, full author list with affiliations
- Grant list (GrantID + Agency for each)
- DataBankList (author-submitted accessions)
- Journal, year, DOI

Read the **abstract carefully**. You need to understand:
- What disease(s) are studied (not just NF1/NF2 in general — which specific manifestation?)
- What experimental approach was used (what assay, what model system)
- What tissue/tumor types were profiled
- What species
- What the study's main finding was (for the wiki summary)

### Step 3 — Fetch source repository metadata

For every accession in the project, pull the repository's own metadata:

**GEO (GSExxxxxx):**
```python
# Fetch GEO SOFT miniml for series-level metadata
import httpx
r = httpx.get(f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={gse_id}&targ=self&form=text&view=quick', timeout=30)
# Parse: !Series_platform_id, !Series_type, !Series_sample_taxid, !Series_overall_design
# Then fetch a sample record for platform details:
r2 = httpx.get(f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={gsm_id}&targ=self&form=text&view=quick', timeout=30)
# Parse: !Sample_instrument_model, !Sample_library_strategy, !Sample_library_source,
#        !Sample_source_name_ch1, !Sample_organism_ch1, !Sample_characteristics_ch1
```

**ENA / SRA (SRPxxxxxx, PRJNAxxxxxx, PRJEBxxxxxx):**
```python
# ENA portal API — get study + run metadata
r = httpx.get('https://www.ebi.ac.uk/ena/portal/api/filereport',
    params={{'accession': accession, 'result': 'read_run',
             'fields': 'run_accession,scientific_name,library_strategy,library_source,'
                       'library_selection,instrument_model,instrument_platform,sample_alias',
             'format': 'json'}}, timeout=30)
```

**PRIDE (PXDxxxxxx):**
```python
r = httpx.get(f'https://www.ebi.ac.uk/pride/ws/archive/v2/projects/{{acc}}', timeout=15)
# Check: instrumentNames, softwareList, sampleProcessingProtocol
```

**ArrayExpress / BioStudies:**
```python
r = httpx.get(f'https://www.ebi.ac.uk/biostudies/api/v1/studies/{{acc}}', timeout=15)
# Parse section.subsections for Sample attributes, Protocol attributes
```

**Zenodo / Figshare / OSF:**
- Read the record title, description, and file list carefully
- These often have limited structured metadata — use description + publication abstract together

### Step 4 — Fetch valid schema enum values

Before writing ANY annotation value, fetch the valid enum options:

```python
import httpx, yaml, json

with open('config/settings.yaml') as f:
    cfg = yaml.safe_load(f)
uri_prefix = cfg['synapse']['schema']['uri_prefix']

# Determine correct template from assay type
# e.g. 'rnaSeq' → 'rnaseqtemplate', 'proteomics' → 'proteomicstemplate'
# Full list at: cfg['synapse']['schema']['metadata_dictionary_url']

schema_uri = f"{{uri_prefix}}{{template_name}}"
r = httpx.get(
    f'https://repo-prod.prod.sagebase.org/repo/v1/schema/type/registered/{{schema_uri}}',
    timeout=30
)
schema = r.json()

# Extract enum values for each field by traversing properties
def get_enum_values(schema, field_name):
    props = schema.get('properties', {{}})
    if field_name in props:
        prop = props[field_name]
        # Handle $ref
        if '$ref' in prop:
            ref = prop['$ref']
            # fetch the referenced definition
            pass
        return prop.get('enum', prop.get('items', {{}}).get('enum', []))
    return []
```

**Never set a value that isn't in the enum.** If you can't find an exact match,
use the closest valid value and note it in your summary comment. For free-text
fields (studyLeads, institutions, fundingAgency), any value is valid.

### Step 5 — Reason about every annotation field

Work through **every field** in the tables below. For each one:
1. What does the current value say?
2. What does the primary source (paper + repo metadata) say?
3. Is the current value correct, missing, or wrong?
4. What is the correct value, validated against the enum?

**Project-level fields to evaluate:**

| Field | Primary source | Notes |
|-------|---------------|-------|
| `studyName` | PubMed title | Verify it's the full title, not truncated |
| `studyStatus` | Published? | Always `Completed` for published studies |
| `dataStatus` | Data accessible? | `Available` if repo is accessible |
| `diseaseFocus` | Abstract | From `config/settings.yaml` vocabulary |
| `manifestation` | Abstract | Be specific — plexiform vs cutaneous neurofibroma matter |
| `dataType` | Repo metadata | `geneExpression`, `genomicVariants`, `proteomics`, etc. |
| `studyLeads` | PubMed AuthorList | First author + last/corresponding author |
| `institutions` | PubMed affiliations | Institutions of study leads, not all co-authors |
| `fundingAgency` | PubMed GrantList | Agency names, not grant IDs |
| `pmid` | PubMed | Verify |
| `doi` | PubMed | Verify |
| `alternateDataRepository` | All accessions | `prefix:accession` format |

**File-level fields to evaluate for every File entity:**

| Field | Primary source | Notes |
|-------|---------------|-------|
| `assay` | GEO library_strategy / ENA library_strategy | Must match enum exactly |
| `species` | GEO organism / ENA scientific_name | Must match enum exactly |
| `tumorType` | Abstract + GEO sample source | Never omit |
| `diagnosis` | Abstract + sample characteristics | Must match enum |
| `fileFormat` | Filename extension | Strip `.gz`/`.bz2` — `fastq.gz` → `fastq` |
| `platform` | GEO instrument_model / ENA instrument_model | e.g. `Illumina HiSeq 4000` |
| `libraryPreparationMethod` | GEO library_strategy + protocol | e.g. `polyA selection`, `ribo-depletion`, `10x Chromium` |
| `dataSubtype` | File type + GEO processed/raw | `raw`, `processed`, `normalized` |
| `resourceType` | Always | `experimentalData` for data files |
| `specimenID` | Filename prefix or GEO sample name | One value per file |
| `individualID` | GEO sample characteristics or SRA sample alias | One value per file |
| `externalAccessionID` | Accession | Already set — verify |
| `externalRepository` | Source | Already set — verify |

**Also check:**
- `resourceStatus` should NOT be set on File entities — remove it if present
- Dataset entities should have `studyId`, `title`, `creator`, `contentType`, `resourceStatus` annotations

### Step 6 — Apply all annotations

For project-level, use the annotations2 REST endpoint:
```python
import json, requests, os

token = os.environ['SYNAPSE_AUTH_TOKEN']
headers = {{'Authorization': f'Bearer {{token}}', 'Content-Type': 'application/json'}}

# GET current etag
r = requests.get(f'https://repo-prod.prod.sagebase.org/repo/v1/entity/{{project_id}}/annotations2',
    headers=headers)
etag = r.json()['etag']

# Build annotation payload — preserve existing values you're not changing
payload = {{
    'id': project_id,
    'etag': etag,
    'annotations': {{
        'studyName':        {{'type': 'STRING', 'value': [study_name]}},
        'studyLeads':       {{'type': 'STRING', 'value': study_leads}},
        'institutions':     {{'type': 'STRING', 'value': institutions}},
        'fundingAgency':    {{'type': 'STRING', 'value': funding_agencies}},
        'diseaseFocus':     {{'type': 'STRING', 'value': disease_focus}},
        'manifestation':    {{'type': 'STRING', 'value': manifestation}},
        'dataType':         {{'type': 'STRING', 'value': data_types}},
        'studyStatus':      {{'type': 'STRING', 'value': ['Completed']}},
        'dataStatus':       {{'type': 'STRING', 'value': ['Available']}},
        'resourceStatus':   {{'type': 'STRING', 'value': ['pendingReview']}},
        'pmid':             {{'type': 'STRING', 'value': [pmid]}},
        'doi':              {{'type': 'STRING', 'value': [doi]}},
        'alternateDataRepository': {{'type': 'STRING', 'value': alt_repos}},
    }}
}}
requests.put(f'https://repo-prod.prod.sagebase.org/repo/v1/entity/{{project_id}}/annotations2',
    headers=headers, json=payload)
```

For file-level annotations, batch by files folder (50 files per script call to avoid timeout):
```python
# Use syn.set_annotations() for each file entity
from synapseclient import Annotations
anns = Annotations(entity=file_id, etag=current_etag, annotations={{
    'assay': assay_value,
    'species': species_value,
    'tumorType': tumor_type,
    ...
}})
syn.set_annotations(anns)
```

### Step 7 — Rebind schema to correct template

After updating file annotations, rebind the files folder to the correct schema:
```python
# Pick the right template based on assay
ASSAY_TO_TEMPLATE = {{
    'rnaSeq': 'rnaseqtemplate',
    'single-cell RNA-seq': 'scrnaseqtemplate',
    'wholeGenomeSeq': 'wholegenomesequencingtemplate',
    'wholeExomeSeq': 'exomeseqtemplate',
    'ATACSeq': 'atacseqtemplate',
    'ChIPSeq': 'chipseqtemplate',
    'methylationArray': 'methylationarraytemplate',
    'proteomics': 'proteomicstemplate',
    'other': 'processedgeneexpressiontemplate',  # universal fallback
}}
schema_uri = uri_prefix + ASSAY_TO_TEMPLATE.get(assay, 'processedgeneexpressiontemplate')
syn._rest_post(f'/entity/{{files_folder_id}}/schema/binding',
    body=json.dumps({{'entityId': files_folder_id,
                      'schema': {{'concreteType': 'org.sagebionetworks.repo.model.schema.JsonSchema',
                                  '$id': schema_uri}}}}))
```

### Step 8 — Post a detailed comment on the GitHub issue

After completing the project, post a comment on its study-review issue summarising
exactly what you changed and what you verified. Be specific — list field names and
the values you set. The data manager reading this comment should be able to approve
the project immediately without opening Synapse.

Format:
```
## Curation Pass Complete

**Source:** [PubMed PMID:xxxxx](https://pubmed.ncbi.nlm.nih.gov/xxxxx/) |
[GEO GSExxxxxx](https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSExxxxxx)

### Project-level annotations updated
| Field | Previous | New |
|-------|----------|-----|
| studyLeads | Not Available | Smith J, Doe A |
| institutions | (empty) | Indiana University, Johns Hopkins |
| fundingAgency | (empty) | NCI NIH HHS, DoD |
| manifestation | MPNST | MPNST, Plexiform Neurofibroma |
...

### File-level annotations updated (N files)
| Field | Value set | Source |
|-------|-----------|--------|
| platform | Illumina HiSeq 4000 | GEO GSM... instrument_model |
| libraryPreparationMethod | polyA selection | GEO series overall_design |
| tumorType | Malignant Peripheral Nerve Sheath Tumor | abstract + GSM source_name |
...

### Schema
Rebound files folder `syn...` from `processedgeneexpressiontemplate` → `rnaseqtemplate`

### Could not determine
- specimenID: filename prefixes are hash-like (e.g. `d7a3f9`) — cannot reliably parse
  without sample sheet. Data manager should verify.
```

Use `scripts/github_issue.py`'s `post_issue_comment()` or POST directly to the GitHub API.

---

## Important constraints

- **Read `CLAUDE.md` and `config/` before writing any code** — follow all safety rules
- **Rule 1 still applies** — portal tables are read-only
- **Rule 4 still applies** — do not modify CLAUDE.md, lib/, config/, or prompts/
- **Maximum 50 Synapse write operations total** across this entire run (projects × file batches)
- If you are uncertain about an annotation value, do not guess — leave it blank and note it
  in the comment. Wrong annotations are worse than missing ones.
- For files where specimenID/individualID cannot be reliably parsed from filenames or
  sample metadata, note this explicitly in the comment rather than inventing values.
- Work in `$AGENT_REPO_ROOT` and `$NADIA_WORKSPACE_DIR`. Write all scripts to workspace.

Start with the first project in `{queue_path}` and work down the list.
"""

    prompt_path = os.path.join(workspace_dir, "nadia_polish_prompt.md")
    with open(prompt_path, "w") as f:
        f.write(prompt)

    print(f"Queue written to {queue_path}")
    print(f"Prompt written to {prompt_path}")
    print(f"First 5 projects:")
    for p in projects[:5]:
        print(f"  {p['synapse_project_id']} PMID:{p['pmid'] or '(none)'}")


if __name__ == "__main__":
    main()
