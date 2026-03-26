"""PubMed + NCBI GEO connector.

Searches GEO DataSets via the NCBI Entrez E-utilities API using Biopython.
For each GEO dataset found, metadata is enriched with the linked PubMed record
when available.
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from xml.etree import ElementTree

import structlog
from Bio import Entrez

from .base import BaseConnector, StudyCandidate

logger = structlog.get_logger(__name__)

# GEO dataset types that likely contain primary experimental data
_PRIMARY_DATASET_TYPES = {"Expression profiling by array", "Expression profiling by high throughput sequencing",
                           "Genome binding/occupancy profiling by high throughput sequencing",
                           "Non-coding RNA profiling by high throughput sequencing",
                           "Methylation profiling by genome tiling array",
                           "Methylation profiling by high throughput sequencing",
                           "SNP genotyping by SNP array",
                           "Genome variation profiling by genome tiling array",
                           "Protein expression by protein array"}


class PubMedGeoConnector(BaseConnector):
    """Connector for NCBI GEO DataSets."""

    repository_name = "GEO"

    def __init__(
        self,
        search_terms: list[str],
        max_results: int = 50,
        lookback_days: int = 30,
        ncbi_email: str = "nf-data-contributor@sagebionetworks.org",
        ncbi_api_key: str | None = None,
    ) -> None:
        super().__init__(search_terms, max_results, lookback_days)
        self.ncbi_email = ncbi_email
        self.ncbi_api_key = ncbi_api_key or os.environ.get("NCBI_API_KEY")
        Entrez.email = self.ncbi_email
        if self.ncbi_api_key:
            Entrez.api_key = self.ncbi_api_key

    def _build_query(self) -> str:
        term_clause = " OR ".join(f'"{t}"[All Fields]' for t in self.search_terms)
        cutoff = date.today() - timedelta(days=self.lookback_days)
        date_clause = f'("{cutoff.strftime("%Y/%m/%d")}"[PDAT] : "3000"[PDAT])'
        return f"({term_clause}) AND {date_clause} AND gds[Filter]"

    def fetch_candidates(self) -> list[StudyCandidate]:
        query = self._build_query()
        self.log.info("geo_search", query=query[:120])

        handle = Entrez.esearch(db="gds", term=query, retmax=self.max_results)
        record = Entrez.read(handle)
        handle.close()

        ids = record.get("IdList", [])
        if not ids:
            return []

        candidates: list[StudyCandidate] = []
        for gds_id in ids:
            try:
                candidate = self._fetch_gds_record(gds_id)
                if candidate:
                    candidates.append(candidate)
                time.sleep(0.15)  # respect rate limits
            except Exception:
                self.log.exception("geo_record_error", gds_id=gds_id)

        return candidates

    def _fetch_gds_record(self, gds_id: str) -> StudyCandidate | None:
        handle = Entrez.esummary(db="gds", id=gds_id)
        records = Entrez.read(handle)
        handle.close()

        if not records:
            return None
        rec = records[0]

        title = str(rec.get("title", ""))
        summary = str(rec.get("summary", ""))
        accession = str(rec.get("Accession", f"GDS{gds_id}"))
        n_samples = int(rec.get("n_samples", 0)) or None

        pub_date_str = str(rec.get("PDAT", ""))
        try:
            pub_date = date.fromisoformat(pub_date_str[:10])
        except ValueError:
            pub_date = date.today()

        data_types_raw = str(rec.get("gdsType", ""))
        data_types = self._map_data_types(data_types_raw)

        pmids = [str(p) for p in rec.get("PubMedIds", [])]
        pmid = pmids[0] if pmids else None

        taxon = str(rec.get("taxon", ""))
        authors = [str(a) for a in rec.get("Contributor", [])][:5]

        geo_url = f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}"

        return StudyCandidate(
            title=title,
            abstract=summary,
            authors=authors,
            source_repository=self.repository_name,
            accession_id=accession,
            doi=None,
            pmid=pmid,
            publication_date=pub_date,
            data_types=data_types,
            file_formats=["SOFT", "TXT", "FASTQ"],
            sample_count=n_samples,
            access_type="open",
            data_url=geo_url,
            license=None,
            raw_metadata=dict(rec),
        )

    @staticmethod
    def _map_data_types(gds_type: str) -> list[str]:
        mapping = {
            "array": "microarray",
            "sequencing": "rnaSeq",
            "chip": "ChIPSeq",
            "methylation": "bisulfiteSeq",
            "snp": "SNPArray",
            "protein": "proteomics",
        }
        lower = gds_type.lower()
        result = []
        for key, val in mapping.items():
            if key in lower:
                result.append(val)
        return result or ["genomics"]
