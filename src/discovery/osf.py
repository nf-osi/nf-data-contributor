"""Open Science Framework (OSF) connector.

Uses the OSF REST API v2 to search for NF/SWN datasets and projects.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import httpx
import structlog

from .base import BaseConnector, StudyCandidate

_OSF_API = "https://api.osf.io/v2"


class OsfConnector(BaseConnector):
    """Connector for OSF projects and datasets."""

    repository_name = "OSF"

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
                self.log.exception("osf_search_error", term=term)

        return candidates[: self.max_results]

    def _search(self, term: str) -> list[StudyCandidate]:
        cutoff = (date.today() - timedelta(days=self.lookback_days)).isoformat()
        params = {
            "q": term,
            "filter[type]": "project",
            "filter[date_created][gte]": cutoff,
            "page[size]": 10,
            "sort": "-date_modified",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{_OSF_API}/nodes/", params=params)
            resp.raise_for_status()
            data = resp.json()

        results = []
        for node in data.get("data", []):
            candidate = self._parse_node(node)
            if candidate:
                results.append(candidate)
        return results

    def _parse_node(self, node: dict) -> StudyCandidate | None:
        attrs = node.get("attributes", {})
        node_id = node.get("id", "")

        title = attrs.get("title", "")
        description = (attrs.get("description") or "")[:2000]

        date_created = attrs.get("date_created", "")
        try:
            pub_date = date.fromisoformat(date_created[:10])
        except (ValueError, TypeError):
            pub_date = date.today()

        is_public = attrs.get("public", False)
        access_type = "open" if is_public else "controlled"

        osf_url = f"https://osf.io/{node_id}/"

        return StudyCandidate(
            title=title,
            abstract=description,
            authors=[],
            source_repository=self.repository_name,
            accession_id=node_id,
            doi=None,
            pmid=None,
            publication_date=pub_date,
            data_types=["other"],
            file_formats=[],
            sample_count=None,
            access_type=access_type,
            data_url=osf_url,
            license=None,
            raw_metadata=node,
        )
