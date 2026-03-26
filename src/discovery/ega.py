"""European Genome-phenome Archive (EGA) connector.

EGA datasets are controlled-access. The agent discovers them via the
EGA REST API and marks them access_type='controlled'. No actual download
credentials are needed — only metadata is retrieved.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import httpx
import structlog

from .base import BaseConnector, StudyCandidate

_EGA_API = "https://ega-archive.org/metadata/v2"


class EgaConnector(BaseConnector):
    """Connector for EGA (European Genome-phenome Archive)."""

    repository_name = "EGA"

    def fetch_candidates(self) -> list[StudyCandidate]:
        candidates: list[StudyCandidate] = []
        seen: set[str] = set()

        for term in self.search_terms:
            try:
                hits = self._search(term)
                for h in hits:
                    if h.accession_id not in seen:
                        seen.add(h.accession_id)
                        candidates.append(h)
                if len(candidates) >= self.max_results:
                    break
                time.sleep(0.5)
            except Exception:
                self.log.exception("ega_search_error", term=term)

        return candidates[: self.max_results]

    def _search(self, term: str) -> list[StudyCandidate]:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{_EGA_API}/studies",
                params={"queryBy": "study", "query": term, "limit": 20, "skip": 0},
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for study in data.get("response", {}).get("result", []):
            candidate = self._parse_study(study)
            if candidate:
                results.append(candidate)
        return results

    def _parse_study(self, study: dict) -> StudyCandidate | None:
        accession = study.get("egaStableId", "")
        title = study.get("title", "")
        description = (study.get("description") or "")[:2000]

        pub_date_ms = study.get("publishedDate")
        if pub_date_ms:
            from datetime import datetime
            pub_date = datetime.fromtimestamp(pub_date_ms / 1000).date()
        else:
            pub_date = date.today()

        url = f"https://ega-archive.org/studies/{accession}"

        return StudyCandidate(
            title=title,
            abstract=description,
            authors=[],
            source_repository=self.repository_name,
            accession_id=accession,
            doi=None,
            pmid=None,
            publication_date=pub_date,
            data_types=["genomics"],
            file_formats=["VCF", "BAM", "FASTQ"],
            sample_count=study.get("numberOfSamples"),
            access_type="controlled",
            data_url=url,
            license=None,
            raw_metadata=study,
        )
