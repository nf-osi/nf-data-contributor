"""SRA + dbGaP connector.

Searches NCBI SRA for NF/SWN-related sequencing studies.
dbGaP controlled-access studies that surface in SRA are included with
access_type='controlled'.
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta

import structlog
from Bio import Entrez

from .base import BaseConnector, StudyCandidate

logger = structlog.get_logger(__name__)


class SraDbGapConnector(BaseConnector):
    """Connector for NCBI SRA (and dbGaP studies surfaced via SRA)."""

    repository_name = "SRA"

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
        return f"({term_clause}) AND {date_clause}"

    def fetch_candidates(self) -> list[StudyCandidate]:
        query = self._build_query()
        self.log.info("sra_search", query=query[:120])

        handle = Entrez.esearch(db="sra", term=query, retmax=self.max_results)
        record = Entrez.read(handle)
        handle.close()

        ids = record.get("IdList", [])
        if not ids:
            return []

        candidates: list[StudyCandidate] = []
        for sra_id in ids:
            try:
                candidate = self._fetch_sra_record(sra_id)
                if candidate:
                    candidates.append(candidate)
                time.sleep(0.15)
            except Exception:
                self.log.exception("sra_record_error", sra_id=sra_id)

        return candidates

    def _fetch_sra_record(self, sra_id: str) -> StudyCandidate | None:
        handle = Entrez.efetch(db="sra", id=sra_id, rettype="runinfo", retmode="text")
        content = handle.read()
        handle.close()

        # Parse CSV-style runinfo
        lines = content.strip().split("\n")
        if len(lines) < 2:
            return None

        headers = lines[0].split(",")
        values = lines[1].split(",")
        row = dict(zip(headers, values))

        accession = row.get("Study", f"SRP{sra_id}")
        title = row.get("SampleName", "") or row.get("LibraryName", accession)
        abstract = row.get("ScientificName", "")
        pub_date_str = row.get("ReleaseDate", "")
        try:
            pub_date = date.fromisoformat(pub_date_str[:10])
        except (ValueError, TypeError):
            pub_date = date.today()

        dbgap_id = row.get("dbgap_study_accession", "")
        access_type = "controlled" if dbgap_id else "open"

        library_strategy = row.get("LibraryStrategy", "OTHER")
        data_types = self._map_library_strategy(library_strategy)

        return StudyCandidate(
            title=title,
            abstract=abstract,
            authors=[],
            source_repository=self.repository_name,
            accession_id=accession,
            doi=None,
            pmid=None,
            publication_date=pub_date,
            data_types=data_types,
            file_formats=["FASTQ", "BAM"],
            sample_count=None,
            access_type=access_type,
            data_url=f"https://www.ncbi.nlm.nih.gov/sra/{accession}",
            license=None,
            raw_metadata=row,
        )

    @staticmethod
    def _map_library_strategy(strategy: str) -> list[str]:
        mapping = {
            "RNA-Seq": "rnaSeq",
            "WGS": "wholeGenomeSeq",
            "WXS": "wholeExomeSeq",
            "CHIP-Seq": "ChIPSeq",
            "ATAC-seq": "ATACSeq",
            "Bisulfite-Seq": "bisulfiteSeq",
            "miRNA-Seq": "miRNASeq",
            "OTHER": "other",
        }
        return [mapping.get(strategy, "other")]
