# Discovery APIs — publication-first data discovery

This file contains the concrete API patterns and code examples for discovering
disease-relevant datasets. CLAUDE.md describes the high-level flow and when to
call these APIs; this file is the reference for *how*.

Read this file when you are:
- Querying PubMed for disease-relevant publications
- Resolving what data each paper deposited via NCBI elink / DataBankList /
  Europe PMC / CrossRef
- Running secondary-path repository-direct queries (DataCite, etc.)
- Classifying a publication group against the portal for deduplication

All search terms come from `config/keywords.yaml` — never hardcode disease
terms. All agent identity fields (contact email, user-agent) come from
`config/settings.yaml`.

---

## PubMed search

```python
from Bio import Entrez
import os, yaml

with open('config/settings.yaml') as f:
    cfg = yaml.safe_load(f)
with open('config/keywords.yaml') as f:
    kw = yaml.safe_load(f)

Entrez.email = cfg['agent']['contact_email']
if os.environ.get('NCBI_API_KEY'):
    Entrez.api_key = os.environ['NCBI_API_KEY']

mesh_query = kw['pubmed_mesh_query']
query = f'{mesh_query} AND ("{since_date}"[PDAT] : "3000"[PDAT])'

handle = Entrez.esearch(db='pubmed', term=query, retmax=200, usehistory='y')
search_results = Entrez.read(handle)
pmids = search_results['IdList']
```

### Batch fetch full PubMed records (title, abstract, authors, DOI)

```python
# Fetch one at a time to avoid DictionaryElement parsing issues
for pmid in pmids:
    handle = Entrez.efetch(db='pubmed', id=pmid, rettype='xml', retmode='xml')
    recs = Entrez.read(handle)
    record = recs['PubmedArticle'][0]
    # record['MedlineCitation']['Article']['ArticleTitle']
    # record['MedlineCitation']['Article']['Abstract']['AbstractText']
    # record['MedlineCitation']['Article']['AuthorList']
    # record['PubmedData']['ArticleIdList'] for DOI
```

---

## NCBI elink — find linked datasets

```python
# GEO datasets
handle = Entrez.elink(dbfrom='pubmed', db='gds', id=','.join(pmids))
link_results = Entrez.read(handle)

# SRA studies
handle = Entrez.elink(dbfrom='pubmed', db='sra', id=','.join(pmids))

# dbGaP
handle = Entrez.elink(dbfrom='pubmed', db='gap', id=','.join(pmids))
```

**CRITICAL — Verify elink accession ownership before using.**
NCBI elink frequently returns accessions from OTHER papers. For each GEO
accession, call `Entrez.esummary(db='gds', id=...)` and check the `PubMedIds`
field. If the PMID differs from the paper being processed, discard it.

---

## PubMed DataBankList — author-submitted accessions

```python
def get_pubmed_databanks(pmid_records: list) -> dict[str, list[str]]:
    results = {}
    for rec in pmid_records:
        pmid = str(rec['MedlineCitation']['PMID'])
        article = rec['MedlineCitation']['Article']
        databanks = article.get('DataBankList', [])
        entries = []
        for db in databanks:
            db_name = str(db.get('DataBankName', ''))
            accessions = [str(a) for a in db.get('AccessionNumberList', [])]
            if db_name and accessions:
                entries.append({'db': db_name, 'accessions': accessions})
        if entries:
            results[pmid] = entries
    return results

NON_DATA_DBS = {'PDB', 'UniProt', 'ClinicalTrials.gov', 'RefSeq', 'GenBank',
                'OMIM', 'PubChem', 'ChEMBL', 'RRID', 'INSDC'}
```

---

## Europe PMC annotations — ALL repository accessions in full text

```python
import httpx, time

def get_europepmc_accessions(pmid: str) -> list[dict]:
    resp = httpx.get(
        'https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds',
        params={'articleIds': f'MED:{pmid}', 'type': 'Accession Numbers', 'format': 'JSON'},
        timeout=15
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    accessions = []
    for article in data:
        for ann in article.get('annotations', []):
            acc_id = ann.get('exact', '') or ''
            provider = ann.get('provider', '') or ''
            # S-EPMC* accessions are auto-created BioStudies records for PMC articles
            # (supplementary PDFs/docs), never primary data deposits — always skip
            if acc_id.startswith('S-EPMC'):
                continue
            # The 'EuropePMC' provider refers to these same PMC supplementary bundles
            # Real data repositories report as 'GEO', 'ENA', 'EGA', etc.
            if provider == 'EuropePMC':
                continue
            accessions.append({'accession_id': acc_id, 'source': provider})
    return accessions
# 404 or empty for papers not in open-access PMC — skip and continue, this is normal
```

Europe PMC provider → repository: `GEO` → GEO, `ENA`/`SRA` → SRA/ENA, `EGA` → EGA,
`ArrayExpress` → ArrayExpress, `PRIDE` → PRIDE, `metabolights` → MetaboLights,
`Zenodo` → Zenodo, `Figshare` → Figshare.

**Never accept `S-EPMC*` accessions or `provider: EuropePMC` entries from the
annotations API.** These are auto-generated BioStudies records holding journal
supplementary files (PDFs, Word docs) — not research datasets.

---

## DataCite API — institutional and national repository datasets

```python
import httpx, time

def search_datacite(term: str, since_date: str, page_size: int = 50) -> list[dict]:
    """Search DataCite for disease-relevant datasets from any repository not otherwise covered."""
    results = []
    for page in range(1, 4):  # max 3 pages = 150 results per term
        resp = httpx.get(
            'https://api.datacite.org/dois',
            params={
                'query': term,
                'resource-type-id': 'dataset',
                'registered': f'{since_date},',  # ISO date filter
                'page[size]': page_size,
                'page[number]': page,
            },
            timeout=30
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        items = data.get('data', [])
        if not items:
            break
        for item in items:
            attrs = item.get('attributes', {})
            doi = attrs.get('doi', '')
            # Skip S-EPMC (Europe PMC supplementary bundles) and known-covered repos
            if doi.startswith('10.') and not attrs.get('doi', '').lower().startswith('s-epmc'):
                publisher = attrs.get('publisher', '')
                # Skip repos already covered by dedicated queries
                SKIP_PUBLISHERS = {'Zenodo', 'figshare', 'OSF', 'PRIDE', 'ArrayExpress',
                                   'MetaboLights', 'Dryad', 'Mendeley Data'}
                if any(s.lower() in publisher.lower() for s in SKIP_PUBLISHERS):
                    continue
                title = (attrs.get('titles') or [{}])[0].get('title', '')
                desc = (attrs.get('descriptions') or [{}])[0].get('description', '')
                creators = [c.get('name', '') for c in attrs.get('creators', [])[:4]]
                url = attrs.get('url', '')
                results.append({
                    'doi': doi, 'title': title, 'description': desc,
                    'creators': creators, 'publisher': publisher, 'url': url,
                    'source_repository': 'DataCite',
                    'discovery_path': 'datacite_api',
                })
        time.sleep(0.5)
    return results
# Call once per search term from config/keywords.yaml
```

---

## CrossRef relations — publisher-linked data repos

```python
import httpx, re

def get_crossref_data_links(doi: str) -> list[dict]:
    resp = httpx.get(
        f'https://api.crossref.org/works/{doi}',
        headers={'User-Agent': f'NADIA/1.0 ({cfg["agent"]["contact_email"]})'},
        timeout=15
    )
    if resp.status_code != 200:
        return []
    msg = resp.json().get('message', {})
    DATA_REPO_PATTERNS = [
        (r'zenodo\.org/(?:record|records)/(\d+)',                      'Zenodo'),
        (r'figshare\.com/articles?/(?:\w+/)+(\d+)',                   'Figshare'),
        (r'osf\.io/([a-z0-9]{4,8})',                                  'OSF'),
        (r'10\.5061/(dryad\.\S+)',                                    'Dryad'),
        (r'data\.mendeley\.com/datasets/([^/\s]+)',                   'Mendeley'),
        (r'(GSE\d{4,8})',                                             'GEO'),
        (r'(PXD\d{5,9})',                                             'PRIDE'),
        (r'(E-[A-Z]{3,6}-\d{3,6})',                                  'ArrayExpress'),
        (r'(EGAS\d{8,12})',                                           'EGA'),
        (r'(phs\d{6})',                                               'dbGaP'),
        (r'(MTBLS\d{3,6})',                                           'MetaboLights'),
        (r'cellxgene\.cziscience\.com/collections/([a-f0-9-]{36})',  'CELLxGENE'),
        (r'openneuro\.org/datasets/(ds\d{6})',                        'OpenNeuro'),
    ]
    found = []
    for field in ['relation', 'link', 'resource']:
        field_str = str(msg.get(field, {}))
        for pattern, repo in DATA_REPO_PATTERNS:
            for match in re.findall(pattern, field_str, re.IGNORECASE):
                accession = match if isinstance(match, str) else match[0]
                found.append({'repo': repo, 'accession_id': accession})
    seen = set()
    return [x for x in found if (k := (x['repo'], x['accession_id'])) not in seen and not seen.add(k)]
```

---

## Publication Group Schema

```json
{
  "pub_group_id": "pmid_41760889",
  "publication_title": "Pembrolizumab in advanced MPNSTs — a phase 2 trial",
  "pmid": "41760889",
  "doi": "10.1038/s41591-2025-xxxxx",
  "abstract": "...",
  "authors": ["Smith J", "Doe A"],
  "pub_date": "2025-12-15",
  "datasets": [
    {
      "accession_id": "GSE301187",
      "source_repository": "GEO",
      "discovery_path": "ncbi_elink",
      "data_url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE301187",
      "data_types": ["rnaSeq"],
      "file_formats": ["TXT.GZ"],
      "sample_count": 13,
      "access_type": "open"
    }
  ]
}
```

For secondary-path datasets (no PMID), use `"pmid": null` and derive
`"publication_title"` from the repository record title.

---

## Deduplication — classify a publication group

Before creating or modifying any Synapse project, classify each publication
group into exactly one of:

- **SKIP** — True duplicate: portal study exists (PMID/DOI/accession/high-confidence title match) AND all dataset accessions already present
- **ADD** — Partial match: publication exists but ≥1 new accession not yet in portal
- **NEW** — No match: create a new Synapse project

```python
def classify_publication_group(group, portal_studies_df, agent_state_set):
    # portal_studies_df columns: studyId, studyName, alternateDataRepository
    # (Note: syn52694652 has NO pmid or doi columns)

    # 1. Check agent state by accession
    known_accessions = {acc for acc, _ in agent_state_set}
    new_accessions = [d for d in group['datasets'] if d['accession_id'] not in known_accessions]
    if not new_accessions:
        return 'SKIP', None

    # 2. Exact accession match in portal alternateDataRepository
    # Cast column to str first — it serializes as NaN floats when empty
    adr_col = portal_studies_df['alternateDataRepository'].apply(
        lambda x: str(x) if x is not None else ''
    )
    for dataset in group['datasets']:
        acc = dataset['accession_id']
        # Check both bare accession and prefix:accession forms
        prefix = REPO_TO_PREFIX.get(dataset['source_repository'], '')
        for check_str in [acc, f'{prefix}:{acc}']:
            if adr_col.str.contains(check_str, regex=False, na=False).any():
                portal_match = portal_studies_df[adr_col.str.contains(check_str, regex=False, na=False)]
                return classify_add_or_skip(portal_match, group)

    # 3. Fuzzy title match — two signals
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np

    study_names = portal_studies_df['studyName'].fillna('').tolist()
    query_title = group['publication_title']

    # Signal A: TF-IDF cosine similarity
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), analyzer='word')
    corpus = study_names + [query_title]
    tfidf = vectorizer.fit_transform(corpus)
    cosine_scores = cosine_similarity(tfidf[-1], tfidf[:-1]).flatten()
    best_cosine_idx = int(np.argmax(cosine_scores))
    best_cosine = cosine_scores[best_cosine_idx]

    # Signal B: Jaccard unigram overlap
    q_tokens = set(query_title.lower().split())
    jaccard_scores = [
        len(q_tokens & set(name.lower().split())) / len(q_tokens | set(name.lower().split()))
        for name in study_names
    ] if study_names else []
    best_jaccard = max(jaccard_scores) if jaccard_scores else 0.0
    best_jaccard_idx = int(np.argmax(jaccard_scores)) if jaccard_scores else 0

    # Match if either signal is strong
    if best_cosine >= 0.85:
        portal_match = portal_studies_df.iloc[[best_cosine_idx]]
        return classify_add_or_skip(portal_match, group)
    if best_jaccard >= 0.50:
        portal_match = portal_studies_df.iloc[[best_jaccard_idx]]
        return classify_add_or_skip(portal_match, group)

    # Near-match warning: log but treat as NEW
    if best_cosine >= 0.70 or best_jaccard >= 0.30:
        print(f"  NEAR-MATCH: '{query_title}' (cosine={best_cosine:.2f}, jaccard={best_jaccard:.2f})")

    return 'NEW', None
```

`REPO_TO_PREFIX` lives in `prompts/repo_apis.md` — import or duplicate it as
needed.

**Known gotchas:**
- `syn52694652` has **no `pmid` or `doi` columns**. Do not query for them.
- `alternateDataRepository` column serializes as NaN floats when empty — always
  cast with `.apply(lambda x: str(x) if x is not None else '')` before string
  ops.
- NCBI elink false positives: always verify accession ownership (check GEO
  record's `PubMedIds` matches the paper being processed).
