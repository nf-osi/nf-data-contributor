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

# Human-readable Dataset name — assay type + repository/accession
# normalized_annotations['assay'] is already set from Step D
assay_label = normalized_annotations.get('assay', 'Data')
if isinstance(assay_label, list):
    assay_label = ', '.join(assay_label)
species_label = normalized_annotations.get('species', '')
if isinstance(species_label, list):
    species_label = ', '.join(species_label)

# Synapse name allows: letters, numbers, spaces, underscores, hyphens, periods,
# plus signs, apostrophes, parentheses — NO em-dash, NO colon
dataset_name = f"{assay_label} ({repository} {accession_id})"
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

Without column definitions, the Dataset appears empty in the UI even if files have full annotations.
`facetType: "enumeration"` enables the Filter panel for that column — set it on all categorical annotation columns.
Leave `facetType` unset for high-cardinality identifier columns (`specimenID`, `individualID`, `externalAccessionID`).

```python
# (col_name, col_type, max_size, facet_type)
# facet_type = 'enumeration' | None
ANNOTATION_COLUMNS = [
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
    ('resourceStatus',           'STRING',  64, 'enumeration'),
    ('externalAccessionID',      'STRING', 128, None),           # one value per dataset — not a useful facet
    ('externalRepository',       'STRING',  64, 'enumeration'),
    ('specimenID',               'STRING', 128, None),           # high-cardinality identifier
    ('individualID',             'STRING', 128, None),           # high-cardinality identifier
    ('nf1Genotype',              'STRING',  64, 'enumeration'),
    ('modelSpecies',             'STRING', 128, 'enumeration'),
    ('modelSex',                 'STRING',  32, 'enumeration'),
    ('sex',                      'STRING',  32, 'enumeration'),
]

col_ids = []
for col_name, col_type, col_size, facet_type in ANNOTATION_COLUMNS:
    body = {'name': col_name, 'columnType': col_type, 'maximumSize': col_size}
    if facet_type:
        body['facetType'] = facet_type
    col = syn.restPOST('/column', json.dumps(body))
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
1. Create project → folders → File entities  (**no Dataset yet**)
2. Apply annotations to each individual File entity  (**must happen before Dataset linking** — each `syn.store(f)` or `annotations2` PUT increments the file version; if the Dataset is linked first it will point to pre-annotation versions and show blank columns)
3. Create the Dataset entity and link files  (**now version numbers reflect the annotated state**)
4. Apply annotations to Dataset entity
5. Set columnIds on Dataset entity
6. `bind_nf_schema(syn, files_folder_id, schema_uri)` ← bind to FILES FOLDER
7. Print validation result

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

### Step C — Reason through ALL schema properties and normalize

**Do not hardcode field names.** Use `schema_props` from Step A2 as the authoritative list. For EVERY field the schema defines, check whether source data contains a value, then reason about the best match.

The process (in agent reasoning, not Python):
1. Read `schema_props` to get the full list of available fields
2. Read `raw_metadata` to see what was extracted from source
3. For each schema property:
   - Does source data contain something mappable to this field?
   - If the field has an enum: does the raw value match any enum entry (exact or near-match)?
   - If no match: leave unset (do NOT invent values)
4. Write only the fields you can confidently populate

Common source → schema field mappings (illustrative, not exhaustive — always check schema_props for current field names):

| Source data key | Schema field (if present) | Notes |
|-----------------|--------------------------|-------|
| organism / scientific_name | `species` | enum — match to binomial |
| tissue / source | `organ` | enum — match best available |
| sex / gender | `sex` | enum — male/female/unknown |
| age / age_at_diagnosis | `age` + `ageUnit` | numeric + enum unit |
| cell_type / cell line | `isCellLine` (bool enum), `isPrimaryCell` (bool enum) | |
| genotype / nf1_genotype | `nf1Genotype` | enum |
| genotype / nf2_genotype | `nf2Genotype` | enum |
| model organism strain / line | `modelSystemName` | freetext — e.g. "C57BL/6" |
| model_species | `modelSpecies` | enum |
| model_age / age | `age` + `ageUnit` | numeric age value + unit enum (days/months/years/etc.) |
| model_sex | `sex` | enum |
| treatment / drug / compound | `experimentalCondition` | freetext |
| perturbed gene / knockdown | `genePerturbed`, `genePerturbationType`, `genePerturbationTechnology` | |
| library_strategy / assay | `assay` | enum — RNA-seq, WGS, etc. |
| instrument_model | `platform` | enum |
| library_selection / library_prep | `libraryPreparationMethod` | enum |
| library_layout (PAIRED/SINGLE) | `runType` | enum |
| strandedness | `libraryStrand` | enum |
| read_length | `readLength` | integer |
| read_count / base_count | `readDepth` | integer |
| nucleic_acid_source (total RNA, polyA, etc.) | `nucleicAcidSource` | enum |
| sample_alias / run_accession | `specimenID`, `individualID` | per-file |
| batch / lane | `batchID` | freetext |
| tissue_preparation / extraction | `specimenPreparationMethod` | enum |
| specimen type (tumor/normal/cell line) | `specimenType` | enum |
| diagnosis / disease | `diagnosis` | enum |
| tumor type | `tumorType` | enum |
| data processing level | `dataSubtype` | raw/processed/normalized |

Write the result to `/tmp/nf_agent/normalized_annotations.json` — keyed only by fields present in `schema_props`:

```python
import json

# After reasoning through each field:
normalized = {}
# ... populate only fields confirmed in schema_props with values confirmed in raw_metadata ...

with open('/tmp/nf_agent/normalized_annotations.json', 'w') as f:
    json.dump(normalized, f, indent=2)
print(f"  Normalized {len(normalized)} annotation fields")
```
```

**Key constraints:**
- **Never hardcode field names** — always derive available fields from `schema_props = fetch_schema_properties(schema_uri)` at runtime
- Only set enum-constrained fields to values that appear in the schema's enum list
- Populate as many schema fields as source data supports — more is better; omitting a field is fine, inventing one is not
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

**Shared across all files** (values that are the same for every file in the dataset — check schema_props for exact field names): fields like `assay`, `species`, `diagnosis`, `tumorType`, `platform`, `libraryPreparationMethod`, `libraryStrand`, `libraryPrep`, `specimenPreparationMethod`, `dissociationMethod`, `study`, `externalAccessionID`, `externalRepository`, `dataSubtype`, `resourceStatus`, `sex`, `organ`, `isCellLine`, `isPrimaryCell`, `nucleicAcidSource`, `runType`, `readLength` — populate all that apply.

**Model organism shared fields** (required for any mouse/rat/zebrafish study): `modelSpecies`, `modelSex`, `modelAgeUnit`, `modelSystemName`, `nf1Genotype`, `nf2Genotype`. Fetch from repository sample attributes at creation time — do not leave blank.

**Per-file** (values that differ per file): `fileFormat`, `specimenID`, `individualID`, `readPair` (I1/R1/R2 for 10x; R1/R2 for paired-end), `modelAge` (if age varies per sample), `batchID` (if batch varies per file), `aliquotID`

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

*This project was ingested automatically by @nadia-bot on {today} and is pending data manager review.*
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
    tmp = f'/tmp/nf_agent/source_meta_{filename}'
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

## `bind_nf_schema()` Helper

**Checking if a schema is already bound** — use `js.validate()`, NOT the REST endpoint `/entity/{id}/json_schema_binding` (that returns 404 unconditionally):

```python
def get_bound_schema_uri(syn, folder_id: str) -> str | None:
    """Returns schema URI if a schema is bound to this folder, else None."""
    try:
        js = syn.service('json_schema')
        val = js.validate(folder_id)
        schema_id = val.get('schema$id', '')
        if schema_id:
            prefix = 'https://repo-prod.prod.sagebase.org/repo/v1/schema/type/registered/'
            return schema_id[len(prefix):] if schema_id.startswith(prefix) else schema_id
        return None
    except Exception:
        return None  # 404 means no schema bound
```

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

8. **Dataset item versions go stale after annotation fixes** — every `syn.store(f)` or `annotations2` PUT increments the file version. If the audit auto-fix applies file annotations (step 4b), the Dataset items end up pointing to the pre-fix version and the Datasets tab shows blank annotation columns. The version-sync check in step 4c now catches and corrects this. The root fix is to always annotate files BEFORE linking them to the Dataset (see Required order above).

9. **`studyLeads` must be first + last/corresponding author — not the repository submitter** — ENA/ArrayExpress submitters are typically research engineers or postdocs who performed the experiment, not the PI. The BioStudies `[Author]` section lists role (`submitter`, `experiment performer`, `principal investigator`). If only a submitter is present, search bioRxiv for a preprint using the mouse model name + assay + institution. If a preprint is found, derive studyLeads from its author list (first + last). Never default to the submitter name as studyLeads without checking for a publication or preprint.

10. **`species` must be verified from the repository taxon field, never inferred** — An NF1 dataset can use human, mouse, zebrafish, or Drosophila. Always read `scientific_name` (ENA filereport), `!Series_sample_taxid` (GEO SOFT), or `Organism` (BioStudies). A dataset submitted by a mouse lab may contain human cell data (e.g. patient-derived iPSCs or bone marrow aspirates). This was the root cause of species being set to Mus musculus on a human scRNA-seq dataset (GSE196652).

11. **`assay` must reflect single-cell vs bulk** — When `library_source = 'TRANSCRIPTOMIC SINGLE CELL'`, `library_strategy` mentions scRNA, or the protocol names 10x Chromium / Fluidigm C1 / Drop-seq / Smart-seq2 (per-cell), the assay is `'single-cell RNA-seq'`, NOT `'RNA-seq'`. Setting bulk RNA-seq on a scRNA-seq dataset causes the wrong schema to be selected (rnaseqtemplate vs scrnaseqtemplate), which cascades to wrong annotation fields and wrong Curator Grid validation.

12. **Model animal fields must be populated at creation, not as a post-hoc fix** — For any mouse/rat/zebrafish dataset, fetch sample metadata from the repository at creation time and populate: `modelAge`, `modelAgeUnit`, `modelSex`, `modelSpecies`, `modelSystemName`, `nf1Genotype` (if NF1 study). These values live in ENA sample attributes, GEO sample characteristics, or BioStudies sample sections. Also populate `dissociationMethod`, `specimenPreparationMethod`, `isCellLine`, `runType`, `readPair`, `libraryPrep`, `libraryStrand` — these are all available from repository protocol/library metadata and should never be left blank.

13. **Dataset ANNOTATION_COLUMNS must include the expanded set** — The minimum 16-column list is insufficient for scRNA-seq and model organism data. The standard set is now 22 columns, adding: `nucleicAcidSource`, `organ`, `nf1Genotype`, `modelSpecies`, `modelSex`, `sex`. Always use the full 22-column list.

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
    ('resourceStatus',           'STRING',  64, 'enumeration'),
    ('externalAccessionID',      'STRING', 128, None),
    ('externalRepository',       'STRING',  64, 'enumeration'),
    ('specimenID',               'STRING', 128, None),
    ('individualID',             'STRING', 128, None),
    ('nf1Genotype',              'STRING',  64, 'enumeration'),
    ('modelSpecies',             'STRING', 128, 'enumeration'),
    ('modelSex',                 'STRING',  32, 'enumeration'),
    ('sex',                      'STRING',  32, 'enumeration'),
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

                # columnIds
                if not ds_body.get('columnIds'):
                    col_ids = []
                    for col_name, col_type, col_size, facet_type in ANNOTATION_COLUMNS:
                        body = {'name': col_name, 'columnType': col_type, 'maximumSize': col_size}
                        if facet_type:
                            body['facetType'] = facet_type
                        col = syn.restPOST('/column', json.dumps(body))
                        col_ids.append(col['id'])
                    ds_body2 = syn.restGET(f'/entity/{dataset_id}')
                    ds_body2['columnIds'] = col_ids
                    syn.restPUT(f'/entity/{dataset_id}', json.dumps(ds_body2))
                    result['fixes_applied'].append(f'{acc}: Dataset columnIds created')
                    print(f"    Dataset columnIds: FIXED")
                else:
                    # Check that annotation columns have facetType set
                    # Columns are immutable — must replace any without facets
                    FACET_COLS = {c[0] for c in ANNOTATION_COLUMNS if c[3] == 'enumeration'}
                    ds_check = syn.restGET(f'/entity/{dataset_id}')
                    existing_col_ids = ds_check.get('columnIds', [])
                    needs_replace = []
                    for cid in existing_col_ids:
                        col_def = syn.restGET(f'/column/{cid}')
                        if col_def['name'] in FACET_COLS and not col_def.get('facetType'):
                            needs_replace.append(col_def['name'])
                    if needs_replace:
                        # Re-create annotation columns with facets; keep system cols
                        ann_names = {c[0] for c in ANNOTATION_COLUMNS}
                        system_col_ids = [cid for cid in existing_col_ids
                                          if syn.restGET(f'/column/{cid}')['name'] not in ann_names]
                        new_col_ids = []
                        for col_name, col_type, col_size, facet_type in ANNOTATION_COLUMNS:
                            body = {'name': col_name, 'columnType': col_type, 'maximumSize': col_size}
                            if facet_type:
                                body['facetType'] = facet_type
                            col = syn.restPOST('/column', json.dumps(body))
                            new_col_ids.append(col['id'])
                        ds_body3 = syn.restGET(f'/entity/{dataset_id}')
                        ds_body3['columnIds'] = system_col_ids + new_col_ids
                        syn.restPUT(f'/entity/{dataset_id}', json.dumps(ds_body3))
                        result['fixes_applied'].append(
                            f'{acc}: Dataset columns re-created with facets ({len(needs_replace)} fixed)')
                        print(f"    Dataset columnIds: FIXED facets on {needs_replace}")
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
