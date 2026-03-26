"""PRIDE / ProteomeXchange connector.

Uses the PRIDE REST API to discover proteomics datasets related to NF/SWN.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import httpx
import structlog

from .base import BaseConnector, StudyCandidate

_PRIDE_API = "https://www.ebi.ac.uk/pride/ws/archive/v2"


class PrideConnector(BaseConnector):
    """Connector for PRIDE / ProteomeXchange proteomics data."""

    repository_name = "PRIDE"

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
                self.log.exception("pride_search_error", term=term)

        return candidates[: self.max_results]

    def _search(self, term: str) -> list[StudyCandidate]:
        params = {
            "keyword": term,
            "pageSize": 20,
            "page": 0,
            "sortDirection": "DESC",
            "sortConditions": "submissionDate",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{_PRIDE_API}/projects", params=params)
            resp.raise_for_status()
            data = resp.json()

        results = []
        for project in data.get("_embedded", {}).get("projects", []):
            candidate = self._parse_project(project)
            if candidate:
                results.append(candidate)
        return results

    def _parse_project(self, project: dict) -> StudyCandidate | None:
        accession = project.get("accession", "")
        title = project.get("title", "")
        description = (project.get("projectDescription") or "")[:2000]

        submission_date = project.get("submissionDate", "")
        try:
            pub_date = date.fromisoformat(submission_date[:10])
        except (ValueError, TypeError):
            pub_date = date.today()

        authors = [lab.get("name", "") for lab in project.get("labPIs", [])]

        instruments = [i.get("name", "") for i in project.get("instruments", [])]
        doi = project.get("doi")

        url = f"https://www.ebi.ac.uk/pride/archive/projects/{accession}"

        return StudyCandidate(
            title=title,
            abstract=description,
            authors=authors,
            source_repository=self.repository_name,
            accession_id=accession,
            doi=doi,
            pmid=None,
            publication_date=pub_date,
            data_types=["proteomics"],
            file_formats=["mzML", "RAW"],
            sample_count=project.get("numberOfSamples"),
            access_type="open",
            data_url=url,
            license=None,
            raw_metadata=project,
        )
