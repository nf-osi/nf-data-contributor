"""MetaboLights connector.

Uses the EMBL-EBI MetaboLights REST API to discover metabolomics studies
related to NF/SWN.
"""

from __future__ import annotations

import time
from datetime import date

import httpx
import structlog

from .base import BaseConnector, StudyCandidate

_METABOLIGHTS_API = "https://www.ebi.ac.uk/metabolights/ws"


class MetaboLightsConnector(BaseConnector):
    """Connector for MetaboLights metabolomics data."""

    repository_name = "MetaboLights"

    def fetch_candidates(self) -> list[StudyCandidate]:
        candidates: list[StudyCandidate] = []
        seen: set[str] = set()

        study_ids = self._get_all_public_studies()

        # MetaboLights doesn't support full-text search; filter by title/description
        checked = 0
        for study_id in study_ids:
            if len(candidates) >= self.max_results:
                break
            if checked >= 500:  # safety cap
                break
            try:
                candidate = self._check_study(study_id)
                checked += 1
                if candidate and candidate.accession_id not in seen:
                    seen.add(candidate.accession_id)
                    candidates.append(candidate)
                time.sleep(0.2)
            except Exception:
                self.log.exception("metabolights_study_error", study_id=study_id)

        return candidates

    def _get_all_public_studies(self) -> list[str]:
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{_METABOLIGHTS_API}/studies/list")
            resp.raise_for_status()
            return resp.json().get("content", [])

    def _check_study(self, study_id: str) -> StudyCandidate | None:
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{_METABOLIGHTS_API}/studies/{study_id}/title")
            if resp.status_code != 200:
                return None
            title = resp.json().get("content", "")

        # Quick relevance pre-filter before fetching full metadata
        title_lower = title.lower()
        if not any(t.lower() in title_lower for t in self.search_terms):
            return None

        # Fetch full metadata only when relevant
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{_METABOLIGHTS_API}/studies/{study_id}/description")
            description = resp.json().get("content", "") if resp.status_code == 200 else ""

        return StudyCandidate(
            title=title,
            abstract=description[:2000],
            authors=[],
            source_repository=self.repository_name,
            accession_id=study_id,
            doi=None,
            pmid=None,
            publication_date=date.today(),
            data_types=["metabolomics"],
            file_formats=["mzML", "ISA-Tab"],
            sample_count=None,
            access_type="open",
            data_url=f"https://www.ebi.ac.uk/metabolights/{study_id}",
            license=None,
            raw_metadata={"title": title, "description": description},
        )
