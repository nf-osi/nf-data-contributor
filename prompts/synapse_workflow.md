# Synapse Entity Creation & Annotation Workflow

This file contains the detailed Synapse entity creation steps, annotation vocabulary workflow, zip handling, ADD outcome pattern, wiki template, and schema binding. Read this file when creating or updating Synapse projects.

---

## Dataset Entity Creation — Required Steps

**A `Dataset` entity (`org.sagebionetworks.repo.model.table.Dataset`) must be explicitly created for each accession.** It must be a **direct child of the project** (not nested in a subfolder) to appear in the Datasets tab.

```
{Project}/                         ← Synapse Project
├── {Repo}_{AccessionID}           ← Dataset entity (direct child — appears in Datasets tab)
├── Raw Data/                      ← Folder
│   └── {Repo}_{AccessionID}_files/← Folder (holds the actual File entities)
│       ├── file1.fastq.gz         ← File (externalURL)
│       └── file2.fastq.gz         ← File (externalURL)
├── Analysis/
└── Source Metadata/
```

### Step 1 — Create the files folder and populate it

```python
from synapseclient import Folder, File

files_folder = syn.store(Folder(
    name=f"{repository}_{accession_id}_files",
    parentId=raw_folder_id,
))

for filename, download_url in file_list:
    syn.store(File(
        name=filename,
        parentId=files_folder.id,
        synapseStore=False,
        path=download_url,   # use path= not externalURL= in synapseclient v4.x
    ))
```

### Step 2 — Create the Dataset entity and link files

```python
import json

# Create Dataset as direct child of the PROJECT (not Raw Data folder)
dataset_body = {
    'name': f"{repository}_{accession_id}",
    'parentId': project_id,   # ← project root
    'concreteType': 'org.sagebionetworks.repo.model.table.Dataset',
}
dataset = syn.restPOST('/entity', json.dumps(dataset_body))
dataset_id = dataset['id']

# Link file items to the Dataset
dataset_body = syn.restGET(f'/entity/{dataset_id}')
file_items = []
for child in syn.getChildren(files_folder.id, includeTypes=['file']):
    file_entity = syn.get(child['id'], downloadFile=False)
    file_items.append({
        'entityId': child['id'],
        'versionNumber': file_entity.properties.get('versionNumber', 1)
    })
dataset_body['items'] = file_items
syn.restPUT(f'/entity/{dataset_id}', json.dumps(dataset_body))
```

### Step 3 — Annotate the Dataset entity

```python
ann = syn.restGET(f'/entity/{dataset_id}/annotations2')
ann['annotations'] = {
    'contentType':         {'type': 'STRING', 'value': ['dataset']},
    'externalAccessionID': {'type': 'STRING', 'value': [accession_id]},
    'externalRepository':  {'type': 'STRING', 'value': [repository]},
    'resourceStatus':      {'type': 'STRING', 'value': ['pendingReview']},
    'study':               {'type': 'STRING', 'value': [project_name]},
}
syn.restPUT(f'/entity/{dataset_id}/annotations2', json.dumps(ann))
```

### Step 4 — Define columns on the Dataset entity

Without column definitions, the Dataset appears empty in the UI even if files have full annotations.

```python
ANNOTATION_COLUMNS = [
    ('study',                    'STRING', 256),
    ('assay',                    'STRING', 128),
    ('species',                  'STRING', 128),
    ('diagnosis',                'STRING', 256),
    ('tumorType',                'STRING', 256),
    ('platform',                 'STRING', 256),
    ('libraryPreparationMethod', 'STRING', 128),
    ('libraryStrand',            'STRING', 64),
    ('dataSubtype',              'STRING', 64),
    ('fileFormat',               'STRING', 64),
    ('resourceType',             'STRING', 64),
    ('resourceStatus',           'STRING', 64),
    ('externalAccessionID',      'STRING', 128),
    ('externalRepository',       'STRING', 64),
    ('specimenID',               'STRING', 128),
    ('individualID',             'STRING', 128),
]

col_ids = []
for col_name, col_type, col_size in ANNOTATION_COLUMNS:
    col = syn.restPOST('/column', json.dumps({
        'name': col_name, 'columnType': col_type, 'maximumSize': col_size
    }))
    col_ids.append(col['id'])

ds_body = syn.restGET(f'/entity/{dataset_id}')
ds_body['columnIds'] = col_ids
syn.restPUT(f'/entity/{dataset_id}', json.dumps(ds_body))
```

### Step 5 — Bind NF schema to the files folder and validate

```python
import time

js = syn.service('json_schema')
js.bind_json_schema(schema_uri, files_folder.id)   # ← bind to FILES FOLDER, not Dataset
time.sleep(3)
validation = js.validate(files_folder.id)
print(f"  Schema bound: {schema_uri}")
```

**Required order within create_project.py:**
1. Create project → folders → Dataset entity → File entities
2. Apply annotations to each individual File entity
3. Apply annotations to Dataset entity
4. `bind_nf_schema(syn, files_folder_id, schema_uri)` ← bind to FILES FOLDER
5. Print validation result

---

## Schema Selection — Dynamic, Not Hardcoded

1. Fetch available templates:
```python
import httpx

resp = httpx.get(
    'https://api.github.com/repos/nf-osi/nf-metadata-dictionary/contents/registered-json-schemas',
    timeout=15
)
available_templates = [f['name'].replace('.json', '') for f in resp.json() if f['name'].endswith('.json')]
```

2. Pick the best-matching template through reasoning (read the names, understand the dataset, select).

3. Convert to schema URI: lowercase and prepend `org.synapse.nf-`:
```python
# e.g. 'ScRNASeqTemplate' → 'org.synapse.nf-scrnaseqtemplate'
schema_uri = 'org.synapse.nf-' + template_name.lower()
```

4. Verify before binding:
```python
check = httpx.get(
    f'https://repo-prod.prod.sagebase.org/repo/v1/schema/type/registered/{schema_uri}',
    timeout=10
)
if check.status_code != 200:
    raise ValueError(f"Schema {schema_uri} not found")
```

---

## Annotation Vocabulary — Runtime Schema Query

### Step A — Fetch valid enum values

```python
import httpx

def fetch_schema_enums(schema_uri: str) -> dict[str, list[str]]:
    """
    Return field_name → [valid_enum_values] for a registered NF schema.
    IMPORTANT: Must traverse the 'properties' layer, not arbitrary keys — otherwise
    you pick up enum values from unrelated schema sub-objects (e.g. finding an 'assay'
    enum inside a clinical questionnaire block of the behavioral template).
    """
    url = f'https://repo-prod.prod.sagebase.org/repo/v1/schema/type/registered/{schema_uri}'
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    schema = resp.json()

    enums = {}
    def extract_enums(obj):
        if not isinstance(obj, dict):
            return
        props = obj.get('properties', {})
        for field_name, field_def in props.items():
            if isinstance(field_def, dict):
                if 'enum' in field_def:
                    enums[field_name] = field_def['enum']
                # Resolve $ref or anyOf/oneOf — recurse into sub-schemas
                for sub_key in ('anyOf', 'oneOf', 'allOf'):
                    for sub in field_def.get(sub_key, []):
                        if 'enum' in sub:
                            enums[field_name] = sub['enum']
        # Also recurse into definitions/$defs in case properties use $refs
        for defs_key in ('definitions', '$defs'):
            for defn in obj.get(defs_key, {}).values():
                extract_enums(defn)
    extract_enums(schema)
    return enums
```

**When to use which schema for behavioral/model organism data:**
- `org.synapse.nf-behavioralassaytemplate` — covers both human clinical behavioral instruments AND animal model behavioral assays (open field test, rotarod, elevated plus maze, grooming, etc.). Use this for behavioral data of any species. Note: this schema always requires `compoundName`, `compoundDose`, and `compoundDoseUnit` — for non-drug studies, set these to `'Not Applicable'` / `'0'` / `'Not Applicable'` respectively. Also requires `dataType` (use `'behavioral data'` for behavioral studies).
- `org.synapse.nf-biologicalassaydatatemplate` — has no enum constraints at all; not useful for controlled-vocabulary annotation. Avoid.
- `org.synapse.nf-generalmeasuredatatemplate` — general quantitative measurements; has the broadest assay enum (202 values including RNA-seq, imaging, etc.).

### Step B — Extract raw metadata from source

Gather raw metadata from all available sources before normalizing:
- **Repository metadata API**: organism, tissue, disease, assay, platform, library prep, sample IDs, sex, age, treatment
- **Sample-level metadata**: GEO GSM records, SRA BioSample records, ArrayExpress sample sheets — often the richest per-specimen details
- **Associated publication**: Extract from abstract/methods when PMID available (specimen prep, patient cohort, library kits, platforms)
- **File names/formats**: Infer `fileFormat` from extensions

Dump all extracted values into a flat `raw_metadata` dict before normalizing.

### Step C — Normalize raw values to schema terms

Read the raw values, read the valid enum lists from Step A, pick the best match. Write the result directly — no API call needed:

```python
import json

normalized = {
    'assay': 'RNA-seq',
    'species': 'Mus musculus',
    'diagnosis': 'Neurofibromatosis type 1',
    'tumorType': 'Malignant Peripheral Nerve Sheath Tumor',
    'organ': 'bone marrow',
    'sex': 'male',
    'platform': 'Illumina NovaSeq X Plus',
    'libraryPreparationMethod': 'unknown',
    'libraryStrand': 'Unstranded',
    'dataSubtype': 'raw',
}

with open('/tmp/nf_agent/normalized_annotations.json', 'w') as f:
    json.dump(normalized, f, indent=2)
```

**Key constraints:**
- Only set values that exist in the schema's enum list — never hardcode
- `tumorType` is required on every file — derive from paper/abstract
- `specimenID` is one per file — parse from filename prefix, never a multi-value list
- `fileFormat` must match schema enum exactly — strip `.gz`/`.zip` (e.g. `fastq.gz` → `fastq`)
- `manifestation` at project level: use "Low-Grade Glioma NOS" NOT "Low Grade Glioma"

### Step D — Apply annotations to File entities

```python
import re

for child in syn.getChildren(files_folder_id, includeTypes=['file']):
    f = syn.get(child['id'], downloadFile=False)
    f.annotations.update(shared_annotations)  # shared across all files

    # fileFormat: strip compression suffix, take final extension
    name_lower = re.sub(r'\.(gz|zip|bz2)$', '', child['name'].lower())
    f.annotations['fileFormat'] = name_lower.rsplit('.', 1)[-1]

    # specimenID / individualID: parse from filename prefix or sample map
    m = re.match(r'([A-Z]+\d+)[_.]', child['name'])
    sample_id = m.group(1) if m else None
    if sample_id and sample_id in sample_map:
        f.annotations['specimenID'] = sample_id
        f.annotations['individualID'] = sample_map[sample_id].get('individualID', sample_id)
    elif sample_id:
        f.annotations['specimenID'] = sample_id
        f.annotations['individualID'] = sample_id

    syn.store(f)
```

**Shared across all files**: `assay`, `species`, `diagnosis`, `tumorType`, `platform`, `libraryPreparationMethod`, `libraryStrand`, `specimenPreparationMethod`, `study`, `externalAccessionID`, `externalRepository`, `dataSubtype`, `resourceStatus`

**Per-file**: `fileFormat`, `specimenID`, `individualID`

---

## Project-Level Annotations

```python
ann = syn.restGET(f'/entity/{project_id}/annotations2')
ann['annotations'] = {
    'studyName':               {'type': 'STRING', 'value': [project_name]},
    'studyStatus':             {'type': 'STRING', 'value': ['Completed']},
    'dataStatus':              {'type': 'STRING', 'value': ['Available']},
    'diseaseFocus':            {'type': 'STRING', 'value': disease_focus_list},
    'manifestation':           {'type': 'STRING', 'value': manifestation_list},
    'dataType':                {'type': 'STRING', 'value': data_type_list},
    'studyLeads':              {'type': 'STRING', 'value': study_leads_list},
    'institutions':            {'type': 'STRING', 'value': institutions_list},
    'fundingAgency':           {'type': 'STRING', 'value': funding_agency_list},
    'resourceStatus':          {'type': 'STRING', 'value': ['pendingReview']},
    'alternateDataRepository': {'type': 'STRING', 'value': alternate_data_repos},
    'pmid':                    {'type': 'STRING', 'value': [pmid]} if pmid else {},
    'doi':                     {'type': 'STRING', 'value': [doi]}  if doi  else {},
}
syn.restPUT(f'/entity/{project_id}/annotations2', json.dumps(ann))
```

**Funder extraction — check in order:**
1. **PubMed GrantList** (most reliable): `grants = art.get('GrantList', [])` → `{g.get('Agency') for g in grants}`
2. **Acknowledgements section**: look for "funded by", "supported by" phrases
3. **Repository metadata**: Zenodo records sometimes list funders in `metadata.grants`
4. **Fallback**: `['Not Applicable (External Study)']`

**`studyStatus`**: Always `Completed` for published studies with deposited data. Never "Active".

**`dataType` vocabulary**: `geneExpression`, `genomicVariants`, `proteomics`, `drugScreen`, `immunoassay`, `image`, `surveyData`, `clinicalData`, `other`

---

## Adding a Dataset to an Existing Project (ADD Outcome)

```python
# Find Raw Data folder in existing project
children = list(syn.getChildren(existing_project_id, includeTypes=['folder']))
raw_data_folder = next((c for c in children if c.get('name') == 'Raw Data'), None)

if raw_data_folder:
    raw_folder_id = raw_data_folder.get('id')

    # Create files folder inside Raw Data/
    files_folder = syn.store(Folder(
        name=f'{repository}_{accession_id}_files',
        parentId=raw_folder_id
    ))
    # Enumerate and create File entities (see prompts/repo_apis.md)

    # Create Dataset entity as direct child of the PROJECT
    import json
    ds_body = syn.restPOST('/entity', json.dumps({
        'name': f'{repository}_{accession_id}',
        'parentId': existing_project_id,   # ← project root, not Raw Data
        'concreteType': 'org.sagebionetworks.repo.model.table.Dataset',
    }))
    ds_id = ds_body['id']
    # Link files as dataset items, set columnIds, annotate (same as Steps 2-4 above)
```

If the existing project is portal-managed (not in agent state table), **do not write to it**. Create a JIRA ticket flagged as "manual action required" instead.

---

## Zip Files — Flag for Interactive Processing

When a repository contains `.zip` files, do NOT attempt to download/extract in automated runs:

1. Create a `File` entity pointing to the zip's direct download URL (as normal)
2. Add annotation `needsExtraction: true` to that File entity
3. Create a JIRA ticket flagged as `interactive-processing-required`
4. Note the zip in the wiki under "Pending Data Manager Actions"

```python
import os, json

def flag_zip_for_extraction(syn, file_entity_id, zip_url, zip_size_bytes,
                             zip_filename, dataset_id, files_folder_id,
                             project_id, project_name, accession_id,
                             source_repository, schema_uri, normalized_annotations):
    f = syn.get(file_entity_id, downloadFile=False)
    f.annotations['needsExtraction'] = 'true'
    f.annotations['zipSizeMB'] = str(round(zip_size_bytes / 1024 / 1024, 1))
    syn.store(f)

    zip_size_mb = zip_size_bytes / 1024 / 1024
    recommended_disk_gb = max(int(zip_size_mb * 3 / 1024) + 1, 1)
    synapse_url = f'https://www.synapse.org/#!Synapse:{project_id}'

    handoff_prompt = f"""
INTERACTIVE EXTRACTION TASK

Synapse project: {project_id}
Dataset entity: {dataset_id}
Files folder: {files_folder_id}
Zip file entity to replace: {file_entity_id}
Zip download URL: {zip_url}
Source repository: {source_repository} ({accession_id})
Schema URI: {schema_uri}
Shared annotations: {json.dumps(normalized_annotations, indent=2)}

Steps:
1. Download the zip to /tmp/extract_{accession_id}/
2. Extract and list all files with sizes
3. For each file: create a Synapse File entity (synapseStore=True) inside {files_folder_id}
4. Apply shared annotations + per-file specimenID/individualID/fileFormat
5. Update Dataset {dataset_id} items list to include all new files
6. Delete the original zip File entity {file_entity_id}
7. Print a summary of files uploaded
"""

    base_url = os.environ.get('JIRA_BASE_URL', '').rstrip('/')
    email = os.environ.get('JIRA_USER_EMAIL', '')
    token = os.environ.get('JIRA_API_TOKEN', '')
    if base_url and email and token:
        import httpx
        payload = {
            'fields': {
                'project': {'key': 'NFOSI'},
                'summary': f'[Interactive] Extract zip — {project_name[:80]} ({project_id})',
                'description': {
                    'type': 'doc', 'version': 1,
                    'content': [{'type': 'paragraph', 'content': [
                        {'type': 'text', 'text':
                         f'Zip: {zip_filename} ({zip_size_mb:.0f} MB)\n'
                         f'Synapse: {synapse_url}\n\n'
                         f'Paste into interactive Claude Code session '
                         f'({recommended_disk_gb} GB free needed):\n\n'
                         + handoff_prompt}
                    ]}]
                },
                'issuetype': {'name': 'Task'},
                'labels': ['interactive-processing-required', 'zip-extraction'],
            }
        }
        resp = httpx.post(f'{base_url}/rest/api/3/issue',
                          json=payload, auth=(email, token), timeout=15)
        if resp.status_code in (200, 201):
            print(f"  JIRA ticket created: {resp.json().get('key')}")

    return handoff_prompt
```

**Recommended disk:** `max(zip_size_mb * 3, 500)` MB (zip + extracted + upload buffer).

**Do NOT leave `needsExtraction` as a final annotation.** Always either apply proper annotations or remove the placeholder before logging the project as complete.

---

## Wiki Page Template

```python
def make_wiki_content(pub_title, disease_focus, assay_types, species, tissue_types,
                      pmid, doi, authors, pub_date, plain_summary, abstract,
                      datasets_table_rows, today):
    pmid_link = f'[PMID {pmid}](https://pubmed.ncbi.nlm.nih.gov/{pmid}/)' if pmid else (doi or 'Not available')
    authors_str = ', '.join(authors[:3]) + (' et al.' if len(authors) > 3 else '')

    datasets_rows_md = '\n'.join(
        f'| {r["repo"]} | [{r["accession"]}]({r["url"]}) | {r["data_types"]} | {r["files"]} | Open |'
        for r in datasets_table_rows
    )

    return f"""## {pub_title}

**Disease Focus:** {', '.join(disease_focus)}
**Assay Type:** {', '.join(assay_types)}
**Species:** {', '.join(species)}
**Tissue / Cell Type:** {', '.join(tissue_types)}
**Publication:** {pmid_link}
**Authors:** {authors_str}
**Publication Date:** {pub_date or 'Not available'}

---

### Summary

{plain_summary}

---

### Background

{abstract}

---

### Datasets

| Repository | Accession | Data Types | Files | Access |
|-----------|-----------|-----------|-------|--------|
{datasets_rows_md}

---

*This project was ingested automatically by the NF Data Contributor Agent on {today} and is pending data manager review.*
"""
```

**Plain-language summary guidance** (write directly through reasoning, no API call):
- Sentence 1: the disease/condition studied and why it matters
- Sentence 2: what data was generated (assay, model system, experimental design)
- Sentence 3: what was found or what the dataset enables

**Wiki title**: use the full publication title (not "Auto-Discovered..." or similar). Do not add "Auto-Discovered" to the title or anywhere in the wiki header.

---

## `bind_nf_schema()` Helper

```python
import time, httpx

def bind_nf_schema(syn, files_folder_id: str, schema_uri: str) -> dict:
    """Bind a chosen NF metadata schema to a dataset files folder and validate."""
    try:
        check = httpx.get(
            f'https://repo-prod.prod.sagebase.org/repo/v1/schema/type/registered/{schema_uri}',
            timeout=10
        )
        if check.status_code != 200:
            raise ValueError(f"Schema {schema_uri} not found (HTTP {check.status_code})")

        js = syn.service("json_schema")
        js.bind_json_schema(schema_uri, files_folder_id)
        time.sleep(3)

        validation = js.validate(files_folder_id)
        return {'schema_uri': schema_uri, 'folder_id': files_folder_id,
                'validation': validation, 'status': 'bound'}
    except Exception as e:
        print(f"  Warning: schema binding failed for {files_folder_id}: {e}")
        return {'schema_uri': schema_uri, 'folder_id': files_folder_id,
                'status': 'error', 'error': str(e)}
```

Validation warnings are expected at this stage — some required fields can only be filled by human curators. The binding itself is what matters for Curator Grid visibility.

---

## Self-Audit and Remediation

Run this after Step 6 (project creation) and before JIRA notifications. The audit is three phases:

- **Phase 1 (`audit.py`)** — Python script: mechanical checks + immediate auto-fixes for anything that requires no reasoning; collects context for reasoning-required gaps
- **Phase 2 (agent reasoning)** — Read the audit output; reason through missing annotation values using available context; write `audit_reasoning_fixes.json`
- **Phase 3 (`apply_audit_fixes.py`)** — Python script: apply all reasoned fixes to Synapse

### Audit Lessons from Live Testing

These issues were discovered when the audit was run on real agent-created projects (2026-03-28) and are not always caught at creation time:

1. **All file annotations were missing** — projects created before the annotation step was enforced had zero file-level annotations. The audit auto-fix sweep adds the mechanical fields; the reasoning pass adds domain-knowledge fields.

2. **`manifestation='Unspecified'` is not valid portal vocabulary** — treat it the same as missing. Valid behavioral/neurological manifestations in the portal include `'Behavioral'`, `'Cognition'`, `'Memory'`, `'Pain'` in addition to tumor types. Check the portal vocab list explicitly.

3. **`studyLeads=['Unknown']` — search PubMed first** — ENA projects without a PMID often have an associated publication findable via title search. Include `instrument_model`, center name, and key terms in the query.

4. **Schema enum extraction must use the `properties` layer** — a naive recursive search for any dict with `'enum'` key picks up enum values from unrelated sub-objects (e.g. a clinical questionnaire block inside the behavioral template returned `'Child Behavior Checklist for Ages 1.5-5'` as the assay value for Drosophila grooming data). Always traverse via `schema['properties']`.

5. **`nf-behavioralassaytemplate` covers both human clinical and animal model behavioral data** — its assay enum includes animal tests (open field, rotarod, elevated plus maze, etc.) alongside clinical questionnaires. It always requires `compoundName`/`compoundDose`/`compoundDoseUnit`; set these to `'Not Applicable'`/`'0'`/`'Not Applicable'` for non-drug studies. Also requires `dataType` — use `'behavioral data'`. `nf-biologicalassaydatatemplate` has no enum constraints at all and should be avoided.

6. **Dataset `columnIds` count may be stale** — projects created before the column list was expanded from 9 to 16 need their columnIds updated.

7. **Schema binding was missing on all ENA/Zenodo projects** — the binding step was added to the creation workflow but not retroactively applied. The audit catches and fixes this.

### `created_projects.json` Schema (output of Step 6, input to audit)

Step 6 must write this file. The audit reads it:

```json
[
  {
    "synapse_project_id": "syn74287500",
    "project_name": "Clinical spectrum of individuals with pathogenic NF1 missense variants",
    "pmid": "31595648",
    "doi": "10.1038/s41436-019-0691-3",
    "pub_group_id": "pmid_31595648",
    "abstract": "...",
    "outcome": "NEW",
    "datasets": [
      {
        "accession_id": "4688881",
        "source_repository": "Zenodo",
        "schema_uri": "org.synapse.nf-bulksequencingassaytemplate",
        "files_folder_id": "syn74287504",
        "dataset_id": "syn74287503",
        "landing_url": "https://zenodo.org/records/4688881"
      }
    ]
  }
]
```

### Phase 1 — `audit.py`

```python
#!/usr/bin/env python3
"""
Self-audit: check every project created this run against the completion checklist.
Auto-fixes mechanical issues immediately. Collects context for reasoning gaps.
"""
import json, sys, os, re, time
sys.path.insert(0, os.environ.get('AGENT_REPO_ROOT', '.') + '/lib')
from synapse_login import get_synapse_client
import synapseclient

syn = get_synapse_client()

with open('/tmp/nf_agent/created_projects.json') as f:
    created = json.load(f)

ANNOTATION_COLUMNS = [
    ('study','STRING',256), ('assay','STRING',128), ('species','STRING',128),
    ('diagnosis','STRING',256), ('tumorType','STRING',256), ('platform','STRING',256),
    ('libraryPreparationMethod','STRING',128), ('libraryStrand','STRING',64),
    ('dataSubtype','STRING',64), ('fileFormat','STRING',64), ('resourceType','STRING',64),
    ('resourceStatus','STRING',64), ('externalAccessionID','STRING',128),
    ('externalRepository','STRING',64), ('specimenID','STRING',128), ('individualID','STRING',128),
]

# Fields auto-fixable without reasoning (value is deterministic)
AUTO_FIX_PROJECT = {
    'studyStatus':    'Completed',
    'dataStatus':     'Available',
    'resourceStatus': 'pendingReview',
}
# Fields that require reasoning if missing
REASONING_PROJECT_FIELDS = ['diseaseFocus', 'manifestation', 'dataType',
                             'studyLeads', 'institutions', 'alternateDataRepository']
REASONING_FILE_FIELDS     = ['assay', 'species', 'tumorType', 'diagnosis',
                              'platform', 'libraryPreparationMethod']

audit_results = []

for proj in created:
    project_id   = proj['synapse_project_id']
    project_name = proj.get('project_name', '')
    pmid         = proj.get('pmid', '')
    doi          = proj.get('doi', '')
    abstract     = proj.get('abstract', '')
    datasets     = proj.get('datasets', [])

    result = {
        'project_id':     project_id,
        'project_name':   project_name,
        'pmid':           pmid,
        'doi':            doi,
        'abstract':       abstract,
        'fixes_applied':  [],
        'warnings':       [],
        'reasoning_gaps': [],  # populated below; agent resolves in Phase 2
    }

    print(f"\n{'='*60}")
    print(f"Auditing: {project_id} — {project_name[:60]}")

    # ── 1. Project-level annotations ─────────────────────────────
    try:
        ann = syn.restGET(f'/entity/{project_id}/annotations2')
        cur = {k: v['value'] for k, v in ann.get('annotations', {}).items()}
        updates = {}

        # Auto-fixable values
        for field, correct_val in AUTO_FIX_PROJECT.items():
            cur_val = cur.get(field, [''])[0] if cur.get(field) else ''
            if cur_val != correct_val:
                updates[field] = {'type': 'STRING', 'value': [correct_val]}
                result['fixes_applied'].append(f'project.{field}: {cur_val!r} → {correct_val!r}')

        if 'studyName' not in cur and project_name:
            updates['studyName'] = {'type': 'STRING', 'value': [project_name]}
            result['fixes_applied'].append('project.studyName set from project_name')

        if 'fundingAgency' not in cur:
            updates['fundingAgency'] = {'type': 'STRING', 'value': ['Not Applicable (External Study)']}
            result['fixes_applied'].append('project.fundingAgency → Not Applicable (External Study)')

        if pmid and 'pmid' not in cur:
            updates['pmid'] = {'type': 'STRING', 'value': [pmid]}
            result['fixes_applied'].append(f'project.pmid → {pmid}')

        if doi and 'doi' not in cur:
            updates['doi'] = {'type': 'STRING', 'value': [doi]}
            result['fixes_applied'].append(f'project.doi → {doi}')

        if updates:
            ann['annotations'].update(updates)
            syn.restPUT(f'/entity/{project_id}/annotations2', json.dumps(ann))
            print(f"  Project annotations: {len(updates)} fix(es) applied")
        else:
            print(f"  Project annotations: all auto-fixable fields OK")

        # Collect reasoning gaps
        for field in REASONING_PROJECT_FIELDS:
            val = cur.get(field, [])
            if isinstance(val, list):
                val = [v for v in val if v and v != 'Unknown']
            if not val:
                result['reasoning_gaps'].append({'scope': 'project', 'field': field})

    except Exception as e:
        result['warnings'].append(f'Project annotation check failed: {e}')

    # ── 2. NF-OSI team permissions ────────────────────────────────
    try:
        acl = syn.restGET(f'/entity/{project_id}/acl')
        has_team = any(ra['principalId'] == 3378999 for ra in acl.get('resourceAccess', []))
        if not has_team:
            syn.setPermissions(
                project_id, principalId='3378999',
                accessType=['READ','DOWNLOAD','CREATE','UPDATE','DELETE',
                            'CHANGE_PERMISSIONS','CHANGE_SETTINGS','MODERATE',
                            'UPDATE_SUBMISSION','READ_PRIVATE_SUBMISSION'],
                warn_if_inherits=False
            )
            result['fixes_applied'].append('NF-OSI team (3378999) permissions granted')
            print(f"  Permissions: FIXED — NF-OSI team granted access")
        else:
            print(f"  Permissions: OK")
    except Exception as e:
        result['warnings'].append(f'Permissions check failed: {e}')

    # ── 3. Wiki ────────────────────────────────────────────────────
    try:
        wiki = syn.getWiki(project_id)
        print(f"  Wiki: OK (id={wiki.id})")
    except Exception:
        result['reasoning_gaps'].append({'scope': 'project', 'field': 'wiki'})
        result['warnings'].append('Wiki missing — flagged for creation in Phase 2')
        print(f"  Wiki: MISSING")

    # ── 4. Per-dataset audit ───────────────────────────────────────
    for ds in datasets:
        acc             = ds.get('accession_id', '')
        repo            = ds.get('source_repository', '')
        schema_uri      = ds.get('schema_uri', '')
        files_folder_id = ds.get('files_folder_id', '')
        dataset_id      = ds.get('dataset_id', '')

        print(f"\n  Dataset: {repo}:{acc}")

        # 4a. List files
        try:
            file_children = list(syn.getChildren(files_folder_id, includeTypes=['file']))
            print(f"    Files: {len(file_children)}")
            if len(file_children) == 0:
                result['warnings'].append(f'{acc}: 0 files in {files_folder_id}')
        except Exception as e:
            result['warnings'].append(f'{acc}: could not list files — {e}')
            file_children = []

        # 4b. File-level annotations — auto-fix + collect gaps
        files_gap_context = []
        for child in file_children:
            try:
                fe       = syn.get(child['id'], downloadFile=False)
                ann_dict = dict(fe.annotations)
                filename = child.get('name', '')
                file_gap = {'file_id': child['id'], 'filename': filename, 'fixes': [], 'gaps': []}
                changed  = False

                def _scalar(v):
                    return v[0] if isinstance(v, list) and v else (v or '')

                # Auto-fixes
                if not _scalar(ann_dict.get('resourceStatus')):
                    fe.annotations['resourceStatus'] = 'pendingReview'
                    file_gap['fixes'].append('resourceStatus → pendingReview'); changed = True

                if not _scalar(ann_dict.get('resourceType')):
                    fe.annotations['resourceType'] = 'experimentalData'
                    file_gap['fixes'].append('resourceType → experimentalData'); changed = True

                if not _scalar(ann_dict.get('externalAccessionID')) and acc:
                    fe.annotations['externalAccessionID'] = acc
                    file_gap['fixes'].append(f'externalAccessionID → {acc}'); changed = True

                if not _scalar(ann_dict.get('externalRepository')) and repo:
                    fe.annotations['externalRepository'] = repo
                    file_gap['fixes'].append(f'externalRepository → {repo}'); changed = True

                if not _scalar(ann_dict.get('study')) and project_name:
                    fe.annotations['study'] = project_name
                    file_gap['fixes'].append('study set'); changed = True

                if not _scalar(ann_dict.get('dataSubtype')):
                    raw_exts = {'.fastq', '.fq', '.bam', '.cram', '.vcf', '.bcf', '.cel', '.idat'}
                    base = re.sub(r'\.(gz|zip|bz2|xz)$', '', filename.lower())
                    subtype = 'raw' if any(base.endswith(e) for e in raw_exts) else 'processed'
                    fe.annotations['dataSubtype'] = subtype
                    file_gap['fixes'].append(f'dataSubtype → {subtype}'); changed = True

                # fileFormat — strip compression suffix
                cur_fmt  = _scalar(ann_dict.get('fileFormat', ''))
                base_name = re.sub(r'\.(gz|zip|bz2|xz)$', '', filename.lower())
                bare_ext  = base_name.rsplit('.', 1)[-1] if '.' in base_name else ''
                if cur_fmt and re.search(r'\.(gz|zip|bz2)$', cur_fmt):
                    fe.annotations['fileFormat'] = bare_ext
                    file_gap['fixes'].append(f'fileFormat {cur_fmt!r} → {bare_ext!r}'); changed = True
                elif not cur_fmt and bare_ext:
                    fe.annotations['fileFormat'] = bare_ext
                    file_gap['fixes'].append(f'fileFormat → {bare_ext!r}'); changed = True

                # specimenID — auto-parse from filename if possible
                specimen = _scalar(ann_dict.get('specimenID', ''))
                if not specimen:
                    m = re.match(r'(GSM\d+|SRR\d+|ERR\d+|DRR\d+)[_.]', filename)
                    if m:
                        fe.annotations['specimenID']  = m.group(1)
                        fe.annotations['individualID'] = m.group(1)
                        file_gap['fixes'].append(f'specimenID/individualID → {m.group(1)}'); changed = True
                    else:
                        file_gap['gaps'].append('specimenID missing (cannot auto-parse)')
                elif isinstance(ann_dict.get('specimenID'), list) and len(ann_dict['specimenID']) > 1:
                    file_gap['gaps'].append('specimenID is multi-value — needs per-file correction')

                # needsExtraction still set?
                if ann_dict.get('needsExtraction'):
                    file_gap['gaps'].append('needsExtraction still set — zip not yet extracted')

                # Reasoning-required fields
                for field in REASONING_FILE_FIELDS:
                    if not _scalar(ann_dict.get(field, '')):
                        file_gap['gaps'].append(f'{field} missing')

                if changed:
                    syn.store(fe)
                    result['fixes_applied'].append(
                        f'{acc}/{filename}: {len(file_gap["fixes"])} file annotation fix(es)')

                if file_gap['fixes'] or file_gap['gaps']:
                    files_gap_context.append(file_gap)

            except Exception as e:
                result['warnings'].append(f'File {child["id"]} check failed: {e}')

        if files_gap_context:
            result['reasoning_gaps'].append({
                'scope':           'files',
                'accession_id':    acc,
                'source_repository': repo,
                'files_folder_id': files_folder_id,
                'dataset_id':      dataset_id,
                'files':           files_gap_context,
            })

        # 4c. Dataset entity — items, columnIds, annotations
        if dataset_id:
            try:
                ds_body = syn.restGET(f'/entity/{dataset_id}')

                # Items
                if not ds_body.get('items') and file_children:
                    new_items = []
                    for child in file_children:
                        fe = syn.get(child['id'], downloadFile=False)
                        new_items.append({
                            'entityId':      child['id'],
                            'versionNumber': fe.properties.get('versionNumber', 1),
                        })
                    ds_body['items'] = new_items
                    syn.restPUT(f'/entity/{dataset_id}', json.dumps(ds_body))
                    result['fixes_applied'].append(f'{acc}: Dataset items re-linked ({len(new_items)})')
                    print(f"    Dataset items: FIXED — linked {len(new_items)} files")
                else:
                    print(f"    Dataset items: OK ({len(ds_body.get('items', []))} items)")

                # columnIds
                if not ds_body.get('columnIds'):
                    col_ids = []
                    for col_name, col_type, col_size in ANNOTATION_COLUMNS:
                        col = syn.restPOST('/column', json.dumps({
                            'name': col_name, 'columnType': col_type, 'maximumSize': col_size,
                        }))
                        col_ids.append(col['id'])
                    ds_body2 = syn.restGET(f'/entity/{dataset_id}')
                    ds_body2['columnIds'] = col_ids
                    syn.restPUT(f'/entity/{dataset_id}', json.dumps(ds_body2))
                    result['fixes_applied'].append(f'{acc}: Dataset columnIds created')
                    print(f"    Dataset columnIds: FIXED")
                else:
                    print(f"    Dataset columnIds: OK")

                # Dataset annotations
                ds_ann = syn.restGET(f'/entity/{dataset_id}/annotations2')
                ds_cur  = {k: v['value'] for k, v in ds_ann.get('annotations', {}).items()}
                ds_upd  = {}
                defaults = {
                    'contentType':         ['dataset'],
                    'resourceStatus':      ['pendingReview'],
                    'externalAccessionID': [acc] if acc else None,
                    'externalRepository':  [repo] if repo else None,
                    'study':               [project_name] if project_name else None,
                }
                for k, v in defaults.items():
                    if v and k not in ds_cur:
                        ds_upd[k] = {'type': 'STRING', 'value': v}
                if ds_upd:
                    ds_ann['annotations'].update(ds_upd)
                    syn.restPUT(f'/entity/{dataset_id}/annotations2', json.dumps(ds_ann))
                    result['fixes_applied'].append(f'{acc}: Dataset annotations updated {list(ds_upd.keys())}')
                    print(f"    Dataset annotations: FIXED {list(ds_upd.keys())}")
                else:
                    print(f"    Dataset annotations: OK")

            except Exception as e:
                result['warnings'].append(f'{acc}: Dataset {dataset_id} check failed: {e}')

        # 4d. Schema binding
        if schema_uri and files_folder_id:
            try:
                try:
                    binding = syn.restGET(f'/entity/{files_folder_id}/jsonschema/binding')
                    bound_uri = binding.get('jsonSchemaVersionInfo', {}).get('$id', '')
                    print(f"    Schema: OK ({bound_uri})")
                except Exception:
                    js = syn.service('json_schema')
                    js.bind_json_schema(schema_uri, files_folder_id)
                    time.sleep(2)
                    result['fixes_applied'].append(f'{acc}: Schema {schema_uri} bound')
                    print(f"    Schema: FIXED — bound {schema_uri}")
            except Exception as e:
                result['warnings'].append(f'{acc}: Schema binding failed: {e}')

    # Per-project summary
    n_fixes = len(result['fixes_applied'])
    n_gaps  = sum(
        len(item.get('files', [])) if item['scope'] == 'files'
        else (1 if item['scope'] == 'project' else 0)
        for item in result['reasoning_gaps']
    )
    print(f"\n  Summary: {n_fixes} auto-fix(es), {n_gaps} reasoning gap(s), "
          f"{len(result['warnings'])} warning(s)")

    audit_results.append(result)

# Write output for Phase 2
with open('/tmp/nf_agent/audit_results.json', 'w') as f:
    json.dump(audit_results, f, indent=2)

# Print structured summary of what needs reasoning
any_gaps = any(r['reasoning_gaps'] for r in audit_results)
if any_gaps:
    print(f"\n{'='*60}")
    print("REASONING GAPS — agent must resolve these before apply_audit_fixes.py:")
    for r in audit_results:
        if not r['reasoning_gaps']:
            continue
        print(f"\n  {r['project_id']} — {r['project_name'][:60]}")
        if r.get('pmid'):
            print(f"  PMID: {r['pmid']} | Abstract available: {'yes' if r.get('abstract') else 'no'}")
        for item in r['reasoning_gaps']:
            if item['scope'] == 'project':
                print(f"    [project annotation] {item['field']} missing")
            elif item['scope'] == 'files':
                print(f"    [files] {item['accession_id']} ({item['source_repository']})")
                for fi in item.get('files', []):
                    if fi.get('gaps'):
                        print(f"      {fi['filename']}: {', '.join(fi['gaps'])}")
else:
    print(f"\nAll projects passed or were auto-fixed. No reasoning gaps.")

print(f"\nAudit Phase 1 complete. Results: /tmp/nf_agent/audit_results.json")
```

---

### Phase 2 — Agent Reasoning

After running `audit.py`, read `/tmp/nf_agent/audit_results.json`. For each project with `reasoning_gaps`:

1. **Read the available context** — project annotations (studyName, alternateDataRepository), the abstract stored in audit_results, and the wiki if it exists
2. **If PMID is available and abstract is missing**, fetch it: `Entrez.efetch(db='pubmed', id=pmid, rettype='xml')`
3. **Reason through each gap** using the publication title + abstract + repository metadata:
   - `diseaseFocus`, `manifestation`: infer from disease mentions in abstract
   - `dataType`: infer from assay type (scRNA-seq → `geneExpression`)
   - `studyLeads`: if PMID known, fetch AuthorList; take first + last author
   - `institutions`: from author affiliations in PubMed record
   - `alternateDataRepository`: reconstruct from accession_id + source_repository using REPO_TO_PREFIX
   - `assay`, `species`, `tumorType`, `diagnosis`: infer from abstract + title
   - `platform`: fetch from repository metadata (GEO GSE → series platform, SRA → instrument model)
   - `libraryPreparationMethod`: infer from abstract ("10x Chromium", "Smart-seq2", "polyA", etc.)
   - `specimenID` for files where auto-parse failed: look at repository sample table (GEO GSM list, SRA BioSample)
   - `wiki` missing: create using the wiki template from this file
4. **Write `/tmp/nf_agent/audit_reasoning_fixes.json`** with all resolved values

```json
[
  {
    "project_id": "syn74287500",
    "project_annotation_fixes": {
      "diseaseFocus": ["Neurofibromatosis type 1"],
      "manifestation": ["Neurofibroma"],
      "dataType": ["geneExpression"],
      "studyLeads": ["Smith J", "Doe A"],
      "institutions": ["University of X"],
      "alternateDataRepository": ["zenodo.record:4688881"]
    },
    "wiki_content": "## Clinical spectrum...\n\n...",
    "file_annotation_fixes": [
      {
        "file_id": "syn74287506",
        "annotations": {
          "assay": "RNA-seq",
          "species": "Homo sapiens",
          "tumorType": "Neurofibroma",
          "diagnosis": "Neurofibromatosis type 1",
          "platform": "Illumina NovaSeq 6000",
          "libraryPreparationMethod": "polyA",
          "specimenID": "NF001",
          "individualID": "NF001"
        }
      }
    ]
  }
]
```

---

### Phase 3 — `apply_audit_fixes.py`

```python
#!/usr/bin/env python3
"""Apply reasoned annotation fixes from audit Phase 2."""
import json, sys, os
sys.path.insert(0, os.environ.get('AGENT_REPO_ROOT', '.') + '/lib')
from synapse_login import get_synapse_client
import synapseclient
from synapseclient import Wiki

syn = get_synapse_client()

with open('/tmp/nf_agent/audit_reasoning_fixes.json') as f:
    fixes = json.load(f)

total_projects = 0
total_files = 0

for proj_fix in fixes:
    project_id = proj_fix['project_id']
    print(f"\n{project_id}")

    # Apply project annotation fixes
    if proj_fix.get('project_annotation_fixes'):
        ann = syn.restGET(f'/entity/{project_id}/annotations2')
        for k, v in proj_fix['project_annotation_fixes'].items():
            ann['annotations'][k] = {'type': 'STRING', 'value': v if isinstance(v, list) else [v]}
        syn.restPUT(f'/entity/{project_id}/annotations2', json.dumps(ann))
        print(f"  Project annotations updated: {list(proj_fix['project_annotation_fixes'].keys())}")
        total_projects += 1

    # Create wiki if missing
    if proj_fix.get('wiki_content'):
        try:
            syn.getWiki(project_id)
            print(f"  Wiki: already exists, skipping")
        except Exception:
            project = syn.get(project_id)
            syn.store(Wiki(
                title=project.name,
                owner=project_id,
                markdown=proj_fix['wiki_content'],
            ))
            print(f"  Wiki: created")

    # Apply file annotation fixes
    for file_fix in proj_fix.get('file_annotation_fixes', []):
        try:
            fe = syn.get(file_fix['file_id'], downloadFile=False)
            for k, v in file_fix['annotations'].items():
                fe.annotations[k] = v
            syn.store(fe)
            total_files += 1
        except Exception as e:
            print(f"  File {file_fix['file_id']} fix failed: {e}")

print(f"\nApply complete: {total_projects} projects, {total_files} files updated.")
```

---

### Audit Output Format

The full audit prints a report like this before JIRA notifications:

```
=== Self-Audit Report ===

syn74287500 — Clinical spectrum of NF1 missense variants
  Auto-fixes: studyStatus Active→Completed, fundingAgency set, project.pmid set
  Reasoning gaps: diseaseFocus, manifestation, studyLeads, institutions, wiki missing
  File fixes: resourceStatus×1, resourceType×1, fileFormat fastq.gz→fastq ×1
  File gaps: tumorType missing ×1, diagnosis missing ×1
  Warnings: 0

syn74287507 — Developmental loss of neurofibromin across neural circuits
  Auto-fixes: resourceStatus set on 20 files
  Reasoning gaps: none
  Warnings: 0

=== Audit Summary ===
Projects audited:   N
Auto-fixes applied: N
Reasoning gaps:     N (resolve in Phase 2, apply in Phase 3)
Warnings:           N
========================
```
