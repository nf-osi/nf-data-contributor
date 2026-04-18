# Annotation Gap-Fill Strategy

This file defines the **generalized, schema-driven gap-fill algorithm** used whenever file annotations are incomplete. Run it in two situations:

1. **During initial annotation** — after Step B raw metadata collection in `prompts/synapse_workflow.md`, and **before** any File entity is created or annotated. Gap-fill is the primary annotation pass, not a cleanup step. By the time `synapse_actions.py` writes annotations, every schema field that has a reachable source must already be populated.
2. **During audit (Step 7b)** — for any field that remains unset after Phase 1 auto-fixes. This is the remediation pass.

In both cases the algorithm is the same. The only difference is the starting point: in the initial pass you start from zero; in the audit pass you start from what Phase 1 already set.

## Required library imports

Use the shared helpers in `lib/` — do not re-author them in generated scripts:

```python
import sys, os
sys.path.insert(0, os.environ.get('AGENT_REPO_ROOT', '.') + '/lib')

from schema_properties import (
    fetch_schema_properties,
    validate_against_enum,
    is_empty_enum,
    is_never_on_files,
)
from gap_report import GapReport, SourceRef
```

`fetch_schema_properties` and `validate_against_enum` in `lib/schema_properties.py` are the single source of truth for schema introspection and enum mapping. `GapReport` in `lib/gap_report.py` is the single source of truth for recording provenance — every filled field carries a `SourceRef`, and every unresolved gap carries the list of tiers and sources that were actually attempted.

---

## The Algorithm

```python
report = GapReport(project_id=project_id, schema_uri=schema_uri, pass_='initial')  # or 'audit'

schema_props = fetch_schema_properties(schema_uri)
current_anns = aggregate_file_annotations(files_folder_id)
never_set    = is_never_on_files()
empty_enum   = {f for f, p in schema_props.items() if is_empty_enum(p)}
missing      = set(schema_props) - set(current_anns) - never_set - empty_enum

for field_name in missing:
    category = classify_field(field_name, schema_props[field_name])  # Category A-E
    tiers_attempted: list[int] = []
    sources_attempted: list[str] = []

    for source_type in SOURCE_PRIORITY[category]:
        tier = SOURCE_TIER[source_type]
        tiers_attempted.append(tier)
        sources_attempted.append(source_type)

        raw_value, source_ref = try_extract(source_type, field_name, schema_props[field_name], context)
        if raw_value is None:
            continue

        validated = validate_against_enum(raw_value, schema_props[field_name])
        if validated is None:
            # Value found but no enum match — record the approximation and stop this field
            report.add_approximation(
                field_name=field_name,
                raw_value=str(raw_value),
                mapped_to=None,
                available_enums=schema_props[field_name].get('enum', []),
                source=source_ref,
            )
            break

        if validated != str(raw_value).strip():
            # An enum mapping was applied — surface it so humans can verify
            report.add_approximation(
                field_name=field_name,
                raw_value=str(raw_value),
                mapped_to=validated,
                available_enums=schema_props[field_name].get('enum', []),
                source=source_ref,
            )
        report.add_filled(field_name, validated, source_ref)
        apply_annotation(field_name, validated)
        break
    else:
        report.add_gap(
            field_name=field_name,
            tiers_attempted=sorted(set(tiers_attempted)),
            sources_attempted=sources_attempted,
            reason='no value found in any source',
        )

with open(f'{WORKSPACE_DIR}/gap_report_{project_id}.json', 'w') as f:
    f.write(report.to_json())
```

`try_extract` returns `(raw_value, SourceRef)` where the `SourceRef` captures tier, source name, and a verification URL when available. Every call to `report.add_filled(...)` or `report.add_gap(...)` writes one row the reviewer will see in the GitHub curation comment — make the source name specific (e.g. `"ENA filereport"`, not `"repository"`) and include the URL when one exists.

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

Try Unpaywall first. If it returns no OA location, also try Semantic Scholar, which indexes full text for many papers not in PMC and provides structured abstract/methods extraction:

```python
def get_semantic_scholar_tldr(doi: str) -> dict:
    """Fetch Semantic Scholar record — tldr, abstract, and open-access PDF URL if available."""
    resp = httpx.get(
        f'https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}',
        params={'fields': 'title,abstract,tldr,openAccessPdf,authors'},
        timeout=15
    )
    if resp.status_code != 200:
        return {}
    data = resp.json()
    return {
        'abstract': data.get('abstract', ''),
        'tldr': (data.get('tldr') or {}).get('text', ''),
        'pdf_url': (data.get('openAccessPdf') or {}).get('url'),
    }
    # pdf_url is a direct open-access PDF link when available — fetch and parse for methods text
```

> **When PMC returns no full text and Unpaywall returns no OA location:** the paper may still be programmatically inaccessible even if it appears readable in a browser (institutional access, publisher "free to read" walls that block bots). Try Semantic Scholar as a last resort. If that also fails, flag the field for human review with an explicit note: *"paper appears accessible via institutional subscription but full text is not programmatically available — age/sex require a human with journal access to populate."*

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

Before applying any value extracted from any source, validate it against the field's schema constraints using `validate_against_enum` from `lib/schema_properties`:

```python
from schema_properties import validate_against_enum

validated = validate_against_enum(raw_value, schema_props[field_name])
if validated is None:
    # No close match — record as an approximation with mapped_to=None and do not set the field
    report.add_approximation(field_name, str(raw_value), mapped_to=None,
                             available_enums=schema_props[field_name].get('enum', []),
                             source=source_ref)
elif validated != str(raw_value).strip():
    # The value was mapped to a different enum entry — record the mapping for reviewer verification
    report.add_approximation(field_name, str(raw_value), mapped_to=validated,
                             available_enums=schema_props[field_name].get('enum', []),
                             source=source_ref)
    report.add_filled(field_name, validated, source_ref)
else:
    # Exact match — no approximation needed
    report.add_filled(field_name, validated, source_ref)
```

The library helper performs three matching stages in order: case-insensitive exact match against the enum, substantial substring match, and a universal synonym table (human/mouse/female/male/etc.) whose candidates must still appear in the schema enum to be accepted. For free-text fields (no enum), it returns the stripped raw value when non-empty. Schema-specific synonyms are built at runtime from the enum list for that field — do not add portal-specific strings to the library.

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

The gap report is produced by `lib/gap_report.py::GapReport`. Every filled field carries a `SourceRef` (tier, source name, verification URL when available); every gap carries `tiers_attempted` and `sources_attempted` so the reviewer can see that the upstream sources were actually consulted. The report is serialized as JSON per project and later rendered into the GitHub curation comment by `scripts/post_curation_comment.py`.

**Before writing any `report.add_gap(...)`, verify the source was actually attempted:**
- For Category B fields (sex, age, diagnosis, genotype, treatment): supplementary tables were attempted (`fetch_geo_supplementary_files` was called and at least one table was downloaded and inspected).
- For Category A fields (instrument, library prep, strand): the PMC methods section was fetched and scanned for kit/protocol names.
- For Category D fields (specimen ID, individual ID): the SRA run table was fetched and the per-sample BioSample XML was checked.

A gap whose `sources_attempted` does not include the required sources for its category is an incomplete gap-fill, not a legitimate gap. Reviewers will see the `sources_attempted` list in the comment — do not list sources you did not actually call.

### Recording filled values

```python
from gap_report import SourceRef

# Example: instrument from ENA filereport
report.add_filled(
    field_name='platform',
    value='Illumina NovaSeq 6000',
    source=SourceRef(
        name='ENA filereport',
        tier=1,
        url=f'https://www.ebi.ac.uk/ena/portal/api/filereport?accession={study_accession}&result=read_run',
        field_in_source='instrument_model',
    ),
)

# Example: sex from PMC methods (Tier 3 reasoning)
report.add_filled(
    field_name='sex',
    value='Female',
    source=SourceRef(
        name=f'PMC {pmcid} methods section',
        tier=3,
        url=f'https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/',
        notes='all patients described as female in methods',
    ),
)
```

### Recording enum approximations

When the raw source value does not match the schema enum exactly, always record the mapping so the reviewer can verify it or flag a vocabulary gap:

```python
# 'enzymatic dissociation' -> 'Enzymatic' when the enum has {Enzymatic, Mechanical, Unknown}
report.add_approximation(
    field_name='dissociationMethod',
    raw_value='enzymatic dissociation (Collagenase IV + DNase I)',
    mapped_to='Enzymatic',
    available_enums=schema_props['dissociationMethod'].get('enum', []),
    source=SourceRef(name=f'PMC {pmcid} methods section', tier=3,
                     url=f'https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/'),
)
```

If no close enum value exists, pass `mapped_to=None` and do not set the field — the reviewer will see it flagged.

### Recording a real gap

```python
report.add_gap(
    field_name='ageUnit',
    tiers_attempted=[1, 2],
    sources_attempted=['ENA filereport', 'GEO GSM characteristics',
                       'PMC methods', 'supplementary Table S1'],
    reason='paper reports age ranges ("young adult", "elderly") without numeric units',
)
```

### Serializing and posting

After the per-project loop completes, write the report and post the curation comment:

```python
gap_report_path = f'{WORKSPACE_DIR}/gap_report_{project_id}.json'
with open(gap_report_path, 'w') as f:
    f.write(report.to_json())

import subprocess, sys as _sys
subprocess.run([
    _sys.executable, 'scripts/post_curation_comment.py',
    '--issue-number', str(issue_number),
    '--gap-report-file', gap_report_path,
    '--synapse-project-id', project_id,
], check=False)  # non-fatal on failure
```

---

## Integration Points

### During initial annotation (Step C in synapse_workflow.md)

Gap-fill is the **primary** annotation pass, not a remediation step. It runs before `synapse_actions.py` creates or annotates any File entity — so the Step C output already contains every field a schema-defined source can resolve.

1. Call `fetch_schema_properties(schema_uri)` from `lib/schema_properties`.
2. Build `normalized_annotations` from structured metadata (ENA filereport, GEO SOFT, BioSample XML).
3. Compute `missing = set(schema_props) - set(normalized_annotations) - is_never_on_files() - empty_enum`.
4. If `missing` is non-empty, run the gap-fill algorithm against Tier 1–4 sources and update `normalized_annotations` with each valid result.
5. For every filled field, call `report.add_filled(...)` with a real `SourceRef`. For every gap, call `report.add_gap(...)` with the actual tiers and sources attempted.
6. Write `{WORKSPACE_DIR}/gap_report_{project_id}.json` (initial pass) and post it to the study-review issue via `scripts/post_curation_comment.py`.
7. Only then apply `normalized_annotations` to File entities.

Writing files twice — once with sparse annotations, then backfilling during audit — is exactly the pattern this pass exists to replace. If the gap report shows most fields coming from Tier 3 or Tier 4 during audit rather than Tier 1 during initial annotation, that is a sign Step C was skipped or ran incompletely.

### During audit (Step 7b in daily_task_template.md)

After Phase 1 auto-fixes, for each project with remaining gaps:

1. Load `{WORKSPACE_DIR}/audit_results.json` to get per-project current annotation state, and the initial-pass `gap_report_{project_id}.json` if it exists.
2. Instantiate a new `GapReport(project_id=..., pass_='audit')` for this pass. (Do NOT mutate the initial-pass report — keep the two as separate artifacts so the completeness trend is visible across passes.)
3. Re-run the gap-fill algorithm against ALL tiers (Tier 1–4) for any field still missing or flagged by Phase 1. Record each result via `report.add_filled(...)` / `report.add_approximation(...)` / `report.add_gap(...)` as in the initial pass.
4. Apply the newly resolved values via `apply_audit_fixes.py`.
5. Write `{WORKSPACE_DIR}/audit_gap_report_{project_id}.json` and post it via `scripts/post_curation_comment.py`. The comment header will say `Pass: audit` so reviewers can tell initial from audit passes.

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
