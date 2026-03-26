"""Zenodo connector.

Uses the Zenodo REST API v3 to search for NF/SWN-related records.
Only records of type 'dataset' are returned.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import httpx
import structlog

from .base import BaseConnector, StudyCandidate

logger = structlog.get_logger(__name__)

_ZENODO_API = "https://zenodo.org/api/records"


class ZenodoConnector(BaseConnector):
    """Connector for Zenodo datasets."""

    repository_name = "Zenodo"

    def fetch_candidates(self) -> list[StudyCandidate]:
        candidates: list[StudyCandidate] = []
        cutoff = (date.today() - timedelta(days=self.lookback_days)).isoformat()

        for term in self.search_terms:
            try:
                results = self._search(term, cutoff)
                candidates.extend(results)
                if len(candidates) >= self.max_results:
                    break
                time.sleep(0.5)
            except Exception:
                self.log.exception("zenodo_search_error", term=term)

        # Deduplicate by accession within this connector
        seen: set[str] = set()
        unique: list[StudyCandidate] = []
        for c in candidates:
            if c.accession_id not in seen:
                seen.add(c.accession_id)
                unique.append(c)
        return unique[: self.max_results]

    def _search(self, term: str, since: str) -> list[StudyCandidate]:
        params = {
            "q": f'"{term}" AND resource_type.type:dataset',
            "sort": "mostrecent",
            "size": 25,
            "page": 1,
        }
        with httpx.Client(timeout=30) as client:
            resp = client.get(_ZENODO_API, params=params)
            resp.raise_for_status()
            data = resp.json()

        results = []
        for hit in data.get("hits", {}).get("hits", []):
            candidate = self._parse_hit(hit)
            if candidate:
                results.append(candidate)
        return results

    def _parse_hit(self, hit: dict) -> StudyCandidate | None:
        meta = hit.get("metadata", {})
        record_id = str(hit.get("id", ""))
        doi = hit.get("doi") or meta.get("doi")

        title = meta.get("title", "")
        description = meta.get("description", "") or ""
        # Strip basic HTML tags from description
        import re
        description = re.sub(r"<[^>]+>", " ", description).strip()

        creators = [c.get("name", "") for c in meta.get("creators", [])]

        pub_date_str = meta.get("publication_date", "")
        try:
            pub_date = date.fromisoformat(pub_date_str[:10])
        except (ValueError, TypeError):
            pub_date = date.today()

        access_right = meta.get("access_right", "open")
        access_type = "open" if access_right == "open" else "controlled"

        license_info = meta.get("license", {})
        license_id = license_info.get("id") if isinstance(license_info, dict) else str(license_info)

        resource_type = meta.get("resource_type", {}).get("type", "")
        if resource_type not in ("dataset", "software"):
            return None

        file_formats = list({
            f.get("type", "").upper()
            for f in hit.get("files", [])
            if f.get("type")
        })

        zenodo_url = f"https://zenodo.org/record/{record_id}"

        return StudyCandidate(
            title=title,
            abstract=description[:2000],
            authors=creators,
            source_repository=self.repository_name,
            accession_id=doi or f"zenodo.{record_id}",
            doi=doi,
            pmid=None,
            publication_date=pub_date,
            data_types=["genomics"],  # refined by relevance scorer
            file_formats=file_formats,
            sample_count=None,
            access_type=access_type,
            data_url=zenodo_url,
            license=license_id,
            raw_metadata=hit,
        )
