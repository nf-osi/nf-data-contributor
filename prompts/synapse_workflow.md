# Synapse Entity Creation & Annotation Workflow

This file contains the detailed Synapse entity creation steps, annotation vocabulary workflow, zip handling, ADD outcome pattern, wiki template, and schema binding. Read this file when creating or updating Synapse projects.

> **Configuration note:** All paths shown as `{WORKSPACE_DIR}/` in code examples represent the agent workspace directory. In every generated script, define `WORKSPACE_DIR` by reading `agent.workspace_dir` from `config/settings.yaml`, then use `os.path.join(WORKSPACE_DIR, 'filename.json')` for all intermediate file paths. Similarly, `TEAM_ID`, `SCHEMA_URI_PREFIX`, `METADATA_DICT_URL`, and `JIRA_PROJECT_KEY` must be read from `config/settings.yaml` — never hardcoded.

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
└── Source Metadata/
```

**Never create an `Analysis/` folder or any other placeholder folder that would be left empty.** Only create `Raw Data/` and `Source Metadata/` — both of which will be populated.

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

# Human-readable Dataset name — assay + specific biological context + repository/accession
# The name must be specific enough that a data manager can understand what the data is
# without opening the record. Pull as many meaningful context signals as available.
#
# Schema field names below are illustrative — check schema_props for the actual names
# in the bound schema (they may differ across dictionaries).
assay_label = normalized_annotations.get('assay', 'Data')
if isinstance(assay_label, list):
    assay_label = ', '.join(assay_label)

# Build a rich biological context string.
# Priority for context signals (use ALL that are available and add up to a useful description):
#   1. Assay target/antibody (for antibody-based assays: ChIP-seq, CUT&RUN, etc.)
#   2. Cell type or tissue type (most specific available)
#   3. Tumor type, diagnosis, or disease
#   4. Organism / model system (if non-obvious or if multiple species)
#   5. Experimental condition or treatment (if this is one arm of a multi-condition study)
# Use normalized_annotations to find these values — field names vary by schema.
context_parts = []

# Assay target (any field capturing antibody/ChIP target — check schema_props)
for key in ('assayTarget', 'antibodyTarget', 'target'):
    val = normalized_annotations.get(key)
    if val:
        context_parts.append(str(val) if not isinstance(val, list) else val[0])
        break

# Cell/tissue type — use most specific available
for key in ('cellType', 'tissueType', 'organ', 'bodyPart', 'tissue'):
    val = normalized_annotations.get(key)
    if val:
        context_parts.append(str(val) if not isinstance(val, list) else val[0])
        break

# Disease/tumor context if not already captured above
if len(context_parts) < 2:
    for key in ('tumorType', 'diagnosis', 'diseaseFocus'):
        val = normalized_annotations.get(key)
        if val:
            context_parts.append(str(val) if not isinstance(val, list) else val[0])
            break

# Organism if non-obvious (not human, or if multiple species present)
species_val = normalized_annotations.get('species') or normalized_annotations.get('organism')
if species_val:
    species_str = str(species_val) if not isinstance(species_val, list) else ', '.join(species_val)
    if 'sapiens' not in species_str.lower() or (isinstance(species_val, list) and len(species_val) > 1):
        context_parts.append(species_str)

context_str = f" — {', '.join(context_parts)}" if context_parts else ""

# Synapse name allows: letters, numbers, spaces, underscores, hyphens, periods,
# plus signs, apostrophes, parentheses — NO em-dash, NO colon
dataset_name = f"{assay_label}{context_str} ({repository} {accession_id})"
dataset_name = dataset_name[:256]  # Synapse name limit

# Description: one sentence covering what the data is, from where, and how many files
dataset_description = (
    f"{assay_label} data"
    + (f" ({species_label})" if species_label else "")
    + f" deposited in {repository} under accession {accession_id}. "
    f"{len(file_list)} file(s). "
    f"Part of: {project_name[:200]}"
)

# Create Dataset as direct child of the PROJECT (not Raw Data folder)
dataset_body = {
    'name': dataset_name,
    'description': dataset_description,
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

The Dataset columns must reflect the annotations that are actually on the files — nothing more, nothing less.
Derive them directly from the file annotations after all files have been annotated.
`facetType: "enumeration"` enables the Filter panel; set it on all columns except high-cardinality identifiers.

```python
import json

# High-cardinality identifier fields — skip faceting
HIGH_CARDINALITY = {'specimenID', 'individualID', 'externalAccessionID', 'name', 'id',
                    'sampleId', 'runAccession', 'biosampleId'}

# NOTE: resourceStatus, filename, and name are intentionally EXCLUDED.
# resourceStatus belongs only on Project and Dataset entities.
# filename and name both create duplicates of the Synapse system 'name' column —
# adding either as a custom annotation produces a confusing extra column in Dataset views.
EXCLUDE_COLS = {'resourceStatus', 'filename', 'name'}

# Collect all unique annotation keys from the files in this dataset
all_annotations = {}   # key -> annotation value object (for type inference)
for file_id in file_entity_ids:
    ann = syn.restGET(f'/entity/{file_id}/annotations2')
    for key, val_obj in ann.get('annotations', {}).items():
        if key not in all_annotations:
            all_annotations[key] = val_obj

# ── System columns (id, name) must ALWAYS be the first two columns ──────────
# Check if they already exist in the current entity's columnIds; create if absent.
ds_body = syn.restGET(f'/entity/{dataset_id}')
sys_col_map = {}  # col name -> col id
for cid in ds_body.get('columnIds', []):
    try:
        col_def = syn.restGET(f'/column/{cid}')
        if col_def.get('name') in ('id', 'name'):
            sys_col_map[col_def['name']] = cid
    except Exception:
        pass
if 'id' not in sys_col_map:
    c = syn.restPOST('/column', json.dumps({'name': 'id', 'columnType': 'ENTITYID'}))
    sys_col_map['id'] = c['id']
if 'name' not in sys_col_map:
    c = syn.restPOST('/column', json.dumps({'name': 'name', 'columnType': 'STRING', 'maximumSize': 256}))
    sys_col_map['name'] = c['id']

# ── Annotation columns (alphabetical, after system cols) ────────────────────
annotation_col_ids = []
for col_name in sorted(all_annotations):
    if col_name in EXCLUDE_COLS or col_name in ('id', 'name'):
        continue
    val_obj = all_annotations[col_name]
    ann_type = val_obj.get('type', 'STRING')
    if ann_type == 'DOUBLE':
        body = {'name': col_name, 'columnType': 'DOUBLE'}
    elif ann_type in ('LONG', 'INTEGER'):
        body = {'name': col_name, 'columnType': 'INTEGER'}
    else:
        values = val_obj.get('value', [])
        max_len = max((len(str(v)) for v in values), default=64)
        size = 500 if max_len > 250 else (256 if max_len > 128 else (128 if max_len > 64 else 64))
        body = {'name': col_name, 'columnType': 'STRING', 'maximumSize': size}
        if col_name not in HIGH_CARDINALITY:
            body['facetType'] = 'enumeration'
    col = syn.restPOST('/column', json.dumps(body))
    annotation_col_ids.append(col['id'])

# Final order: id → name → annotation columns (alphabetical)
col_ids = [sys_col_map['id'], sys_col_map['name']] + annotation_col_ids

ds_body = syn.restGET(f'/entity/{dataset_id}')
ds_body['columnIds'] = col_ids
syn.restPUT(f'/entity/{dataset_id}', json.dumps(ds_body))
```

### Step 5 — Bind metadata schema to the files folder and validate

```python
import time

js = syn.service('json_schema')
js.bind_json_schema(schema_uri, files_folder.id)   # ← bind to FILES FOLDER, not Dataset
time.sleep(3)
validation = js.validate(files_folder.id)
print(f"  Schema bound: {schema_uri}")
```

### Step 6 — Mint a stable snapshot version of the Dataset

After all annotations are confirmed correct, mint a stable version of the Dataset entity. This gives data managers a permanent, citable snapshot:

```python
import json

# Snapshot the Dataset — creates a permanent version number
snapshot_response = syn.restPOST(
    f'/entity/{dataset_id}/version',
    json.dumps({'label': 'v1', 'comment': 'Initial stable version from NADIA ingestion'})
)
snapshot_version = snapshot_response.get('versionNumber', 1)
print(f"  Dataset snapshot minted: {dataset_id}.{snapshot_version}")
```

**Required order within create_project.py:**
1. Create project → folders → File entities  (**no Dataset yet**)
2. Apply annotations to each individual File entity  (**must happen before Dataset linking** — each `syn.store(f)` or `annotations2` PUT increments the file version; if the Dataset is linked first it will point to pre-annotation versions and show blank columns)
3. Create the Dataset entity and link files  (**now version numbers reflect the annotated state**)
4. Apply annotations to Dataset entity
5. Set columnIds on Dataset entity
6. `bind_schema(syn, files_folder_id, schema_uri)` ← bind to FILES FOLDER
7. Mint stable version of Dataset entity (Step 6 above)
8. Print validation result

---

## Schema Selection — Dynamic, Not Hardcoded

1. Fetch available templates. Read `metadata_dictionary_url` and `uri_prefix` from `config/settings.yaml`:
```python
import httpx, yaml

with open('config/settings.yaml') as f:
    cfg = yaml.safe_load(f)
schema_cfg = cfg['synapse']['schema']
metadata_dict_url = schema_cfg['metadata_dictionary_url']
uri_prefix = schema_cfg['uri_prefix']

resp = httpx.get(metadata_dict_url, timeout=15)
available_templates = [f['name'].replace('.json', '') for f in resp.json() if f['name'].endswith('.json')]
```

2. Pick the best-matching template through reasoning (read the names, understand the dataset, select). **The template must match the primary data modality of the files being bound — not the paper title or disease context.** Verify the assay type from repository library metadata (ENA `library_strategy`, GEO `!Series_library_strategy`) before selecting. A multi-assay project (e.g., RNA-seq + ATAC-seq datasets in the same project) requires a different template for each files folder. Binding the wrong template (e.g., an RNA-seq template to chromatin accessibility files) applies incorrect validation rules and will miss required fields.

3. Convert to schema URI: lowercase and prepend the configured URI prefix:
```python
# e.g. uri_prefix='org.synapse.nf-', template='ScRNASeqTemplate'
# → 'org.synapse.nf-scrnaseqtemplate'
schema_uri = uri_prefix + template_name.lower()
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
    Return field_name → [valid_enum_values] for a registered metadata schema.
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

**Schema template selection — general decision process:**

1. Fetch the list of available templates from `synapse.schema.metadata_dictionary_url` in `config/settings.yaml`
2. Determine the assay type confirmed from source repository metadata (not just the paper abstract)
3. Match the assay to the most specific template available:
   - **Chromatin accessibility / histone marks** (ChIP-seq, CUT&RUN, CUT&TAG, ATAC-seq): use an epigenomics-specific template if one exists. These typically add `assayTarget`, `assayTargetDescription`, and `referenceSet` (genome build) fields not found in general templates. Identify the assay target from GEO `!Series_extract_protocol_ch1`, ENA `library_selection`, or the experiment title.
   - **Behavioral / phenotype data** (open field, rotarod, clinical questionnaires): use a behavioral template if available. These often require compound/drug fields — for non-drug studies, set compound fields to `'Not Applicable'` / `'0'` / `'Not Applicable'`.
   - **Single-cell data** (scRNA-seq, snATAC-seq, scADT): use a single-cell template if available. Verify `library_source = 'TRANSCRIPTOMIC SINGLE CELL'` or protocol mentions 10x Chromium / Drop-seq / Smart-seq2 before using.
   - **Mass spectrometry / proteomics**: use a proteomics template if available.
   - **General / fallback**: use the broadest available template (often one with the largest `assay` enum) when no assay-specific template matches.
4. After selection, always call `fetch_schema_properties(schema_uri)` to get the actual field list for that template — never assume field names from a different template carry over.
5. If the assay enum in the selected template does not contain the study's assay type, try the next most general template before falling back. Record which template was selected and why in the curation comment.

**Template selection must be based on the source repository's library metadata** (ENA `library_strategy`, GEO `!Series_instrument_model`/`!Sample_library_strategy`), not inferred from the publication title. The title may describe the biology; the repository fields describe the technology.

### Step A2 — Fetch ALL schema property names (not just enums)

After fetching enums, also collect all property names the schema defines. This determines what fields you CAN set — do not hardcode field names anywhere.

```python
def fetch_schema_properties(schema_uri: str) -> dict:
    """
    Return a dict of all property names defined in the schema.
    Keys = field names. Values = dict with 'type', 'enum' (if constrained), 'description'.
    Use this to know which fields exist — then populate as many as source data supports.
    """
    url = f'https://repo-prod.prod.sagebase.org/repo/v1/schema/type/registered/{schema_uri}'
    resp = httpx.get(url, timeout=15)
    resp.raise_for_status()
    schema = resp.json()

    props = {}
    def collect_props(obj):
        if not isinstance(obj, dict):
            return
        for field_name, field_def in obj.get('properties', {}).items():
            if not isinstance(field_def, dict):
                continue
            entry = {}
            if 'enum' in field_def:
                entry['enum'] = field_def['enum']
                entry['type'] = 'enum'
            else:
                for sub_key in ('anyOf', 'oneOf'):
                    for sub in field_def.get(sub_key, []):
                        if 'enum' in sub:
                            entry['enum'] = sub['enum']
                            entry['type'] = 'enum'
                if 'type' not in entry:
                    entry['type'] = field_def.get('type', 'string')
            entry['description'] = field_def.get('description', '')
            props[field_name] = entry
        for defs_key in ('definitions', '$defs'):
            for defn in obj.get(defs_key, {}).values():
                collect_props(defn)
    collect_props(schema)
    return props

# Usage:
schema_props = fetch_schema_properties(schema_uri)
# schema_props is the ground truth — only set fields that appear here
print(f"  Schema has {len(schema_props)} properties: {sorted(schema_props.keys())}")
```

### Step B — Extract raw metadata from ALL source types

Gather raw metadata comprehensively before normalizing. Dump into a flat `raw_metadata` dict.

**GEO datasets** — fetch the full SOFT file (not brief):
```python
import httpx

def fetch_geo_full_soft(gse: str) -> dict:
    """Download full SOFT and extract all series + first-sample characteristics."""
    raw = {}
    # Series-level metadata
    r = httpx.get(
        f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi'
        f'?acc={gse}&targ=self&form=text&view=full',
        timeout=30
    )
    series_fields = {}
    for line in r.text.split('\n'):
        if line.startswith('!Series_'):
            key, _, val = line.partition(' = ')
            series_fields.setdefault(key.strip(), []).append(val.strip())
    raw['geo_series'] = series_fields

    # All samples — full characteristics
    r2 = httpx.get(
        f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi'
        f'?acc={gse}&targ=gsm&form=text&view=full',
        timeout=60
    )
    samples = {}
    cur_gsm, cur_chars = None, {}
    for line in r2.text.split('\n'):
        if line.startswith('^SAMPLE = '):
            if cur_gsm:
                samples[cur_gsm] = cur_chars
            cur_gsm = line.split(' = ', 1)[1].strip()
            cur_chars = {}
        if cur_gsm:
            ll = line.lower()
            if '!sample_characteristics_ch1' in ll:
                kv = line.split(' = ', 1)[-1].strip()
                if ': ' in kv:
                    k, v = kv.split(': ', 1)
                    cur_chars[k.strip().lower()] = v.strip()
                else:
                    cur_chars.setdefault('_raw', []).append(kv)
            for field in ('!sample_source_name_ch1', '!sample_organism_ch1',
                          '!sample_instrument_model', '!sample_library_strategy',
                          '!sample_library_selection', '!sample_library_source',
                          '!sample_extract_protocol_ch1', '!sample_treatment_protocol_ch1',
                          '!sample_data_processing'):
                if line.lower().startswith(field):
                    raw[field.lstrip('!')] = line.split(' = ', 1)[-1].strip()
    if cur_gsm:
        samples[cur_gsm] = cur_chars
    raw['geo_samples'] = samples
    return raw
```

**SRA / ENA datasets** — fetch XML metadata:
```python
def fetch_sra_metadata(srp_or_prj: str) -> dict:
    """Fetch SRA study + run metadata via ENA API."""
    raw = {}
    # Filereport gives per-run metadata in TSV
    r = httpx.get(
        'https://www.ebi.ac.uk/ena/portal/api/filereport',
        params={
            'accession': srp_or_prj,
            'result': 'read_run',
            'fields': 'run_accession,sample_accession,experiment_accession,'
                      'instrument_model,instrument_platform,library_strategy,'
                      'library_selection,library_source,library_layout,'
                      'read_count,base_count,sample_title,scientific_name,'
                      'tax_id,center_name,study_title,experiment_title,'
                      'sample_alias,broker_name,fastq_ftp,submitted_ftp,'
                      'study_accession',
            'format': 'tsv',
            'download': 'true',
        },
        timeout=30
    )
    if r.status_code == 200 and r.text.strip():
        lines = r.text.strip().split('\n')
        headers = lines[0].split('\t')
        raw['sra_runs'] = [dict(zip(headers, row.split('\t'))) for row in lines[1:]]
        if raw['sra_runs']:
            # Use first run as representative
            raw['representative_run'] = raw['sra_runs'][0]
    return raw
```

**Zenodo / Figshare / OSF datasets** — already available as JSON from repository API during discovery; re-use stored record metadata.

**PubMed** — fetch full record when PMID available:
```python
from Bio import Entrez

def fetch_pubmed_full(pmid: str) -> dict:
    handle = Entrez.efetch(db='pubmed', id=pmid, rettype='xml', retmode='xml')
    recs = Entrez.read(handle)
    if not recs.get('PubmedArticle'):
        return {}
    art = recs['PubmedArticle'][0]
    citation = art['MedlineCitation']
    article = citation['Article']
    return {
        'title': str(article.get('ArticleTitle', '')),
        'abstract': ' '.join(str(x) for x in article.get('Abstract', {}).get('AbstractText', [])),
        'keywords': [str(k) for k in citation.get('KeywordList', [[]])[0]] if citation.get('KeywordList') else [],
        'mesh_terms': [str(m['DescriptorName']) for m in citation.get('MeshHeadingList', [])],
        'grants': [str(g.get('Agency', '')) for g in article.get('GrantList', [])],
        'authors': [
            f"{a.get('LastName', '')} {a.get('ForeName', '')}".strip()
            for a in article.get('AuthorList', [])
        ],
        'affiliations': [
            str(aff) for a in article.get('AuthorList', [])
            for aff in a.get('AffiliationInfo', [{}])
            if aff.get('Affiliation')
        ],
    }
```

### Step C — Populate ALL schema properties via gap-fill

**Read `prompts/annotation_gap_fill.md` for the complete algorithm.** This step runs the full gap-fill strategy — not just the primary source, but every available upstream source — before applying any annotations to File entities.

The process:
1. Start with `schema_props = fetch_schema_properties(schema_uri)` and `raw_metadata` from Step B
2. Classify each schema field into a category (technical/library, biological/sample, study-level, identifier, model system) — see Field Categories in `prompts/annotation_gap_fill.md`
3. For each field, work through the source priority list for its category (Tier 1 → Tier 2 → Tier 3 → Tier 4) until a valid value is found
4. Validate every extracted value against the field's enum constraint before accepting it
5. Build `per_file_annotations` (fields that vary per file) and `study_level_annotations` (fields uniform across all files)
6. Merge into `normalized_annotations` keyed only by fields present in `schema_props`
7. Record what tier each value came from in `normalized_annotations_sources.json`

```python
import json

# After running the gap-fill algorithm from prompts/annotation_gap_fill.md:
# normalized = {field: value}  — only fields in schema_props, all enum-validated
# per_file   = {run_accession: {field: value}}  — per-sample varying values
# gap_report = {filled_tier1: [...], filled_tier2: [...], gap_not_in_source: [...], ...}

with open(f'{WORKSPACE_DIR}/normalized_annotations.json', 'w') as f:
    json.dump(normalized, f, indent=2)
with open(f'{WORKSPACE_DIR}/normalized_annotations_sources.json', 'w') as f:
    json.dump({'study_level': normalized, 'per_file': per_file, 'gap_report': gap_report}, f, indent=2)
print(f"  Normalized {len(normalized)} study-level + {len(per_file)} per-file annotation fields")
print(f"  Gap fill: T1={len(gap_report['filled_tier1'])} T2={len(gap_report['filled_tier2'])} "
      f"T3={len(gap_report['filled_tier3'])} T4={len(gap_report['filled_tier4'])} "
      f"unfilled={len(gap_report['gap_not_in_source'])}")
```

**Key constraints:**
- **Never hardcode field names** — always derive available fields from `schema_props = fetch_schema_properties(schema_uri)` at runtime
- Only set enum-constrained fields to values that appear in the schema's enum list (use `validate_against_enum()` from `prompts/annotation_gap_fill.md`)
- Populate as many schema fields as source data supports — more is better; omitting a field is fine, inventing one is not
- Per-sample identifier fields (fields whose description indicates specimen, individual, or sample identity) must have one unique value per file — never a list or a shared value across files
- File format fields must strip compression suffixes before storing (e.g. `fastq.gz` → `fastq`, `txt.gz` → `txt`)
- All controlled-vocabulary fields: use exact enum values from the schema — spacing and capitalization matter
- Fields with empty enum (`"enum": []`): do not set — no valid value exists in the current schema version

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

**Shared across all files — two-tier annotation approach:**

**Step D (here, in synapse_actions.py) sets only directly-readable structured fields.** These are values you can copy verbatim from a structured metadata column without any interpretation.

**Set in Step D** — the schema field that captures each concept below ← from its source column:
- Assay / data type ← ENA `library_strategy`, GEO `!Series_library_strategy`
- Organism / species ← ENA `scientific_name`, GEO `!Series_sample_organism`
- Sequencing instrument / platform ← ENA `instrument_model`, GEO `!Series_instrument_model`
- Library layout / run type ← ENA `library_layout` (PAIRED / SINGLE — verify enum values match the schema)
- Read depth / read count ← ENA `read_count` (per-file)
- Read length ← ENA `nominal_length` (per-file, if available)
- File format ← filename extension (strip .gz/.bz2/.zip)
- Specimen or biological sample identifier ← ENA `sample_title` or `sample_alias`; GSM ID from GEO
- External accession and repository identity ← from the known accession and hosting repository
- Data subtype / processing level ← inferred from file extension (fastq/bam/vcf → raw; tsv/txt/h5/csv → processed)
- Resource type ← the constant value for experimental data defined in the schema enum

**The rule:** the field names above are illustrative. At runtime, call `fetch_schema_properties(schema_uri)` and use the field descriptions to identify which schema field captures each concept. Only set a field if its value is a verbatim copy of a structured column — no mapping, no interpretation.

**Defer to Step 7b gap-fill** — do not set in Step D:
- Any field whose value requires interpreting a protocol or kit name string to select an enum value (e.g., strand orientation inferred from the library preparation kit name)
- Any field whose value requires biological reasoning applied to a sample description (e.g., disease classification, model organism genotype, experimental condition)
- Any field whose value requires reading a methods section, supplementary table, or publication abstract
- Any field that is not directly available as a column in ENA filereport or GEO SOFT

If you set a field in Step D based on reasoning (not a direct column read), you bake a guess into the file before the structured gap-fill tiers can evaluate it properly. The gap-fill audit (Step 7b) only fills gaps — it will not re-examine or correct a field that is already set. **Set it wrong in Step D and it stays wrong.**

> **CRITICAL — NEVER set on File entities:**
> - **`resourceStatus`** — belongs ONLY on the Project and Dataset entity annotations. Setting it on files causes the Datasets tab to show a `resourceStatus` column with values that data managers must manually remove.
> - **`filename`** — do NOT add a custom `filename` annotation. The Synapse system `name` property (set automatically from the file entity's name) IS the filename column in Dataset views. Adding `filename` as a custom annotation creates a duplicate, non-system column that data managers must remove.

**Model organism shared fields** — call `fetch_schema_properties(schema_uri)` and populate every model organism field the schema defines (typically: species, sex, age, age unit, system/strain name, and any disease-specific genotype keys). Fetch values from repository sample attributes at creation time — do not leave blank.

**Assay-target fields** — for any ChIP-seq, CUT&RUN, or antibody-based assay, check `schema_props` for a target/antibody field and populate it from GEO `!Series_extract_protocol_ch1` or ENA `library_selection`/experiment title. This enables faceted filtering by target in the portal.

**Per-file fields** are those the schema defines as varying per sample — use `fetch_schema_properties()` to identify them. Common per-file concepts: file format, per-sample identifiers, per-sample biological attributes (age, sex, genotype, condition), read depth, read length.

> **Per-sample identifier fields:** Use the biological sample identifier — not the sequencing run accession. For ENA/SRA files, the biological identifier is found in `sample_title`, `sample_alias`, or BioSample accession (SAMN/SAME/SAMD) from the ENA filereport. Run accessions (SRR, ERR, DRR) identify sequencing runs, not biological individuals, and must never be used for specimen or individual identity fields. For studies with a patient/sample naming convention (e.g. from supplementary tables), derive individual IDs from that nomenclature.

> **Fields with an empty enum:** Before setting any field from `fetch_schema_properties()`, check whether the field's enum list is empty. If `"enum": []`, do not set that field — an empty enum means no valid value exists in the current schema version.

> **External hosting repository fields:** Any schema field that captures where files are physically hosted must reflect the actual file host, not the study discovery path. If files are stored in ENA/SRA and the study was discovered via a GEO link, the hosting repository field value must be 'ENA' or 'SRA' — GEO is a study metadata portal, not a file host.

---

## Project-Level Annotations

Read `curation_checklist.required_project_annotations` from `config/settings.yaml` to get the field names for your portal. Build the annotations dict dynamically from those field names and the populated values. The pattern below illustrates the approach — substitute actual field names from config:

```python
import yaml

with open('config/settings.yaml') as f:
    cfg = yaml.safe_load(f)

required_fields = cfg['curation_checklist']['required_project_annotations']
# 'populated_values' is a dict you built from source metadata:
#   { field_name: value_or_list, ... }
# Match each required field to its populated value.

ann = syn.restGET(f'/entity/{project_id}/annotations2')
ann['annotations'] = {}
for field in required_fields:
    val = populated_values.get(field)
    if val is not None:
        if not isinstance(val, list):
            val = [val]
        ann['annotations'][field] = {'type': 'STRING', 'value': val}

# pmid and doi are always set if available (standard regardless of schema)
if pmid:
    ann['annotations']['pmid'] = {'type': 'STRING', 'value': [str(pmid)]}
if doi:
    ann['annotations']['doi']  = {'type': 'STRING', 'value': [str(doi)]}

syn.restPUT(f'/entity/{project_id}/annotations2', json.dumps(ann))
```

**Funder extraction — check in order:**
1. **PubMed GrantList** (most reliable): `grants = art.get('GrantList', [])` → `{g.get('Agency') for g in grants}`
2. **Acknowledgements section**: look for "funded by", "supported by" phrases
3. **Repository metadata**: Zenodo records sometimes list funders in `metadata.grants`
4. **Fallback**: a "not applicable" placeholder — set the specific string from your portal's controlled vocabulary

**Study completion status**: Always set to the "completed" value for published studies with deposited data. Never "active" or "in progress" for public archival deposits.

**Assay/data type vocabulary**: Read valid values from the schema enum at runtime — do not hardcode. Infer the category from the repository's library strategy or assay description.

---

## Post-Curation GitHub Comment

After annotating a project, post a comment on its study-review GitHub issue. This is required — it is the handoff to human reviewers. Use `scripts/github_issue.py`'s `post_issue_comment(issue_number, body)`.

Structure the comment as:

```markdown
## Curation Summary

**Schema template:** `{schema_uri}` (selected because: {reason})
**Source metadata fetched from:** {repositories queried}

### Annotations set

| Field | Value | Source |
|-------|-------|--------|
| studyLeads | {value} | PubMed AuthorList / BioStudies / repository |
| species | {value} | ENA scientific_name / GEO organism field |
| assay | {value} | ENA library_strategy / GEO library_strategy |
| platform | {value} | ENA instrument_model / GEO instrument_model |
| ... | ... | ... |

### Approximations and gaps

- **{field}**: Source value "{raw_value}" is not in schema enum. Used closest match "{chosen_value}". Consider adding "{raw_value}" to the vocabulary.
- (or: No gaps — all field values matched schema enums exactly)

### Items for human review

- [ ] {Any ambiguity that requires a human decision}
- [ ] {Any field that could not be populated}
- [ ] {Any mismatch between paper description and repository metadata}
```

Omit sections that have no content (e.g., omit "Approximations and gaps" if none). Keep the comment factual and actionable — data managers use it to decide whether to approve or request fixes.

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
    # Use same human-readable naming as NEW projects (see Step 2 above)
    ds_body = syn.restPOST('/entity', json.dumps({
        'name': dataset_name,           # e.g. "RNA-seq (ENA PRJEB77277)"
        'description': dataset_description,
        'parentId': existing_project_id,   # ← project root, not Raw Data
        'concreteType': 'org.sagebionetworks.repo.model.table.Dataset',
    }))
    ds_id = ds_body['id']
    # Link files as dataset items, set columnIds, annotate (same as Steps 2-4 above)
```

If the existing project is portal-managed (not in agent state table), **do not write to it**. Post a comment on the existing study-review issue (or create a new one) flagging the new accession as a manual action required.

---

## Zip Files — Flag for Interactive Processing

When a repository contains `.zip` files, do NOT attempt to download/extract in automated runs:

1. Create a `File` entity pointing to the zip's direct download URL (as normal)
2. Add annotation `needsExtraction: true` to that File entity
3. Note the zip in the post-curation GitHub comment under "Items for human review" with the label `interactive-processing-required`
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
        import httpx, yaml
        with open('config/settings.yaml') as f:
            _cfg = yaml.safe_load(f)
        jira_project_key = _cfg['notifications']['jira']['project_key']
        payload = {
            'fields': {
                'project': {'key': jira_project_key},
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
    pub_parts = []
    if pmid:
        pub_parts.append(f'[PMID:{pmid}](https://pubmed.ncbi.nlm.nih.gov/{pmid}/)')
    if doi:
        pub_parts.append(f'[DOI:{doi}](https://doi.org/{doi})')
    pub_line = ' · '.join(pub_parts) if pub_parts else 'Not available'
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
**Publication:** {pub_line}
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

*This project was auto-curated by [NADIA](https://github.com/nf-osi/nadia) on {today} and is pending data manager review.*
"""
```

**Plain-language summary guidance** (write directly through reasoning, no API call):
- Sentence 1: the disease/condition studied and why it matters
- Sentence 2: what data was generated (assay, model system, experimental design)
- Sentence 3: what was found or what the dataset enables

**Wiki title**: use the full publication title (not "Auto-Discovered..." or similar). Do not add "Auto-Discovered" to the title or anywhere in the wiki header.

---

## Source Metadata Folder Population

Every agent-created project has a `Source Metadata/` folder. **This folder must be populated** with all available original metadata files from the source repository. It serves as a permanent reference for future re-extraction and auditing.

### What to store

| Source | Files to create in Source Metadata/ |
|--------|--------------------------------------|
| GEO | `{GSE}_series_matrix.txt` (SOFT series-level), `{GSE}_sample_characteristics.tsv` (tabular sample chars), `{GSE}_abstract.txt` |
| SRA/ENA | `{accession}_filereport.tsv` (ENA filereport TSV), `{accession}_study.xml` (ENA study XML if available) |
| Zenodo | `{record_id}_metadata.json` (full Zenodo record JSON), `{record_id}_description.txt` |
| Figshare | `{article_id}_metadata.json` (Figshare article JSON) |
| OSF | `{project_id}_metadata.json` (OSF project JSON) |
| Any | `pubmed_{pmid}_abstract.txt` (PubMed abstract + title + authors), `pubmed_{pmid}_mesh.txt` (MeSH terms and keywords) |

Include ALL sample characteristics even if they are not currently mapped to schema fields — they may support future annotation expansion.

### How to create these files

```python
import os, json, csv, tempfile
from synapseclient import File

def store_source_metadata_file(syn, content: str | bytes, filename: str,
                                source_metadata_folder_id: str,
                                description: str = '') -> str:
    """Write content to a temp file and store in Synapse Source Metadata/ folder."""
    tmp = f'{WORKSPACE_DIR}/source_meta_{filename}'
    mode = 'wb' if isinstance(content, bytes) else 'w'
    with open(tmp, mode, encoding=None if isinstance(content, bytes) else 'utf-8') as f:
        f.write(content)
    fe = syn.store(File(
        path=tmp,
        name=filename,
        parentId=source_metadata_folder_id,
        synapseStore=True,
    ))
    print(f"  Stored source metadata: {filename} → {fe.id}")
    return fe.id


def populate_geo_source_metadata(syn, gse: str, source_metadata_folder_id: str):
    """Fetch and store GEO SOFT + sample characteristics + abstract."""
    import httpx

    # 1. Full SOFT series matrix
    r = httpx.get(
        f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi'
        f'?acc={gse}&targ=self&form=text&view=full',
        timeout=30
    )
    if r.status_code == 200:
        store_source_metadata_file(syn, r.text, f'{gse}_series_soft.txt',
                                   source_metadata_folder_id)

    # 2. Sample characteristics as TSV
    r2 = httpx.get(
        f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi'
        f'?acc={gse}&targ=gsm&form=text&view=full',
        timeout=60
    )
    if r2.status_code == 200:
        # Parse into tabular form: columns = all characteristic keys; rows = samples
        samples = {}
        all_keys = set()
        cur_gsm, cur_chars = None, {}
        for line in r2.text.split('\n'):
            if line.startswith('^SAMPLE = '):
                if cur_gsm:
                    samples[cur_gsm] = cur_chars
                    all_keys.update(cur_chars.keys())
                cur_gsm = line.split(' = ', 1)[1].strip()
                cur_chars = {}
            if cur_gsm and '!sample_characteristics_ch1' in line.lower():
                kv = line.split(' = ', 1)[-1].strip()
                if ': ' in kv:
                    k, v = kv.split(': ', 1)
                    cur_chars[k.strip().lower()] = v.strip()
        if cur_gsm:
            samples[cur_gsm] = cur_chars
            all_keys.update(cur_chars.keys())

        if samples:
            import io
            buf = io.StringIO()
            keys = sorted(all_keys)
            writer = csv.DictWriter(buf, fieldnames=['gsm'] + keys, extrasaction='ignore')
            writer.writeheader()
            for gsm, chars in samples.items():
                writer.writerow({'gsm': gsm, **chars})
            store_source_metadata_file(syn, buf.getvalue(),
                                       f'{gse}_sample_characteristics.tsv',
                                       source_metadata_folder_id)

        # Also store raw GSM SOFT for completeness
        store_source_metadata_file(syn, r2.text, f'{gse}_samples_soft.txt',
                                   source_metadata_folder_id)


def populate_sra_source_metadata(syn, accession: str, source_metadata_folder_id: str):
    """Fetch and store ENA filereport TSV."""
    import httpx
    r = httpx.get(
        'https://www.ebi.ac.uk/ena/portal/api/filereport',
        params={
            'accession': accession,
            'result': 'read_run',
            'fields': 'run_accession,sample_accession,experiment_accession,'
                      'instrument_model,instrument_platform,library_strategy,'
                      'library_selection,library_source,library_layout,'
                      'read_count,base_count,sample_title,scientific_name,'
                      'tax_id,center_name,study_title,experiment_title,'
                      'sample_alias,fastq_ftp,submitted_ftp,study_accession',
            'format': 'tsv',
            'download': 'true',
        },
        timeout=30
    )
    if r.status_code == 200 and r.text.strip():
        store_source_metadata_file(syn, r.text, f'{accession}_filereport.tsv',
                                   source_metadata_folder_id)


def populate_pubmed_source_metadata(syn, pmid: str, source_metadata_folder_id: str,
                                    pub_data: dict):
    """Store abstract, MeSH terms, and author metadata from PubMed."""
    text = f"Title: {pub_data.get('title', '')}\n\n"
    text += f"Authors: {'; '.join(pub_data.get('authors', []))}\n\n"
    text += f"Abstract:\n{pub_data.get('abstract', '')}\n\n"
    text += f"Affiliations:\n" + '\n'.join(pub_data.get('affiliations', [])) + '\n'
    store_source_metadata_file(syn, text, f'pubmed_{pmid}_abstract.txt',
                               source_metadata_folder_id)

    mesh_text = "MeSH Terms:\n" + '\n'.join(pub_data.get('mesh_terms', [])) + '\n\n'
    mesh_text += "Keywords:\n" + '\n'.join(pub_data.get('keywords', [])) + '\n'
    store_source_metadata_file(syn, mesh_text, f'pubmed_{pmid}_mesh.txt',
                               source_metadata_folder_id)


def populate_zenodo_source_metadata(syn, record_id: str, source_metadata_folder_id: str,
                                     record_json: dict):
    """Store full Zenodo record JSON and description text."""
    store_source_metadata_file(syn, json.dumps(record_json, indent=2),
                               f'{record_id}_metadata.json',
                               source_metadata_folder_id)
    desc = record_json.get('metadata', {}).get('description', '') or \
           record_json.get('description', '')
    if desc:
        store_source_metadata_file(syn, desc, f'{record_id}_description.txt',
                                   source_metadata_folder_id)
```

### Integration with project creation

Call the appropriate `populate_*_source_metadata()` function immediately after the Source Metadata/ folder is created, before schema binding:

```python
# Find Source Metadata folder
source_meta_folder = next(
    (c for c in syn.getChildren(project_id, includeTypes=['folder'])
     if c['name'] == 'Source Metadata'),
    None
)
if source_meta_folder:
    smf_id = source_meta_folder['id']
    if source_repository == 'GEO':
        populate_geo_source_metadata(syn, accession_id, smf_id)
    elif source_repository in ('SRA', 'ENA'):
        populate_sra_source_metadata(syn, accession_id, smf_id)
    if pmid:
        populate_pubmed_source_metadata(syn, pmid, smf_id, pubmed_data)
    if source_repository == 'Zenodo':
        populate_zenodo_source_metadata(syn, accession_id, smf_id, zenodo_record)
```

---

## `bind_schema()` Helper

**Checking if a schema is already bound** — two valid approaches:
- REST: `syn.restGET(f'/entity/{folder_id}/schema/binding')` — returns the binding info (schema `$id`, version). Use this when you only need to know *what* schema is bound.
- Python SDK: `syn.service('json_schema').validate(folder_id)` — validates annotations against the bound schema and returns the schema `$id`. Use this when you want to validate at the same time.
- Do NOT use `/entity/{id}/jsonschema/binding` — that endpoint does not exist and returns 404.

```python
def get_bound_schema_uri(syn, folder_id: str) -> str | None:
    """Returns schema URI if a schema is bound to this folder, else None."""
    try:
        binding = syn.restGET(f'/entity/{folder_id}/schema/binding')
        return binding.get('jsonSchemaVersionInfo', {}).get('$id')
    except Exception:
        return None  # 404 means no schema bound
```

```python
import time, httpx

def bind_schema(syn, files_folder_id: str, schema_uri: str) -> dict:
    """Bind a chosen metadata schema to a dataset files folder and validate."""
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

These issues were discovered when the audit was run on real agent-created projects (2026-03-28) and are not always caught at creation time. The `→ Standard N` references point to the corresponding rule in the **Annotation Quality Standards** section of CLAUDE.md.

1. **All file annotations were missing** — projects created before the annotation step was enforced had zero file-level annotations. The audit auto-fix sweep adds the mechanical fields; the reasoning pass adds domain-knowledge fields. → Standard 1 (fetch schema first)

2. **`manifestation='Unspecified'` is not valid portal vocabulary** — treat it the same as missing. Check the `annotations.manifestation_values` list in `config/settings.yaml` for valid values. → Standard 9 (vocabulary gaps: flag, don't silently drop)

3. **`studyLeads=['Unknown']` — search PubMed first** — ENA projects without a PMID often have an associated publication findable via title search. Include `instrument_model`, center name, and key terms in the query. → Standard 3 (investigator fields: use paper authors, not repository submitters)

4. **Schema enum extraction must use the `properties` layer** — a naive recursive search for any dict with `'enum'` key picks up enum values from unrelated sub-objects (e.g. a clinical questionnaire block inside the behavioral template returned `'Child Behavior Checklist for Ages 1.5-5'` as the assay value for Drosophila grooming data). Always traverse via `schema['properties']`. → Standard 1 (schema enums are ground truth)

5. **`{uri_prefix}behavioralassaytemplate` covers both human clinical and animal model behavioral data** — its assay enum includes animal tests (open field, rotarod, elevated plus maze, etc.) alongside clinical questionnaires. It always requires `compoundName`/`compoundDose`/`compoundDoseUnit`; set these to `'Not Applicable'`/`'0'`/`'Not Applicable'` for non-drug studies. Also requires `dataType` — use `'behavioral data'`. `{uri_prefix}biologicalassaydatatemplate` has no enum constraints at all and should be avoided. → Standard 6 (assay subtype fields: verify from source metadata)

6. **Dataset `columnIds` may be stale or missing** — projects created before this approach was adopted may have no `columnIds` or a hardcoded subset. Rebuild them dynamically from the actual file annotations (see Step 4).

7. **Schema binding was missing on all ENA/Zenodo projects** — the binding step was added to the creation workflow but not retroactively applied. The audit catches and fixes this. → Standard 1 (schema is ground truth — must be bound)

8. **Dataset item versions go stale after annotation fixes** — every `syn.store(f)` or `annotations2` PUT increments the file version. If the audit auto-fix applies file annotations, the Dataset items end up pointing to the pre-fix version and the Datasets tab shows blank annotation columns. The version-sync check in step 4c catches and corrects this. The root fix is to always annotate files BEFORE linking them to the Dataset (see Required order above).

9. **Investigator fields must be first + last/corresponding author — not the repository submitter** — ENA/ArrayExpress submitters are typically research engineers or postdocs who performed the experiment, not the PI. The BioStudies `[Author]` section lists role (`submitter`, `experiment performer`, `principal investigator`). If only a submitter is present, search bioRxiv for a preprint using the model name + assay + institution. If a preprint is found, derive the investigator field from its author list (first + last). Never default to the submitter name without checking for a publication or preprint. → Standard 3

10. **Organism/species fields must be verified from the repository taxon field, never inferred** — A dataset can use human, mouse, zebrafish, or Drosophila regardless of the disease focus. Always read `scientific_name` (ENA filereport), `!Series_sample_taxid` (GEO SOFT), or `Organism` (BioStudies). A dataset submitted by a mouse lab may contain human cell data (e.g. patient-derived iPSCs or bone marrow aspirates). This was the root cause of species being set to Mus musculus on a human scRNA-seq dataset (GSE196652). → Standard 4

11. **Assay type fields must reflect single-cell vs bulk** — When `library_source = 'TRANSCRIPTOMIC SINGLE CELL'`, `library_strategy` mentions scRNA, or the protocol names 10x Chromium / Fluidigm C1 / Drop-seq / Smart-seq2 (per-cell), the assay is `'single-cell RNA-seq'`, NOT `'RNA-seq'`. Setting bulk RNA-seq on a scRNA-seq dataset causes the wrong schema to be selected, which cascades to wrong annotation fields and wrong Curator Grid validation. → Standard 6

12. **Schema-specific fields must be populated at creation, not as a post-hoc fix** — After binding the schema, call `fetch_schema_properties(schema_uri)` to get the full list of fields the schema defines. For each property present in the source repository metadata (sample attributes, sample characteristics, protocol/library fields), populate it. Common field groups to look for: model organism descriptors (age, sex, genotype, species, system name), library prep details (dissociation method, specimen preparation, cell line flag, run type, read pair, library prep method, strand), and any disease-specific genotype or treatment fields. Never leave a schema property blank if the source data contains the corresponding value. → Standard 1

13. **Dataset columns must reflect actual file annotations** — Do not use a hardcoded column list. After annotating all files, derive `columnIds` directly from the annotation keys present on those files (see Step 4). This ensures the Dataset view shows exactly the columns that have data, no more and no less. → Standard 1

14. **Resource/review status must NOT be set on individual File entities** — Data managers have consistently requested removal of this annotation from files. It belongs only on the **Project** and **Dataset entity** (as an entity-level annotation). The audit Phase 1 auto-fix now **removes** it from any file that has it. Do not set it during creation either.

15. **`filename` or `name` annotation causes duplicate columns** — Do NOT add a custom `filename` or `name` annotation to files. The Synapse system `name` property serves as the entity name and filename in Dataset views. Adding either as a custom annotation creates a second column that data managers must manually clean up, and makes the real system `name` column appear empty or broken. The audit Phase 1 auto-fix removes both `filename` and `name` annotations from files. Both fields are in `EXCLUDE_COLS`.

16. **Dataset names must be descriptive and specific** — Names like `GEO_GSE120686` are not useful. The Dataset name is the first thing a data manager sees in the Datasets tab, so it should convey what the data actually is without having to open the record.

    Recommended format: `{assay} — {specific biological context} ({repository} {accession_id})`

    The "specific biological context" should include enough information to distinguish this dataset from others in the same project or portal:
    - Tissue type, cell type, or tumor type (not just the disease name)
    - Organism or model system if not obvious from context
    - Assay target for antibody-based assays (ChIP-seq, CUT&RUN, etc.)
    - Study condition or treatment if the dataset is one arm of a multi-condition study

    Examples of **insufficient** names: `"RNA-seq data (GEO GSE120686)"`, `"tumor samples (ENA PRJEB12345)"`

    Examples of **sufficient** names: `"RNA-seq — peripheral blood mononuclear cells, treatment vs. control (GEO GSE120686)"`, `"ChIP-seq H3K27ac — Schwann cells (ENA PRJEB12345)"`, `"Whole exome sequencing — patient-derived xenograft lines (SRA SRP123456)"`

17. **Mint a stable Dataset version after all annotations are final** — After creating or updating a Dataset and confirming all annotations are correct, call `syn.restPOST(f'/entity/{dataset_id}/version', ...)` to mint a permanent snapshot. This gives data managers a stable, citable version to reference. Data managers will request this explicitly if it is missing. The polish workflow (Step 7) and the daily creation workflow both must mint versions as a final step after annotations are confirmed.

18. **Sample-varying fields set to a single study-level value on all files** — After initial annotation, check whether any field that can vary by sample (genotype, condition, sex, age, tissue, cell type, preparation method, or any treatment/perturbation field) has the same value on every file in the dataset. If the study has multiple experimental groups, this is almost always wrong — the study-level value was copied to all files instead of mapping each file to its source sample. Fix by: (a) fetching the per-sample metadata record for each file's run/sample accession, (b) mapping each file to its sample, (c) re-applying those fields per-file with the correct per-sample value. This is the second thing to check in the audit after mechanical field presence — it is a correctness error, not just a completeness gap. → Standard 5

19. **Schema completeness check at audit time** — Phase 2 (agent reasoning) must include an explicit schema coverage step: call `fetch_schema_properties(schema_uri)` on the bound schema, list every property that is missing from each file's annotations, and for each missing property attempt to resolve it from the file's per-sample source metadata. Properties that cannot be resolved must be documented in the GitHub curation comment under a "fields not populated" section with the reason. The audit output (`audit_results.json`) must include a `missing_schema_fields` list per project. → Standards 5, 11

20. **`dataset_ids_to_snapshot` must always be populated in Phase 2 output** — Every entry in `audit_reasoning_fixes.json` must include a `dataset_ids_to_snapshot` list with all dataset IDs from that project, even when there are no other annotation gaps. Phase 3 only mints stable versions for datasets explicitly listed here. Omitting this list means NO version is ever minted, and the dataset remains an unversioned live entity in the portal. When writing `audit_reasoning_fixes.json`, always include all datasets — even for projects that needed no other fixes.

21. **Dataset columns must start with `id` and `name` system columns** — Data managers expect the Dataset view column order: entity ID (`id`, type ENTITYID), filename (`name`, type STRING), then all annotation columns. Create both system columns via `POST /column` and prepend their IDs to `columnIds` before all annotation column IDs. Datasets created without these system columns show no identifier or filename in the view. The audit Phase 1 fixes missing `columnIds` by re-creating them with system columns first.

22. **Files folder containing only a landing-page link** — When `get_file_list_*` returns empty and the fallback to a single landing-page ExternalLink is used, the project is incomplete. Phase 1 detects this by checking if a files folder has exactly one file whose URL contains no recognizable file extension and matches a landing-page pattern (`acc.cgi`, `dataset.jsp`, `/records/`, `/record/`, etc.). Flag with a `landing_page_fallback` warning in `audit_results.json` and add `file-enumeration-required` to the GitHub curation comment under "Items for human review". Do NOT suppress or auto-fix this — it requires a human to trigger re-enumeration or manually link files. → Standard 13

23. **Dataset stable version must be minted after all annotation fixes** — After Phase 3 applies all annotation fixes, Phase 1 (on a re-run) checks whether each Dataset entity has a stable snapshot version (any version with a label). If not, mint one with `POST /entity/{dataset_id}/version`. The version is the permanent citable record — data managers will explicitly request it if missing. Always mint after annotation corrections are complete, not before. → Lesson 17

### `created_projects.json` Schema (output of Step 6, input to audit)

Step 6 must write this file. The audit reads it:

```json
[
  {
    "synapse_project_id": "syn74287500",
    "project_name": "Example study title",
    "pmid": "31595648",
    "doi": "10.1038/s41436-019-0691-3",
    "pub_group_id": "pmid_31595648",
    "abstract": "...",
    "outcome": "NEW",
    "datasets": [
      {
        "accession_id": "4688881",
        "source_repository": "Zenodo",
        "schema_uri": "<uri_prefix>bulksequencingassaytemplate",
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

with open('{WORKSPACE_DIR}/created_projects.json') as f:
    created = json.load(f)

# Build ANNOTATION_COLUMNS dynamically: universal base + schema-specific properties.
# Do NOT hardcode domain-specific field names — fetch them from the schema at runtime.
BASE_ANNOTATION_COLUMNS = [
    # (col_name, col_type, max_size, facet_type)
    ('study',                    'STRING', 256, 'enumeration'),
    ('assay',                    'STRING', 128, 'enumeration'),
    ('species',                  'STRING', 128, 'enumeration'),
    ('diagnosis',                'STRING', 256, 'enumeration'),
    ('tumorType',                'STRING', 256, 'enumeration'),
    ('platform',                 'STRING', 256, 'enumeration'),
    ('libraryPreparationMethod', 'STRING', 128, 'enumeration'),
    ('libraryStrand',            'STRING',  64, 'enumeration'),
    ('nucleicAcidSource',        'STRING',  64, 'enumeration'),
    ('organ',                    'STRING',  64, 'enumeration'),
    ('dataSubtype',              'STRING',  64, 'enumeration'),
    ('fileFormat',               'STRING',  64, 'enumeration'),
    ('resourceType',             'STRING',  64, 'enumeration'),
    # resourceStatus intentionally excluded — belongs on project/dataset entity, NOT on files
    ('externalAccessionID',      'STRING', 128, None),
    ('externalRepository',       'STRING',  64, 'enumeration'),
    ('specimenID',               'STRING', 128, None),
    ('individualID',             'STRING', 128, None),
]
HIGH_CARDINALITY = {'specimenID', 'individualID', 'externalAccessionID', 'name', 'id'}
BASE_NAMES = {c[0] for c in BASE_ANNOTATION_COLUMNS}
# For audit: derive schema_uri from the project's files folder binding
# (read from created_projects.json → schema_uri field, or re-fetch from Synapse)
schema_props = fetch_schema_properties(proj.get('schema_uri', '')) if proj.get('schema_uri') else {}
ANNOTATION_COLUMNS = list(BASE_ANNOTATION_COLUMNS)
for prop_name, prop_def in schema_props.items():
    if prop_name in BASE_NAMES:
        continue
    has_enum = bool(prop_def.get('enum')) or any(
        'enum' in s for s in prop_def.get('anyOf', [])
    )
    facet = None if prop_name in HIGH_CARDINALITY else ('enumeration' if has_enum else None)
    size = 256 if prop_def.get('maxLength', 64) > 128 else (128 if prop_def.get('maxLength', 64) > 64 else 64)
    ANNOTATION_COLUMNS.append((prop_name, 'STRING', size, facet))

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

    # ── 2. Data manager team permissions ─────────────────────────
    import yaml as _yaml
    with open('config/settings.yaml') as _f:
        _cfg = _yaml.safe_load(_f)
    _team_id = int(_cfg['synapse']['team_id'])
    _team_id_str = str(_team_id)
    try:
        acl = syn.restGET(f'/entity/{project_id}/acl')
        has_team = any(ra['principalId'] == _team_id for ra in acl.get('resourceAccess', []))
        if not has_team:
            syn.setPermissions(
                project_id, principalId=_team_id_str,
                accessType=['READ','DOWNLOAD','CREATE','UPDATE','DELETE',
                            'CHANGE_PERMISSIONS','CHANGE_SETTINGS','MODERATE',
                            'UPDATE_SUBMISSION','READ_PRIVATE_SUBMISSION'],
                warn_if_inherits=False
            )
            result['fixes_applied'].append(f'Data manager team ({_team_id_str}) permissions granted')
            print(f"  Permissions: FIXED — data manager team granted access")
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

    # ── 3b. Source Metadata folder — must not be empty ─────────────
    try:
        sm_folder = next(
            (c for c in syn.getChildren(project_id, includeTypes=['folder'])
             if c['name'] == 'Source Metadata'),
            None
        )
        if sm_folder:
            sm_contents = list(syn.getChildren(sm_folder['id']))
            if not sm_contents:
                result['reasoning_gaps'].append({'scope': 'project', 'field': 'source_metadata_empty'})
                result['warnings'].append('Source Metadata/ folder is empty — populate or remove it')
                print(f"  Source Metadata: EMPTY — flagged for Phase 2")
            else:
                print(f"  Source Metadata: OK ({len(sm_contents)} files)")
        else:
            print(f"  Source Metadata: folder not found (may have been removed or not created)")
    except Exception as e:
        result['warnings'].append(f'Source Metadata check failed: {e}')

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
            elif len(file_children) == 1:
                # Detect landing-page fallback: a single file whose URL looks like a
                # repository landing page rather than a direct download link.
                LANDING_PAGE_PATTERNS = (
                    'acc.cgi', 'dataset.jsp', '/records/', '/record/', 'view/detail',
                    'study.cgi', 'ProteoSAFe/dataset', 'cellimagelibrary.org/image_group',
                )
                try:
                    sole_file = syn.get(file_children[0]['id'], downloadFile=False)
                    sole_url  = getattr(sole_file, 'externalURL', '') or ''
                    if not sole_url:
                        # Check path attribute for ExternalLink-style files
                        sole_url = getattr(sole_file, '_file_handle', {}).get('externalURL', '') or ''
                    import urllib.parse, posixpath
                    parsed   = urllib.parse.urlparse(sole_url)
                    path_ext = posixpath.splitext(parsed.path)[1].lstrip('.').lower()
                    is_landing = (
                        any(pat in sole_url for pat in LANDING_PAGE_PATTERNS)
                        or (not path_ext and sole_url)  # URL with no file extension
                    )
                    if is_landing:
                        result['warnings'].append(
                            f'{acc}: LANDING_PAGE_FALLBACK — files folder contains only a '
                            f'landing-page link ({sole_url[:120]}). '
                            f'File enumeration is incomplete. Flag as file-enumeration-required.'
                        )
                        print(f"    Files: WARNING — landing-page fallback detected ({sole_url[:80]})")
                except Exception as _lp_err:
                    pass  # if we can't check, continue
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
                # resourceStatus must NOT be on File entities — REMOVE it if present
                if 'resourceStatus' in ann_dict:
                    del fe.annotations['resourceStatus']
                    file_gap['fixes'].append('resourceStatus REMOVED (belongs on project/dataset, not files)'); changed = True

                # Also remove any custom 'filename' annotation — use Synapse system 'name' instead
                if 'filename' in ann_dict:
                    del fe.annotations['filename']
                    file_gap['fixes'].append('filename annotation REMOVED (use system name column)'); changed = True

                # Remove any custom 'name' annotation — Synapse system 'name' is the entity name column.
                # A custom 'name' annotation creates a duplicate annotation column in Dataset views,
                # causing the expected name column to appear empty or broken.
                if 'name' in ann_dict:
                    del fe.annotations['name']
                    file_gap['fixes'].append('name annotation REMOVED (conflicts with system name column)'); changed = True

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

                # Zero-annotation check — file has no annotations at all
                # (auto-fixes above may have deleted some; check original ann_dict)
                schema_fields = [k for k in ann_dict if k not in ('resourceStatus', 'filename', 'name')]
                if not schema_fields:
                    file_gap['gaps'].append('FILE HAS NO ANNOTATIONS — requires full annotation pass in Phase 2')

                # Reasoning-required fields
                for field in REASONING_FILE_FIELDS:
                    if not _scalar(ann_dict.get(field, '')):
                        file_gap['gaps'].append(f'{field} missing')

                # modelSystemName — required for all non-human-species files
                cur_species = _scalar(ann_dict.get('species', ''))
                MODEL_ORG_KEYWORDS = ('mus musculus', 'mouse', 'rattus', 'rat', 'drosophila',
                                      'danio rerio', 'zebrafish', 'caenorhabditis', 'xenopus')
                if cur_species and any(k in cur_species.lower() for k in MODEL_ORG_KEYWORDS):
                    if not _scalar(ann_dict.get('modelSystemName', '')):
                        file_gap['gaps'].append('modelSystemName missing (required for model organism studies)')

                # assayTarget — required for ChIP-seq, CUT&RUN, ATAC-seq
                cur_assay = _scalar(ann_dict.get('assay', ''))
                ASSAY_TARGET_ASSAYS = ('chip-seq', 'chip seq', 'cut&run', 'cut&tag', 'atac-seq', 'atac seq')
                if cur_assay and any(k in cur_assay.lower() for k in ASSAY_TARGET_ASSAYS):
                    if not _scalar(ann_dict.get('assayTarget', '')):
                        file_gap['gaps'].append('assayTarget missing (required for ChIP-seq/CUT&RUN/ATAC-seq)')

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

                # Items — re-link if missing; sync versions if stale
                # NOTE: every syn.store(f) or annotations2 PUT increments the file version.
                # The Dataset must always point to the current version or annotations won't show.
                current_items = ds_body.get('items', [])
                if not current_items and file_children:
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
                elif current_items and file_children:
                    # Check for version mismatches caused by annotation updates
                    item_ver_map = {item['entityId']: item['versionNumber'] for item in current_items}
                    updated_items = []
                    mismatched = 0
                    for child in file_children:
                        eid = child['id']
                        fe = syn.get(eid, downloadFile=False)
                        actual_ver = fe.properties.get('versionNumber', 1)
                        linked_ver = item_ver_map.get(eid, actual_ver)
                        updated_items.append({'entityId': eid, 'versionNumber': actual_ver})
                        if linked_ver != actual_ver:
                            mismatched += 1
                    if mismatched:
                        ds_body2 = syn.restGET(f'/entity/{dataset_id}')
                        ds_body2['items'] = updated_items
                        syn.restPUT(f'/entity/{dataset_id}', json.dumps(ds_body2))
                        result['fixes_applied'].append(
                            f'{acc}: Dataset items version-synced ({mismatched} updated)')
                        print(f"    Dataset items: FIXED — {mismatched} item version(s) synced")
                    else:
                        print(f"    Dataset items: OK ({len(current_items)} items, all versions current)")
                else:
                    print(f"    Dataset items: OK ({len(current_items)} items)")

                # columnIds — rebuild from actual file annotations
                HIGH_CARDINALITY_AUDIT = {'specimenID', 'individualID', 'externalAccessionID',
                                          'name', 'id', 'sampleId', 'runAccession', 'biosampleId'}
                EXCLUDE_COLS_AUDIT = {'resourceStatus', 'filename'}

                # Collect annotation keys from files
                all_ann_audit = {}
                for item in ds_body.get('items', []):
                    fid = item.get('entityId')
                    if not fid:
                        continue
                    try:
                        fann = syn.restGET(f'/entity/{fid}/annotations2')
                        for k, v in fann.get('annotations', {}).items():
                            if k not in all_ann_audit:
                                all_ann_audit[k] = v
                    except Exception:
                        pass

                expected_col_names = {k for k in all_ann_audit if k not in EXCLUDE_COLS_AUDIT}
                existing_col_ids = ds_body.get('columnIds', [])
                existing_col_names = set()
                for cid in existing_col_ids:
                    try:
                        existing_col_names.add(syn.restGET(f'/column/{cid}')['name'])
                    except Exception:
                        pass

                # Check column ordering too: id and name must be first two
                existing_ordered = []
                for cid in existing_col_ids:
                    try:
                        existing_ordered.append(syn.restGET(f'/column/{cid}')['name'])
                    except Exception:
                        pass
                id_name_first = (len(existing_ordered) >= 2
                                 and existing_ordered[0] == 'id'
                                 and existing_ordered[1] == 'name')

                if expected_col_names != existing_col_names or not id_name_first:
                    # Get or create system columns (id, name)
                    sys_col_map_audit = {}
                    for cid in existing_col_ids:
                        try:
                            col_def = syn.restGET(f'/column/{cid}')
                            if col_def.get('name') in ('id', 'name'):
                                sys_col_map_audit[col_def['name']] = cid
                        except Exception:
                            pass
                    if 'id' not in sys_col_map_audit:
                        c = syn.restPOST('/column', json.dumps({'name': 'id', 'columnType': 'ENTITYID'}))
                        sys_col_map_audit['id'] = c['id']
                    if 'name' not in sys_col_map_audit:
                        c = syn.restPOST('/column', json.dumps({'name': 'name', 'columnType': 'STRING', 'maximumSize': 256}))
                        sys_col_map_audit['name'] = c['id']

                    annotation_col_ids_audit = []
                    for col_name in sorted(expected_col_names):
                        if col_name in ('id', 'name'):
                            continue
                        val_obj = all_ann_audit[col_name]
                        ann_type = val_obj.get('type', 'STRING')
                        if ann_type == 'DOUBLE':
                            body = {'name': col_name, 'columnType': 'DOUBLE'}
                        elif ann_type in ('LONG', 'INTEGER'):
                            body = {'name': col_name, 'columnType': 'INTEGER'}
                        else:
                            values = val_obj.get('value', [])
                            max_len = max((len(str(v)) for v in values), default=64)
                            size = 500 if max_len > 250 else (256 if max_len > 128 else (128 if max_len > 64 else 64))
                            body = {'name': col_name, 'columnType': 'STRING', 'maximumSize': size}
                            if col_name not in HIGH_CARDINALITY_AUDIT:
                                body['facetType'] = 'enumeration'
                        col = syn.restPOST('/column', json.dumps(body))
                        annotation_col_ids_audit.append(col['id'])

                    # Final order: id → name → annotation columns
                    new_col_ids = ([sys_col_map_audit['id'], sys_col_map_audit['name']]
                                   + annotation_col_ids_audit)
                    ds_body2 = syn.restGET(f'/entity/{dataset_id}')
                    ds_body2['columnIds'] = new_col_ids
                    syn.restPUT(f'/entity/{dataset_id}', json.dumps(ds_body2))
                    result['fixes_applied'].append(f'{acc}: Dataset columnIds rebuilt (id, name first, then annotations)')
                    print(f"    Dataset columnIds: FIXED — id+name first, then {len(annotation_col_ids_audit)} annotation cols")
                else:
                    print(f"    Dataset columnIds: OK ({len(existing_col_ids)} columns, id+name first)")

                # Dataset name — must be descriptive (not just repo_accession)
                ds_name = ds_body.get('name', '')
                # Flag if name looks like just an accession or a generic label
                import re as _re
                if _re.match(r'^(GEO|SRA|ENA|PRIDE|Zenodo|Figshare|OSF)?_?[A-Z]{2,5}\d{4,10}$', ds_name) or \
                   ds_name.lower() in ('data', 'dataset', 'rna-seq', 'atac-seq', 'chip-seq'):
                    result['reasoning_gaps'].append({
                        'scope': 'dataset', 'accession_id': acc,
                        'field': 'dataset_name',
                        'current': ds_name,
                        'note': 'Dataset name is not descriptive — should be: assay + biological context + (repo accession)',
                    })
                    print(f"    Dataset name: NON-DESCRIPTIVE ('{ds_name}') — flagged for Phase 2")
                else:
                    print(f"    Dataset name: OK ('{ds_name[:60]}')")

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

        # 4d-pre. Stable Dataset version — mint if no labeled snapshot exists
        if dataset_id:
            try:
                versions_resp = syn.restGET(f'/entity/{dataset_id}/version?limit=10&offset=0')
                versions = versions_resp.get('results', [])
                has_labeled = any(v.get('versionLabel') for v in versions)
                if not has_labeled:
                    snap = syn.restPOST(
                        f'/entity/{dataset_id}/version',
                        json.dumps({'label': 'v1', 'comment': 'Stable version minted by NADIA audit'})
                    )
                    result['fixes_applied'].append(
                        f'{acc}: Dataset stable version minted (v{snap.get("versionNumber", 1)})')
                    print(f"    Dataset stable version: FIXED — minted v{snap.get('versionNumber', 1)}")
                else:
                    print(f"    Dataset stable version: OK")
            except Exception as e:
                result['warnings'].append(f'{acc}: stable version check failed: {e}')

        # 4e. Schema binding
        if schema_uri and files_folder_id:
            try:
                try:
                    binding = syn.restGET(f'/entity/{files_folder_id}/schema/binding')
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

    # 5. Delete stray empty folders (e.g. Analysis/ left from creation bugs)
    STRUCTURAL_FOLDER_NAMES = {'Raw Data', 'Source Metadata'}
    try:
        for child in syn.getChildren(project_id, includeTypes=['folder']):
            if child['name'] in STRUCTURAL_FOLDER_NAMES:
                continue  # these may be legitimately empty while populating
            try:
                sub_children = list(syn.getChildren(child['id']))
                if not sub_children:
                    syn.delete(child['id'])
                    result['fixes_applied'].append(f"Empty folder '{child['name']}' deleted")
                    print(f"  Empty folder '{child['name']}': DELETED")
            except Exception as e2:
                result['warnings'].append(f"Could not check/delete folder '{child['name']}': {e2}")
    except Exception as e:
        result['warnings'].append(f'Empty folder check failed: {e}')

    # 6. Dataset name readability check
    for ds in datasets:
        dataset_id = ds.get('dataset_id', '')
        acc        = ds.get('accession_id', '')
        repo       = ds.get('source_repository', '')
        if dataset_id:
            try:
                ds_ent = syn.restGET(f'/entity/{dataset_id}')
                ds_name = ds_ent.get('name', '')
                # Flag names that look like the bare accession pattern (not human-readable)
                import re as _re
                if _re.match(r'^[A-Za-z]+_[A-Za-z0-9]+$', ds_name) or ds_name == f'{repo}_{acc}':
                    result['reasoning_gaps'].append({
                        'scope': 'dataset_name',
                        'dataset_id': dataset_id,
                        'current_name': ds_name,
                        'field': 'dataset_name',
                    })
                    print(f"    Dataset name '{ds_name}': flagged as non-human-readable")
            except Exception as e:
                result['warnings'].append(f'Dataset name check failed: {e}')

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
with open('{WORKSPACE_DIR}/audit_results.json', 'w') as f:
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

print(f"\nAudit Phase 1 complete. Results: {WORKSPACE_DIR}/audit_results.json")
```

---

### Phase 2 — Agent Reasoning

After running `audit.py`, read `{WORKSPACE_DIR}/audit_results.json`. For each project with `reasoning_gaps`:

1. **Read the available context** — project annotations (studyName, alternateDataRepository), the abstract stored in audit_results, and the wiki if it exists
2. **If PMID is available and abstract is missing**, fetch it: `Entrez.efetch(db='pubmed', id=pmid, rettype='xml')`
3. **Reason through each gap** using the publication title + abstract + repository metadata:
   - `diseaseFocus`, `manifestation`: infer from disease mentions in abstract
   - `dataType`: infer from assay type (scRNA-seq → `geneExpression`)
   - `studyLeads`: if PMID known, fetch AuthorList; take first + last author
   - `institutions`: from author affiliations in PubMed record
   - `alternateDataRepository`: reconstruct from accession_id + source_repository using REPO_TO_PREFIX
   - `assay`, `species`, `tumorType`, `diagnosis`: infer from abstract + title — but see Standard 12: if samples are normal/control cells, do NOT assign the disease tumor type
   - `platform`: fetch from repository metadata (GEO GSE → series platform, SRA → instrument model)
   - `libraryPreparationMethod`: infer from abstract ("10x Chromium", "Smart-seq2", "polyA", etc.)
   - `specimenID` for files where auto-parse failed: look at repository sample table (GEO GSM list, SRA BioSample)
   - `wiki` missing: create using the wiki template from this file
   - **`LANDING_PAGE_FALLBACK` warnings**: for each dataset flagged in Phase 1 warnings, add an item to the GitHub curation comment under "Items for human review": `file-enumeration-required — files folder contains only a landing-page link to {url}. Actual data files could not be enumerated. Manual re-enumeration or data linking needed.`
4. **Write `{WORKSPACE_DIR}/audit_reasoning_fixes.json`** with all resolved values

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

with open('{WORKSPACE_DIR}/audit_reasoning_fixes.json') as f:
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

    # After fixing file annotations, sync Dataset item versions and mint a stable snapshot
    for ds_fix in proj_fix.get('dataset_ids_to_snapshot', []):
        dataset_id = ds_fix.get('dataset_id')
        if not dataset_id:
            continue
        try:
            # Sync item versions (file annotations may have incremented file versions)
            ds_body = syn.restGET(f'/entity/{dataset_id}')
            if ds_body.get('items'):
                updated_items = []
                for item in ds_body['items']:
                    eid = item['entityId']
                    fe = syn.get(eid, downloadFile=False)
                    updated_items.append({
                        'entityId': eid,
                        'versionNumber': fe.properties.get('versionNumber', 1)
                    })
                ds_body2 = syn.restGET(f'/entity/{dataset_id}')
                ds_body2['items'] = updated_items
                syn.restPUT(f'/entity/{dataset_id}', json.dumps(ds_body2))
                print(f"  Dataset {dataset_id}: item versions synced")
            # Mint stable version
            snap = syn.restPOST(
                f'/entity/{dataset_id}/version',
                json.dumps({'label': 'v1', 'comment': 'Stable version after NADIA annotation review'})
            )
            print(f"  Dataset {dataset_id}: stable version minted → v{snap.get('versionNumber', '?')}")
        except Exception as e:
            print(f"  Dataset {dataset_id} snapshot failed: {e}")

print(f"\nApply complete: {total_projects} projects, {total_files} files updated.")
```

---

### Audit Output Format

The full audit prints a report like this before JIRA notifications:

```
=== Self-Audit Report ===

syn74287500 — Example Study Title
  Auto-fixes: studyStatus Active→Completed, fundingAgency set, project.pmid set
  Reasoning gaps: diseaseFocus, manifestation, studyLeads, institutions, wiki missing
  File fixes: resourceStatus×1, resourceType×1, fileFormat fastq.gz→fastq ×1
  File gaps: tumorType missing ×1, diagnosis missing ×1
  Warnings: 0

syn74287507 — Developmental loss of neurofibromin across neural circuits
  Auto-fixes: resourceStatus REMOVED from 20 files (was incorrectly set on files)
  Reasoning gaps: none
  Warnings: 0

=== Audit Summary ===
Projects audited:   N
Auto-fixes applied: N
Reasoning gaps:     N (resolve in Phase 2, apply in Phase 3)
Warnings:           N
========================
```
