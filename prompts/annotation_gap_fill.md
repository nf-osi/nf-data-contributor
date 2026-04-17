# Annotation Gap-Fill Strategy

This file defines the **generalized, schema-driven gap-fill algorithm** used whenever file annotations are incomplete. Run it in two situations:

1. **During initial annotation** — after Step C normalization in `prompts/synapse_workflow.md`, before applying annotations to File entities. This is the primary pass.
2. **During audit (Step 7b)** — for any field that remains unset after Phase 1 auto-fixes. This is the remediation pass.

In both cases the algorithm is the same. The only difference is the starting point: in the initial pass you start from zero; in the audit pass you start from what Phase 1 already set.

---

## The Algorithm

```
for each dataset in project:
  schema_props  = fetch_schema_properties(schema_uri)          # all fields the schema defines
  current_anns  = aggregate_file_annotations(files_folder_id)  # what's currently set
  never_set     = {'resourceStatus', 'filename'}               # never on File entities
  empty_enum    = {f for f, p in schema_props.items()          # fields with no valid values
                   if p.get('type') == 'enum' and not p.get('enum')}
  missing       = set(schema_props) - set(current_anns) - never_set - empty_enum

  for field in missing:
    category = classify_field(field, schema_props[field])      # see Field Categories below
    sources  = SOURCE_PRIORITY[category]                       # ordered list of source types

    for source_type in sources:
      raw_value = try_extract(source_type, field, schema_props[field], context)
      if raw_value is None:
        continue
      validated = validate_against_enum(raw_value, schema_props[field])
      if validated is not None:
        apply_annotation(field, validated)                     # write to Synapse
        break
      else:
        log_gap(field, f"value '{raw_value}' found but not in enum")
        break  # don't try another source for the same field with the wrong type of value
    else:
      log_gap(field, "no value found in any source")

  write_gap_report(project_id, missing_fields, gaps_with_reasons)
```

---

## Field Categories and Source Priority

Different schema fields have different natural sources. Classify each missing field into one of these categories, then work through the corresponding source priority list in order. Stop at the first source that yields a valid value.

### Category A — Technical / library-level fields
Fields describing how sequencing was performed: assay type, library preparation, strand orientation, run type, nucleic acid source, instrument, read depth, read length.

**Source priority:**
1. ENA filereport TSV (columns: `library_strategy`, `library_source`, `library_selection`, `library_layout`, `instrument_model`, `read_count`, `base_count`, `nominal_length`, `nominal_sdev`)
2. GEO SOFT `!Series_library_strategy`, `!Series_library_selection`, `!Sample_instrument_model`, `!Sample_library_source`, `!Sample_extract_protocol_ch1`
3. SRA RunInfo (same fields, alternate form)
4. PubMed/PMC methods section — search for kit names, instrument names, protocol keywords
5. Repository experiment descriptions (ArrayExpress SDRF `Protocol REF` column; PRIDE project XML)
6. FASTQ header parsing (instrument and run info from first read header)
7. BAM @RG tag (`PL` = platform, `LB` = library, `PU` = platform unit, `CN` = sequencing center)

### Category B — Biological sample fields (vary per file)
Fields describing the biological sample: organism/species, sex, age, tissue, cell type, specimen type, diagnosis, tumor type, genotype, treatment/condition, model system name, dissociation method.

**Source priority:**
1. GEO GSM `!Sample_characteristics_ch1` lines — these are structured `key: value` pairs per sample
2. ENA filereport (columns: `scientific_name`, `sample_title`, `sample_alias`, `tax_id`)
3. BioSample attributes (fetch via NCBI `efetch?db=biosample&id={biosample_id}`) — richer than ENA filereport
4. SRA BioSample XML attributes (same data, alternate form)
5. **Paper tables and supplementary files** — download and parse all supplementary files (see `fetch_geo_supplementary_files` + `try_download_and_parse_table` in Source Tier 2) and fetch PMC full text to scan main-text tables. **This is mandatory, not optional.** Fields like `sex`, `age`, `diagnosis`, `genotype`, and `treatment` are routinely absent from GEO/ENA sample records but present somewhere in the paper — in any supplementary table, a main-text cohort table, the results section, or the methods section. "Not in GEO metadata" is not a stopping condition — always search the paper before declaring a field unresolvable.
6. PubMed abstract — extract disease terms, organism, tissue, cell type via reasoning
7. PMC full text methods section — sex, age, genotype, treatment, dissociation protocol
8. Data file inspection — h5ad/loom `obs` columns contain per-cell/per-sample annotations (see Source Tier 4)

> **Never flag `sex`, `age`, `diagnosis`, `genotype`, or `treatment` for human review without first attempting supplementary table download.** These are the most commonly under-annotated fields in GEO/ENA records and the most commonly present in paper supplementary tables. A curation comment that says "sex not in GEO metadata" without having attempted the supplementary tables is an incomplete gap-fill.

### Category C — Study-level / investigator fields
Fields that apply uniformly across all files in the study: funding agency, study leads, institutions, data type category, external accession identifiers, resource type.

**Source priority:**
1. PubMed full record — GrantList (funding), AuthorList (leads), AffiliationInfo (institutions)
2. CrossRef API for the DOI — funder info, author affiliations
3. Repository project metadata — ENA study XML, GEO series, PRIDE project
4. Paper abstract / title — data type, assay category
5. BioStudies record (for ENA/ArrayExpress submissions) — can have `principal investigator` role explicitly tagged

### Category D — Identifier fields (per-file, must be unique)
Schema fields that capture specimen identity, individual identity, aliquot identity, sample IDs, BioSample accessions, or external accession IDs. Identify these at runtime by checking `fetch_schema_properties()` — field descriptions will indicate they capture per-sample biological or external identifiers.

**Source priority:**
1. ENA filereport: `run_accession` → maps to the file; `sample_alias` or `sample_title` → biological ID; `biosample_accession` (SAMN/SAME/SAMD) → individual ID
2. GEO GSM table: parse GSM ID from filename or from SOFT `^SAMPLE` block; use `!Sample_geo_accession`
3. SRA RunInfo: `Sample`, `BioSample`, `Experiment` columns
4. Supplementary table with patient/sample manifest — often has one row per patient/sample with all IDs
5. Filename parsing — extract SRR/ERR/DRR/GSM prefix as a last resort (never use run accessions as biological IDs; these are technical IDs)

### Category E — Model organism / in vitro system fields
Fields specific to mouse models, cell lines, organoids: model species, model sex, model age, model age unit, model system name, cell line name, passage number, strain, genotype of the model.

**Source priority:**
1. GEO GSM characteristics — often explicitly lists strain, genotype, treatment, passage
2. BioSample attributes — `strain`, `genotype`, `sex`, `age` attributes
3. ENA sample `sample_attribute` XML records (via `https://www.ebi.ac.uk/ena/browser/api/xml/{sample_accession}`)
4. Paper abstract / methods — named mouse model (e.g. "Nf1fl/fl; GFAP-Cre"), cell line name, organoid protocol
5. PMC full text — Methods section describes experimental system in detail
6. Supplementary tables — sample manifests often list genotype per mouse
7. Paper figures / supplementary figures — genotype labels on experimental groups (extract from figure legends if full text available)

---

## Source Tier 1 — Structured Repository Metadata

These are machine-readable, per-sample, highly reliable. Always attempt Tier 1 sources first.

### ENA filereport (comprehensive column list)

The filereport endpoint returns far more than what's typically fetched. Request ALL columns:

```python
import httpx

def fetch_ena_filereport_full(accession: str) -> list[dict]:
    """Fetch comprehensive ENA filereport with all available per-run metadata columns."""
    ALL_FIELDS = (
        'run_accession,experiment_accession,sample_accession,study_accession,'
        'secondary_study_accession,secondary_sample_accession,'
        'experiment_title,study_title,sample_title,sample_alias,'
        'scientific_name,tax_id,common_name,'
        'instrument_platform,instrument_model,'
        'library_name,library_strategy,library_selection,library_source,'
        'library_layout,library_construction_protocol,'
        'nominal_length,nominal_sdev,'
        'read_count,base_count,submitted_bases,submitted_reads,'
        'fastq_ftp,fastq_md5,sra_ftp,'
        'submitted_ftp,submitted_md5,'
        'sample_description,'
        'center_name,broker_name,'
        'first_public,last_updated,'
        'biosample_accession'
    )
    resp = httpx.get(
        'https://www.ebi.ac.uk/ena/portal/api/filereport',
        params={
            'accession': accession,
            'result': 'read_run',
            'fields': ALL_FIELDS,
            'format': 'tsv',
            'download': 'true',
        },
        timeout=60
    )
    if resp.status_code != 200 or not resp.text.strip():
        return []
    lines = resp.text.strip().split('\n')
    headers = lines[0].split('\t')
    return [dict(zip(headers, row.split('\t'))) for row in lines[1:] if row.strip()]
```

### BioSample XML attributes (richer than ENA filereport)

```python
import httpx, xml.etree.ElementTree as ET

def fetch_biosample_attributes(biosample_id: str) -> dict:
    """Fetch BioSample XML and return all attribute key-value pairs."""
    resp = httpx.get(
        'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi',
        params={'db': 'biosample', 'id': biosample_id, 'retmode': 'xml'},
        timeout=20
    )
    if resp.status_code != 200:
        return {}
    root = ET.fromstring(resp.text)
    attrs = {}
    for attr in root.findall('.//Attribute'):
        name = attr.get('attribute_name', attr.get('harmonized_name', ''))
        if name:
            attrs[name.lower().replace(' ', '_')] = attr.text or ''
    return attrs
    # Common attribute keys: sex, age, tissue, cell_type, cell_line, disease,
    #   genotype, strain, treatment, developmental_stage, passage_number,
    #   tumor_grade, sample_type, organism, isolation_source
```

### ENA sample XML (for model organism fields)

```python
def fetch_ena_sample_xml(sample_accession: str) -> dict:
    """Fetch ENA sample XML for full attribute list."""
    resp = httpx.get(
        f'https://www.ebi.ac.uk/ena/browser/api/xml/{sample_accession}',
        timeout=20
    )
    if resp.status_code != 200:
        return {}
    root = ET.fromstring(resp.text)
    attrs = {}
    for attr in root.findall('.//SAMPLE_ATTRIBUTE'):
        tag = attr.findtext('TAG', '').lower().replace(' ', '_')
        val = attr.findtext('VALUE', '')
        if tag and val:
            attrs[tag] = val
    # Also capture organism fields
    scientific_name = root.findtext('.//SCIENTIFIC_NAME', '')
    if scientific_name:
        attrs['scientific_name'] = scientific_name
    return attrs
```

### GEO GSM characteristics (per-sample)

Already fetched in `fetch_geo_full_soft()` — re-read from `raw_metadata['geo_samples']`:
```python
# Each GSM maps to a characteristics dict parsed from !Sample_characteristics_ch1 lines
# Example keys: 'genotype', 'tissue', 'treatment', 'age', 'sex', 'cell type', 'passage', etc.
# The file-to-GSM mapping is available in the GEO SOFT series relations block.

def map_file_to_gsm(filename: str, geo_samples: dict, sra_run_to_gsm: dict) -> str | None:
    """Map a filename (e.g. SRR12345_1.fastq.gz) to its GSM accession."""
    import re
    srr_match = re.match(r'(SRR|ERR|DRR)\d+', filename)
    if srr_match:
        run_acc = srr_match.group(0)
        return sra_run_to_gsm.get(run_acc)
    # Try direct GSM match
    gsm_match = re.search(r'(GSM\d+)', filename)
    if gsm_match:
        return gsm_match.group(1)
    return None
```

---

## Source Tier 2 — Publication Metadata

### PMC full text — methods section extraction

```python
import httpx, re

def fetch_pmc_methods(pmid: str) -> str:
    """Fetch PMC full text methods section (open-access articles only)."""
    # First resolve PMID → PMCID
    resp = httpx.get(
        'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi',
        params={'dbfrom': 'pubmed', 'db': 'pmc', 'id': pmid, 'retmode': 'json'},
        timeout=15
    )
    if resp.status_code != 200:
        return ''
    data = resp.json()
    pmc_ids = []
    for linkset in data.get('linksets', []):
        for ldb in linkset.get('linksetdbs', []):
            if ldb.get('dbto') == 'pmc':
                pmc_ids = ldb.get('links', [])
    if not pmc_ids:
        return ''  # Not in PMC (most clinical/controlled studies aren't)
    pmcid = f"PMC{pmc_ids[0]}"

    # Fetch full text XML
    resp2 = httpx.get(
        'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi',
        params={'db': 'pmc', 'id': pmcid, 'rettype': 'xml', 'retmode': 'xml'},
        timeout=30
    )
    if resp2.status_code != 200:
        return ''

    # Extract all <sec> elements containing "method" in the title
    root = ET.fromstring(resp2.text)
    methods_text = []
    for sec in root.iter('sec'):
        title = sec.findtext('title', '').lower()
        if any(kw in title for kw in ('method', 'material', 'procedure', 'protocol', 'experiment')):
            methods_text.append(' '.join(sec.itertext()))
    return ' '.join(methods_text)[:10000]  # cap at 10k chars to stay in context
```

### Supplementary tables from GEO or publisher

**Supplementary tables and paper text are required work, not optional.** Per-sample metadata (patient IDs, sex, age, diagnosis, genotype, treatment, etc.) that is absent from GEO/ENA sample records may appear anywhere in the paper — main-text tables, any supplementary table (not just Table 1), results section cohort descriptions, or methods section protocol details. For every project with missing Category B fields, you MUST attempt to retrieve this information from the paper before writing any gap report or flagging anything for human review.

Workflow:
1. Call `fetch_geo_supplementary_files(gse)` to list all supplementary files
2. Download each candidate file with `try_download_and_parse_table(url)`
3. Call `find_sample_metadata_table(tables, sample_ids)` to identify the sample manifest
4. Map table rows to files via the matched ID column
5. Apply per-sample values to each file's annotations

Only after completing steps 1–5 with no result should a Category B field be declared unresolvable from supplementary tables.

```python
import httpx, io, csv
try:
    import openpyxl   # for .xlsx
    HAVE_OPENPYXL = True
except ImportError:
    HAVE_OPENPYXL = False

def fetch_geo_supplementary_files(gse: str) -> list[dict]:
    """List supplementary files attached to a GEO series."""
    resp = httpx.get(
        f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi',
        params={'acc': gse, 'targ': 'self', 'form': 'json'},
        timeout=20
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    supp = []
    for key, val in data.items():
        if 'supplementary' in key.lower() and isinstance(val, str) and val.startswith('ftp'):
            supp.append({'name': key, 'url': val.replace('ftp://', 'https://')})
    return supp

def try_download_and_parse_table(url: str, max_rows: int = 500) -> list[dict] | None:
    """
    Download a supplementary file and try to parse it as a table.
    Returns list of row dicts if parseable, None otherwise.
    Handles: .tsv, .csv, .txt (tab or comma delimited), .xlsx
    """
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True)
        if resp.status_code != 200:
            return None
        name = url.split('/')[-1].lower()

        # Excel
        if name.endswith('.xlsx') or name.endswith('.xls'):
            if not HAVE_OPENPYXL:
                return None
            wb = openpyxl.load_workbook(io.BytesIO(resp.content), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return None
            headers = [str(h or '').strip() for h in rows[0]]
            return [dict(zip(headers, [str(v or '').strip() for v in row])) for row in rows[1:max_rows+1]]

        # Text-based (try tab first, then comma)
        text = resp.text
        for delimiter in ('\t', ','):
            reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
            rows = list(reader)
            if rows and len(rows[0]) > 2:   # at least 3 columns = probably a real table
                return rows[:max_rows]

        return None
    except Exception:
        return None

def find_sample_metadata_table(tables: list[list[dict]], sample_ids: list[str]) -> tuple[list[dict], str] | None:
    """
    Among a list of parsed tables, find the one that looks like a sample manifest
    (has rows matching the known sample IDs). Returns (rows, matched_id_column) or None.
    """
    for rows in tables:
        if not rows:
            continue
        headers = list(rows[0].keys())
        for col in headers:
            col_values = [str(r.get(col, '')).strip() for r in rows]
            # Check overlap with known sample IDs
            overlap = sum(1 for sid in sample_ids if any(sid in v for v in col_values))
            if overlap >= min(3, len(sample_ids) // 2):
                return rows, col
    return None
```

### PMC supplementary data links

```python
def fetch_pmc_supplementary_links(pmcid: str) -> list[str]:
    """Extract supplementary data download links from PMC article page."""
    resp = httpx.get(
        f'https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/',
        timeout=20
    )
    if resp.status_code != 200:
        return []
    import re
    # PMC supplements are linked as /pmc/articles/PMCXXXX/bin/filename.ext
    links = re.findall(r'href="(/pmc/articles/PMC\d+/bin/[^"]+\.(xlsx?|tsv|csv|txt|zip))"', resp.text)
    return [f'https://www.ncbi.nlm.nih.gov{lnk[0]}' for lnk in links]
```

### Unpaywall — access publisher PDFs/supplementary when not in PMC

```python
def get_unpaywall_oa_url(doi: str, contact_email: str) -> str | None:
    """Return the best open-access PDF URL for a DOI, or None if not available."""
    resp = httpx.get(
        f'https://api.unpaywall.org/v2/{doi}',
        params={'email': contact_email},
        timeout=15
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    best = data.get('best_oa_location')
    if best:
        return best.get('url_for_pdf') or best.get('url')
    return None
```

---

## Source Tier 3 — Text Extraction via Reasoning

When structured sources don't yield a value, extract from free text using agent reasoning. This is NOT Python code — it is reasoning guidance.

**For each remaining missing field, read the following sources in order and reason about the value:**

1. **Abstract** (PubMed, already fetched): Contains disease focus, organism, broad tissue type, study design keywords. Good for: schema fields capturing disease state, organism/species, tissue type, data modality — verify any assay-type field against the structured `library_strategy` column before applying.

2. **Methods section** (PMC or Unpaywall PDF text): Contains library preparation kit names, instrument model, dissociation protocol (for scRNA-seq), centrifuge speeds (hints at specimen type), age range, sex breakdown. Good for: schema fields capturing library preparation method, dissociation method, specimen preparation, sequencing instrument/platform, age/age-unit ranges, sex distribution.

3. **Supplementary table rows** (already fetched above): Often the ground truth for per-sample annotations. Good for: schema fields capturing per-sample identifiers, demographics (sex, age, age unit), disease/diagnosis classification, tumor type, genotype or model system identity, treatment or experimental condition.

4. **Figure legends** (from PMC full text): Figure 1 often describes the cohort. Figure S1/S2 supplementary figures frequently list patient demographics and sample characteristics.

**Extraction rules:**
- Only apply values that are **unambiguous** in the text — e.g., "All patients were female" → set the sex field to Female for all files
- For per-file varying fields (e.g., sex varies per patient), extract from a supplementary table, not from cohort-level text
- When text describes patient disease status or model organism genotype, look up the corresponding schema fields from `fetch_schema_properties()` and reason about what values apply
- For age and age-unit: extract numeric value and unit separately. Map "weeks" → `Weeks`, "months" → `Months`, "years" → `Years` (verify these strings against the schema enum for the age-unit field)
- Always validate extracted values against the schema enum before applying. If the exact text value isn't in the enum, find the closest enum entry. If no close match exists, do not set the field — log it as a controlled vocabulary gap.

---

## Source Tier 4 — Data File Inspection

Read actual data files only after exhausting all upstream metadata. This is the last resort for fields that genuinely aren't documented anywhere else.

### h5ad / AnnData files (scRNA-seq, snATAC-seq)

```python
import h5py, json

def inspect_h5ad_obs(local_path: str) -> dict:
    """
    Extract obs column names and sample values from an h5ad file.
    Returns: {column_name: [sample_values...]} for all obs columns.
    h5ad is an HDF5 file — read without loading full matrix into memory.
    """
    obs_meta = {}
    with h5py.File(local_path, 'r') as f:
        obs = f.get('obs')
        if obs is None:
            return {}
        # Categorical columns are stored as codes + categories
        for col_name in obs.keys():
            try:
                col = obs[col_name]
                if isinstance(col, h5py.Group) and 'categories' in col:
                    # Categorical: read unique categories
                    cats = [str(c.decode() if isinstance(c, bytes) else c) for c in col['categories'][:20]]
                    obs_meta[col_name] = cats
                elif isinstance(col, h5py.Dataset):
                    sample = col[:min(5, len(col))]
                    obs_meta[col_name] = [str(v.decode() if isinstance(v, bytes) else v) for v in sample]
            except Exception:
                continue
    return obs_meta
    # Useful obs columns often include: 'sample', 'batch', 'cell_type', 'cluster',
    #   'sex', 'age', 'diagnosis', 'patient', 'genotype', 'condition', 'tissue'
```

### BAM files — @RG header tags

```python
def inspect_bam_header(local_path_or_url: str) -> dict:
    """
    Read just the BAM header to extract @RG (read group) metadata.
    Use samtools view -H (does not require full download for indexed BAMs).
    """
    import subprocess
    result = subprocess.run(
        ['samtools', 'view', '-H', local_path_or_url],
        capture_output=True, text=True, timeout=30
    )
    rg_fields = {}
    for line in result.stdout.split('\n'):
        if not line.startswith('@RG'):
            continue
        for part in line.split('\t')[1:]:
            if ':' in part:
                tag, val = part.split(':', 1)
                rg_fields[tag] = val
    # Common @RG tags: ID, SM (sample), LB (library), PL (platform),
    #   PU (platform unit = flowcell.lane), CN (sequencing center), DS (description)
    return rg_fields
```

### FASTQ headers — instrument and run info

```python
import gzip

def inspect_fastq_header(url_or_path: str) -> dict:
    """
    Read the first FASTQ header line to extract Illumina instrument/run metadata.
    Illumina format: @{instrument}:{run_number}:{flowcell_id}:{lane}:{tile}:{x}:{y}
    Only downloads the first ~2KB — safe for large remote files if using HTTP Range.
    """
    try:
        resp = httpx.get(url_or_path, headers={'Range': 'bytes=0-2048'}, timeout=10)
        content = resp.content
        try:
            text = gzip.decompress(content).decode('utf-8', errors='ignore')
        except Exception:
            text = content.decode('utf-8', errors='ignore')
        first_line = text.split('\n')[0]
        if first_line.startswith('@'):
            parts = first_line.lstrip('@').split(':')
            return {'instrument': parts[0], 'run_number': parts[1] if len(parts) > 1 else '',
                    'flowcell_id': parts[2] if len(parts) > 2 else '',
                    'lane': parts[3] if len(parts) > 3 else ''}
    except Exception:
        pass
    return {}
```

### Count matrices (TSV/CSV) — column names as sample IDs

```python
def inspect_count_matrix_header(url_or_path: str, max_bytes: int = 4096) -> list[str]:
    """
    Read just the header row of a count matrix.
    Column names are typically sample IDs or cell IDs — use for specimen mapping.
    Works for .tsv, .csv, .txt (gzip-compressed or plain).
    """
    try:
        resp = httpx.get(url_or_path, headers={'Range': f'bytes=0-{max_bytes}'}, timeout=15)
        content = resp.content
        try:
            text = gzip.decompress(content).decode('utf-8', errors='ignore')
        except Exception:
            text = content.decode('utf-8', errors='ignore')
        first_line = text.split('\n')[0]
        delimiter = '\t' if '\t' in first_line else ','
        return first_line.split(delimiter)
    except Exception:
        return []
```

---

## Validation and Enum Matching

Before applying any value extracted from any source, validate it against the field's schema constraints:

```python
def validate_against_enum(raw_value: str, field_props: dict) -> str | None:
    """
    Given a raw value and schema field properties, return the valid enum entry
    (exact case) or None if no match.
    """
    enum_list = field_props.get('enum', [])
    if not enum_list:
        # Free-text field — any non-empty string is valid
        return raw_value.strip() if raw_value and raw_value.strip() else None

    raw_norm = raw_value.strip().lower()

    # 1. Exact match (case-insensitive)
    for entry in enum_list:
        if str(entry).lower() == raw_norm:
            return str(entry)   # return exact enum case

    # 2. Substring / prefix match for common abbreviations
    for entry in enum_list:
        if raw_norm in str(entry).lower() or str(entry).lower() in raw_norm:
            return str(entry)

    # 3. Domain-specific synonyms — built at runtime from the schema's actual enum values.
    # Do not hardcode schema-specific enum strings here. Instead, after calling
    # fetch_schema_properties(), inspect each enum list and build a synonym map for that
    # field based on common abbreviations of the values present.
    #
    # Universal synonyms that apply across any life-science portal:
    SYNONYMS = {
        'homo sapiens': 'Homo sapiens',
        'human': 'Homo sapiens',
        'mus musculus': 'Mus musculus',
        'mouse': 'Mus musculus',
        'rattus norvegicus': 'Rattus norvegicus',
        'rat': 'Rattus norvegicus',
        'female': 'Female', 'f': 'Female', 'male': 'Male', 'm': 'Male',
        'unknown': 'Unknown', 'not reported': 'Unknown',
        'n/a': 'Unknown', 'na': 'Unknown', 'not applicable': 'Unknown',
        'paired': 'Paired', 'paired-end': 'Paired',
        'single': 'Single', 'single-end': 'Single',
        'fresh frozen': 'Fresh Frozen', 'ffpe': 'FFPE',
        'reverse stranded': 'SecondStranded',
        'forward stranded': 'FirstStranded',
        'unstranded': 'Unstranded',
    }
    # Note: all target values above must be verified against the actual enum list at
    # runtime before applying — the `if candidate in enum_list` check below enforces this.
    if raw_norm in SYNONYMS:
        candidate = SYNONYMS[raw_norm]
        if candidate in enum_list:
            return candidate

    return None  # no match — do not set the field, log the gap
```

---

## Per-File Application

The gap-fill algorithm runs at the **file level** for Category A/B/D/E fields (which vary per sample), and at the **study level** for Category C fields (which are uniform).

```python
# Build a per-file annotation map before writing to Synapse
# sample_meta[run_accession] = dict of resolved per-sample values

def build_per_file_annotations(
    files: list[dict],          # [{file_id, name, current_annotations}]
    run_to_sample_meta: dict,   # run_accession → {field: validated_value, ...}
    study_level_fills: dict,    # field → validated_value (Category C fields)
    schema_props: dict,         # from fetch_schema_properties()
) -> dict:
    """
    Merge per-file resolved values with study-level fills.
    Returns {file_id: {field: value, ...}} — only new fields not already set.
    """
    import re
    result = {}
    for f in files:
        fid = f['file_id']
        new_anns = {}

        # Map file to run accession
        run_match = re.match(r'(SRR|ERR|DRR|GSM)\d+', f['name'])
        run_acc = run_match.group(0) if run_match else None
        per_sample = run_to_sample_meta.get(run_acc, {}) if run_acc else {}

        for field, props in schema_props.items():
            if field in f['current_annotations']:
                continue   # already set — don't overwrite
            # Per-sample fill first, then study-level
            val = per_sample.get(field) or study_level_fills.get(field)
            if val is not None:
                new_anns[field] = val

        result[fid] = new_anns
    return result
```

---

## Gap Report Format

After exhausting **all** sources (all four tiers), write a gap report. This becomes the `curation_notes` block posted in the GitHub curation comment.

**Before writing any field to `gap_not_in_source`, verify:**
- For Category B fields (sex, age, diagnosis, genotype, treatment): supplementary tables were attempted (`fetch_geo_supplementary_files` was called and at least one table was downloaded and inspected)
- For Category A fields (instrument, library prep, strand): PMC methods section was fetched and scanned for kit/protocol names
- For Category D fields (specimen ID, individual ID): the SRA run table was fetched and the per-sample BioSample XML was checked

A field in `gap_not_in_source` that skipped any of these mandatory sources is an incomplete gap-fill, not a legitimate gap.

```python
GAP_CATEGORIES = {
    'filled_tier1': [],    # value found in structured repo metadata
    'filled_tier2': [],    # value found in publication metadata
    'filled_tier3': [],    # value extracted from text via reasoning
    'filled_tier4': [],    # value from data file inspection
    'gap_not_in_source': [],   # field couldn't be populated — genuinely not in any source
    'gap_not_in_enum':   [],   # value found but not in schema enum (log raw value)
    'gap_not_applicable': [],  # field clearly N/A for this study type
}

# Write to audit_reasoning_fixes.json under each project's entry.
# Field names below are illustrative — use the actual schema field names from fetch_schema_properties():
# {
#   "project_id": "synXXX",
#   "gap_fill_report": {
#     "filled_tier1": ["<instrument field> = Illumina NovaSeq 6000", "<read-length field> = 150"],
#     "filled_tier2": ["<sex field> = Female (from PubMed abstract)", ...],
#     "filled_tier3": ["<dissociation field> = Collagenase IV (from PMC methods)", ...],
#     "filled_tier4": ["<model-system field> = P1 (from h5ad obs['sample'])"],
#     "gap_not_in_source": ["<age field>", "<age-unit field>"],
#     "gap_not_in_enum": ["<dissociation field>: raw value 'enzymatic dissociation' has no enum match"],
#     "gap_not_applicable": ["<antibody field>", "<perturbation field>"],
#   }
# }
```

---

## Integration Points

### During initial annotation (Step C in synapse_workflow.md)

After calling `fetch_schema_properties(schema_uri)` and building `normalized_annotations` from the primary source (ENA filereport + GEO SOFT), run the gap-fill algorithm immediately:

1. `missing = set(schema_props) - set(normalized_annotations) - never_set - empty_enum`
2. If `missing` is non-empty, run the gap-fill algorithm against Tier 1–4 sources
3. Merge gap-fill results into `normalized_annotations` before applying to File entities
4. Record what was filled from each tier in `normalized_annotations_sources.json`

### During audit (Step 7b in daily_task_template.md)

After Phase 1 auto-fixes, for each project with remaining `reasoning_gaps`:

1. Load `{WORKSPACE_DIR}/audit_results.json` to get per-project current annotation state
2. For each project, re-run the gap-fill algorithm against ALL tiers (Tier 1–4)
3. Apply results via `apply_audit_fixes.py`
4. Record all filled/unfilled fields in the gap report for the GitHub curation comment

### Priority rule: distinguish structured reads from reasoning guesses

The gap-fill distinguishes two categories of existing annotations:

**Safe to keep (do not re-examine):** Values that were set by directly reading a structured metadata column — e.g., ENA `read_count` → the read-depth field, ENA `library_layout` → the run-type field, filename extension → the file-format field, `scientific_name` → the species/organism field. These are ground truth reads; overwriting them would be wrong.

**Must re-examine:** Values that were set by interpreting free text, mapping a kit name string to an enum, or applying biological reasoning inline during `synapse_actions.py` generation. The gap-fill should re-derive these values through the Tier 1→4 source hierarchy:
- If the new derivation confirms the existing value: document the confirmation in the gap report
- If the new derivation produces a different value: correct it and document the correction with source evidence
- If no source can confirm or deny: keep the existing value, flag for human review with the note "set by inline reasoning, source not verified"

**How to identify re-examine candidates:** When reading the generated `synapse_actions.py`, look for fields whose values were derived by:
- Mapping a protocol or kit name string to a controlled vocabulary term (e.g., stranded library prep → strand orientation field)
- Inferring a disease classification or model organism genotype from the study description
- Applying biological context to guess a preparation method, extraction method, or experimental condition
- Any field where the comment or logic in the script is "I think" / "likely" / "inferred from" rather than "copied from column X"

Do not use this list to re-examine fields that are verbatim reads of structured repository columns — those are ground truth.
