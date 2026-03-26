"""ArrayExpress / EMBL-EBI BioStudies connector.

Searches the BioStudies API for NF/SWN-related studies that include
ArrayExpress and other multi-omics submissions.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import httpx
import structlog

from .base import BaseConnector, StudyCandidate

_BIOSTUDIES_API = "https://www.ebi.ac.uk/biostudies/api/v1"


class ArrayExpressConnector(BaseConnector):
    """Connector for ArrayExpress via the BioStudies API."""

    repository_name = "ArrayExpress"

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
                time.sleep(0.3)
            except Exception:
                self.log.exception("arrayexpress_search_error", term=term)

        return candidates[: self.max_results]

    def _search(self, term: str) -> list[StudyCandidate]:
        cutoff = (date.today() - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")
        params = {
            "query": term,
            "pageSize": 25,
            "page": 1,
        }
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{_BIOSTUDIES_API}/search", params=params)
            resp.raise_for_status()
            data = resp.json()

        results = []
        for hit in data.get("hits", []):
            candidate = self._parse_hit(hit)
            if candidate:
                results.append(candidate)
        return results

    def _parse_hit(self, hit: dict) -> StudyCandidate | None:
        accession = hit.get("accession", "")
        title = hit.get("title", "")
        description = (hit.get("description") or "")[:2000]

        release_date_str = hit.get("releaseDate") or hit.get("creationDate") or ""
        try:
            pub_date = date.fromisoformat(release_date_str[:10])
        except (ValueError, TypeError):
            pub_date = date.today()

        authors = [a.get("name", "") for a in hit.get("authors", [])]

        repo = (hit.get("source") or "").upper()
        url = f"https://www.ebi.ac.uk/biostudies/studies/{accession}"

        return StudyCandidate(
            title=title,
            abstract=description,
            authors=authors,
            source_repository=self.repository_name,
            accession_id=accession,
            doi=None,
            pmid=None,
            publication_date=pub_date,
            data_types=["microarray"],
            file_formats=[],
            sample_count=None,
            access_type="open",
            data_url=url,
            license=None,
            raw_metadata=hit,
        )
