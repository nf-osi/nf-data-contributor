# Repository File Enumeration APIs

This file contains per-repository `get_file_list_*` implementations and the general file enumeration algorithm. Read this file when you need to enumerate files from any supported repository.

---

## General File Enumeration Algorithm

Apply this pattern for **every** repository. The goal is always: enumerate individual files with direct download URLs. No file count cap — always enumerate all files regardless of count.

```python
def populate_dataset_with_files(syn, files_folder_id, accession_id, repository, landing_url):
    """
    General pattern used for all repositories.
    Returns the number of File entities created.
    """
    files = get_file_list(accession_id, repository)  # repo-specific, see below
    # files = list of (filename: str, download_url: str)

    if not files:
        # No enumerable files (controlled access, API error, etc.)
        # Fall back to a single landing-page link
        syn.store(File(
            name=f'Source: {accession_id}',
            parentId=files_folder_id,
            synapseStore=False,
            path=landing_url,
        ))
        return 0

    for filename, download_url in files:
        syn.store(File(
            name=filename,
            parentId=files_folder_id,
            synapseStore=False,
            path=download_url,
        ))
    return len(files)
```

**Note on filenames with special characters:** Synapse rejects URLs containing unencoded spaces, commas, or slashes. Always encode filenames in URLs:

```python
from urllib.parse import quote

def encoded_download_url(base_url, filename):
    return base_url + quote(filename, safe='')

def sanitize_entity_name(filename):
    """Make filename safe for use as a Synapse entity name."""
    return filename.replace('/', '-').replace(',', '').strip()
```

---

## GEO — Supplementary Files + Linked SRA Runs

Most GEO series have supplementary processed files AND raw reads in SRA. Enumerate both.

```python
import re, httpx
from Bio import Entrez

def get_file_list_geo(gds_numeric_id: str, geo_accession: str) -> list[tuple[str, str]]:
    files = []

    # Part A: GEO supplementary files (processed counts, matrices, etc.)
    handle = Entrez.efetch(db='gds', id=gds_numeric_id, rettype='soft', retmode='text')
    soft_text = handle.read()
    ftp_urls = re.findall(r'!Series_supplementary_file\s*=\s*(ftp://\S+)', soft_text)
    for url in ftp_urls:
        filename = url.rstrip('/').split('/')[-1]
        https_url = url.replace('ftp://ftp.ncbi.nlm.nih.gov/', 'https://ftp.ncbi.nlm.nih.gov/')
        files.append((filename, https_url))

    # Part B: linked SRA runs → per-run FASTQ via ENA
    link_handle = Entrez.elink(dbfrom='gds', db='sra', id=gds_numeric_id)
    link_records = Entrez.read(link_handle)
    sra_ids = [l['Id'] for ls in link_records
               for db in ls.get('LinkSetDb', [])
               for l in db.get('Link', [])]

    for sra_id in sra_ids:
        run_handle = Entrez.efetch(db='sra', id=sra_id, rettype='runinfo', retmode='text')
        runinfo_csv = run_handle.read().strip()
        lines = runinfo_csv.split('\n')
        if len(lines) < 2:
            continue
        headers = lines[0].split(',')
        for line in lines[1:]:
            row = dict(zip(headers, line.split(',')))
            srr = row.get('Run', '')
            if not srr:
                continue
            run_files = get_sra_run_fastq_urls(srr)
            files.extend(run_files)

    return files
```

Put both GEO supplementary files AND SRA FASTQ files inside the **same** `GEO_{AccessionID}` Dataset entity.

---

## SRA Run FASTQ URLs

```python
def get_sra_run_fastq_urls(srr: str) -> list[tuple[str, str]]:
    """
    Get direct FASTQ (or CRAM/BAM) URLs for a single SRR accession.
    Only returns open, human-readable raw formats — never .sra format files.
    Tries ENA filereport first (preferred — stable FTP URLs).
    Falls back to NCBI SDL API requesting only fastq/cram/bam.
    Returns [] if only .sra format is available (caller should fall back to BioProject link).
    """
    RAW_FORMATS = ('.fastq', '.fastq.gz', '.fq', '.fq.gz', '.cram', '.bam')

    # 1. Try ENA filereport — stable https:// FTP URLs, always FASTQ
    try:
        ena_resp = httpx.get(
            'https://www.ebi.ac.uk/ena/portal/api/filereport',
            params={'accession': srr, 'result': 'read_run',
                    'fields': 'run_accession,fastq_ftp,submitted_ftp', 'format': 'json'},
            timeout=15
        )
        if ena_resp.status_code == 200:
            results = []
            for record in ena_resp.json():
                for ftp_field in ['fastq_ftp', 'submitted_ftp']:
                    for ftp_path in record.get(ftp_field, '').split(';'):
                        if ftp_path and any(ftp_path.lower().endswith(ext) for ext in RAW_FORMATS):
                            results.append((ftp_path.split('/')[-1], 'https://' + ftp_path))
            if results:
                return results
    except Exception:
        pass

    # 2. Fallback: NCBI SRA SDL API — request fastq specifically
    # Only use if ENA mirror is not yet available. Never accept .sra format.
    for filetype in ['fastq', 'cram', 'bam']:
        try:
            sdl_resp = httpx.get(
                'https://locate.ncbi.nlm.nih.gov/sdl/2/retrieve',
                params={'acc': srr, 'location': 's3.us-east-1', 'filetype': filetype},
                timeout=15
            )
            if sdl_resp.status_code == 200:
                results = []
                for bundle in sdl_resp.json().get('result', []):
                    if bundle.get('status') != 200:
                        continue
                    for f in bundle.get('files', []):
                        fname = f.get('name', '')
                        if fname.endswith('.sra') or f.get('type') == 'sra':
                            continue
                        for loc in f.get('locations', []):
                            url = loc.get('link', '')
                            if url and not url.endswith('.sra'):
                                if not fname:
                                    fname = url.split('/')[-1].split('?')[0]
                                results.append((fname, url))
                if results:
                    return results
        except Exception:
            pass

    return []
```

---

## SRA (Standalone, Not via GEO)

```python
def get_file_list_sra(sra_study_accession: str) -> list[tuple[str, str]]:
    files = []
    try:
        resp = httpx.get(
            'https://www.ebi.ac.uk/ena/portal/api/filereport',
            params={'accession': sra_study_accession, 'result': 'read_run',
                    'fields': 'run_accession,fastq_ftp,submitted_ftp', 'format': 'json'},
            timeout=30
        )
        if resp.status_code == 200:
            for record in resp.json():
                for ftp_field in ['fastq_ftp', 'submitted_ftp']:
                    for ftp_path in record.get(ftp_field, '').split(';'):
                        if ftp_path:
                            files.append((ftp_path.split('/')[-1], 'https://' + ftp_path))
    except Exception:
        pass

    if files:
        return files

    # Fallback: fetch run list from NCBI runinfo, then get URLs via SDL
    from Bio import Entrez
    try:
        handle = Entrez.esearch(db='sra', term=sra_study_accession)
        search = Entrez.read(handle)
        for sra_id in search.get('IdList', []):
            run_handle = Entrez.efetch(db='sra', id=sra_id, rettype='runinfo', retmode='text')
            runinfo_csv = run_handle.read()
            if isinstance(runinfo_csv, bytes):
                runinfo_csv = runinfo_csv.decode('utf-8')
            lines = runinfo_csv.strip().split('\n')
            headers = lines[0].split(',') if lines else []
            for line in lines[1:]:
                row = dict(zip(headers, line.split(',')))
                srr = row.get('Run', '')
                if srr:
                    files.extend(get_sra_run_fastq_urls(srr))
    except Exception:
        pass

    return files
```

---

## ENA (Direct BioProject Accessions)

```python
def get_file_list_ena(accession: str) -> list[tuple[str, str]]:
    """Get FASTQ files from ENA filereport, expanding all per-run URLs."""
    r = httpx.get(
        'https://www.ebi.ac.uk/ena/portal/api/filereport',
        params={'accession': accession, 'result': 'read_run',
                'fields': 'run_accession,fastq_ftp,submitted_ftp',
                'format': 'json'},
        timeout=30
    )
    if r.status_code != 200:
        return []
    files = []
    for row in r.json():
        for ftp_field in ['fastq_ftp', 'submitted_ftp']:
            ftps = [f.strip() for f in row.get(ftp_field, '').split(';') if f.strip()]
            for ftp_path in ftps:
                fname = ftp_path.split('/')[-1]
                url = 'https://' + ftp_path
                files.append((fname, url))
            if ftps:
                break  # use fastq_ftp if available, don't also add submitted_ftp
    return files
```

---

## Zenodo

```python
def get_file_list_zenodo(record_id: str) -> list[tuple[str, str]]:
    from urllib.parse import quote
    resp = httpx.get(f'https://zenodo.org/api/records/{record_id}', timeout=15)
    resp.raise_for_status()
    data = resp.json()
    files = []
    for f in data.get('files', []):
        filename = f.get('key', '') or f.get('filename', '')
        # v3 API: links.self is the download URL
        download_url = f.get('links', {}).get('self') or f.get('links', {}).get('download')
        if not download_url and filename:
            # Construct URL manually with encoding
            encoded = quote(filename, safe='')
            download_url = f'https://zenodo.org/api/records/{record_id}/files/{encoded}/content'
        if filename and download_url:
            safe_name = filename.replace('/', '-').replace(',', '').strip()
            files.append((safe_name, download_url))
    return files
```

---

## Figshare

```python
def get_file_list_figshare(article_id: str) -> list[tuple[str, str]]:
    resp = httpx.get(f'https://api.figshare.com/v2/articles/{article_id}', timeout=15)
    resp.raise_for_status()
    data = resp.json()
    files = []
    for f in data.get('files', []):
        filename = f.get('name', '')
        download_url = f.get('download_url', '')
        if filename and download_url:
            files.append((filename, download_url))
    return files
```

---

## OSF

```python
def get_file_list_osf(node_id: str) -> list[tuple[str, str]]:
    files = []
    url = f'https://api.osf.io/v2/nodes/{node_id}/files/osfstorage/'
    while url:
        resp = httpx.get(url, timeout=15)
        if resp.status_code != 200:
            break
        data = resp.json()
        for item in data.get('data', []):
            if item.get('attributes', {}).get('kind') == 'file':
                name = item['attributes'].get('name', '')
                download_url = item.get('links', {}).get('download', '')
                if name and download_url:
                    files.append((name, download_url))
        url = data.get('links', {}).get('next')  # follow pagination
    return files
```

---

## ArrayExpress / BioStudies

```python
def get_file_list_arrayexpress(accession: str) -> list[tuple[str, str]]:
    resp = httpx.get(
        f'https://www.ebi.ac.uk/biostudies/api/v1/studies/{accession}/info',
        timeout=15
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    files = []
    for section in data.get('section', {}).get('subsections', []):
        for link in section.get('links', []):
            url = link.get('url', '')
            name = link.get('attributes', {}).get('name', url.split('/')[-1])
            if url and (url.startswith('ftp://') or url.startswith('https://')):
                https_url = url.replace('ftp://ftp.ebi.ac.uk/', 'https://ftp.ebi.ac.uk/')
                files.append((name, https_url))
    return files
```

---

## PRIDE / ProteomeXchange

```python
def get_file_list_pride(accession: str) -> list[tuple[str, str]]:
    files = []
    page = 0
    while True:
        resp = httpx.get(
            f'https://www.ebi.ac.uk/pride/ws/archive/v2/projects/{accession}/files',
            params={'page': page, 'pageSize': 100, 'sortConditions': 'fileName'},
            timeout=15
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        # Handle both list response and dict with _embedded
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get('_embedded', {}).get('files', [])
        else:
            items = []
        if not items:
            break
        for f in items:
            name = f.get('fileName', '')
            download_url = f.get('downloadLink', '')
            if name and download_url:
                https_url = download_url.replace('ftp://', 'https://')
                files.append((name, https_url))
        if not data.get('_links', {}).get('next') if isinstance(data, dict) else True:
            break
        page += 1
    return files
```

**Note:** PRIDE API response can be a list or a dict — always handle both shapes (see above).

---

## MetaboLights

```python
def get_file_list_metabolights(accession: str) -> list[tuple[str, str]]:
    resp = httpx.get(
        f'https://www.ebi.ac.uk/metabolights/ws/studies/{accession}/files',
        timeout=15
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    files = []
    for f in data.get('study', []):
        name = f.get('file', '')
        ftp_base = f'https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public/{accession}/'
        if name and not f.get('directory', False):
            files.append((name, ftp_base + name))
    return files
```

---

## Mendeley Data

```python
def get_file_list_mendeley(dataset_id: str) -> list[tuple[str, str]]:
    """
    dataset_id: the slug from data.mendeley.com/datasets/{slug}/{version}
    Also accepts a DOI like '10.17632/abc123.1' — extract the slug from the path.
    """
    import re
    slug_match = re.search(r'10\.17632/([^./]+)', dataset_id)
    if slug_match:
        dataset_id = slug_match.group(1)

    files = []
    for version in range(5, 0, -1):  # try versions 5 down to 1
        resp = httpx.get(
            f'https://data.mendeley.com/api/datasets/{dataset_id}/versions/{version}',
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            for f in data.get('files', []):
                filename = f.get('filename', '')
                download_url = (f.get('content_details', {}).get('download_url')
                                or f.get('download_url', ''))
                if filename and download_url:
                    files.append((filename, download_url))
            if files:
                return files
        elif resp.status_code == 404:
            continue
    return files
```

---

## EGA (Controlled Access — No Direct Download)

```python
def get_file_list_ega(accession: str) -> list[tuple[str, str]]:
    return []  # always falls back to landing page; access requires application
# Use path= pointing to: https://ega-archive.org/studies/{accession}
# Set accessType=controlled in annotations
```

---

## dbGaP (Controlled Access — No Direct Download)

```python
def get_file_list_dbgap(accession: str) -> list[tuple[str, str]]:
    return []  # always falls back to landing page; access requires dbGaP application
# Use path= pointing to:
#   https://www.ncbi.nlm.nih.gov/projects/gap/cgi-bin/study.cgi?study_id={accession}
# Set accessType=controlled in annotations
```

---

## NCI PDC

```python
def get_file_list_pdc(pdc_study_id: str) -> list[tuple[str, str]]:
    query = f"""{{
      fileMetadata(pdc_study_id: "{pdc_study_id}" acceptDUA: true) {{
        file_name
        file_location
        file_size
        md5sum
        signedUrl {{ url }}
      }}
    }}"""
    resp = httpx.post('https://pdc.cancer.gov/graphql', json={'query': query}, timeout=30)
    if resp.status_code != 200:
        return []
    files = []
    for f in resp.json().get('data', {}).get('fileMetadata', []):
        name = f.get('file_name', '')
        url = f.get('signedUrl', {}).get('url', '') or f.get('file_location', '')
        if name and url:
            files.append((name, url))
    return files
# Note: signedUrls expire (~1 hour). Consider using file_location (stable S3 path) if available.
```

---

## MassIVE

```python
def get_file_list_massive(accession: str) -> list[tuple[str, str]]:
    """
    If the MassIVE dataset has a linked PRIDE/ProteomeXchange accession (PXDxxxxxx),
    use get_file_list_pride() instead — it has more reliable file enumeration.
    MassIVE accession alone: fall back to ExternalLink at
      https://massive.ucsd.edu/ProteoSAFe/dataset.jsp?task={accession}
    """
    return []  # MassIVE file enumeration API is complex; PRIDE mirror preferred
```

---

## CELLxGENE

```python
def get_file_list_cellxgene(collection_id: str) -> list[tuple[str, str]]:
    """
    collection_id: UUID from https://cellxgene.cziscience.com/collections/{uuid}
    Returns H5AD/RDS download links for each dataset in the collection.
    """
    import re
    resp = httpx.get(
        f'https://api.cellxgene.cziscience.com/curation/v1/collections/{collection_id}',
        timeout=15
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    files = []
    for dataset in data.get('datasets', []):
        dataset_title = dataset.get('title', dataset.get('dataset_id', ''))
        for asset in dataset.get('dataset_assets', []):
            filetype = asset.get('filetype', '')  # 'H5AD' or 'RDS'
            url = asset.get('url', '')
            if url and filetype in ('H5AD', 'RDS'):
                safe_title = re.sub(r'[^\w\s-]', '', dataset_title)[:80].strip()
                filename = f"{safe_title}.{filetype.lower()}"
                files.append((filename, url))
    return files
# Landing page: https://cellxgene.cziscience.com/collections/{collection_id}
```

---

## TCIA (The Cancer Imaging Archive)

```python
def get_file_list_tcia(collection_name: str) -> list[tuple[str, str]]:
    """
    TCIA collections contain thousands of DICOM files — never enumerate.
    Always fall back to ExternalLink.
    """
    return []
# Use path= pointing to:
#   https://www.cancerimagingarchive.net/collection/{collection_name}
# Set fileFormat=DICOM in annotations, accessType=open
```

---

## OpenNeuro

```python
def get_file_list_openneuro(dataset_id: str) -> list[tuple[str, str]]:
    """
    dataset_id: ds000xxx format
    OpenNeuro datasets are in BIDS format and can contain hundreds of files.
    """
    query = """
    query($datasetId: ID!) {
      dataset(id: $datasetId) {
        latestSnapshot {
          files {
            filename
            urls
          }
        }
      }
    }
    """
    resp = httpx.post(
        'https://openneuro.org/crn/graphql',
        json={'query': query, 'variables': {'datasetId': dataset_id}},
        timeout=30
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    files = []
    for f in (data.get('data', {}).get('dataset', {})
              .get('latestSnapshot', {}).get('files', [])):
        filename = f.get('filename', '')
        urls = f.get('urls', [])
        if filename and urls:
            files.append((filename, urls[0]))
    return files
# Landing page: https://openneuro.org/datasets/{dataset_id}
```

---

## File Format Normalization

Strip compression suffixes before mapping extension to schema enum values:

```python
import re

def normalize_file_format(filename: str) -> str:
    """Map filename extension to NF schema fileFormat enum value."""
    name = re.sub(r'\.(gz|zip|bz2|xz)$', '', filename.lower())
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    FORMAT_MAP = {
        'fastq': 'fastq', 'fq': 'fastq',
        'bam': 'bam', 'cram': 'cram', 'sam': 'sam',
        'vcf': 'vcf', 'bcf': 'bcf',
        'txt': 'txt', 'tsv': 'tsv', 'csv': 'csv',
        'h5': 'h5', 'h5ad': 'h5ad', 'hdf5': 'hdf5',
        'mtx': 'mtx', 'rds': 'rds', 'rda': 'rda',
        'bed': 'bed', 'bigwig': 'bigwig', 'bw': 'bigwig',
        'pdf': 'pdf', 'png': 'png', 'tiff': 'tiff',
        'xlsx': 'xlsx', 'xml': 'xml', 'json': 'json',
    }
    return FORMAT_MAP.get(ext, ext)
```

**Never** use compressed forms as fileFormat values (`fastq.gz` → `fastq`, `txt.gz` → `txt`).

---

## Important Notes

- **No file count cap**: Always enumerate all files. Do not impose arbitrary per-dataset limits.
- **Never create .sra format file entities**: SRA format requires sra-tools to decompress. If only `.sra` files are available, fall back to a BioProject landing page link.
- **ENA is the preferred source for SRA runs**: ENA provides stable `https://` FTP URLs. NCBI SDL API is the fallback.
- **Zenodo filenames with spaces/commas**: Must URL-encode with `quote(filename, safe='')`.
- **PRIDE API shape**: Response can be a list or `{'_embedded': {'files': [...]}}` — handle both.
