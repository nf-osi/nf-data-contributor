"""Figshare connector.

Uses the Figshare API v2 to search for NF/SWN datasets.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import httpx
import structlog

from .base import BaseConnector, StudyCandidate

_FIGSHARE_API = "https://api.figshare.com/v2"


class FigshareConnector(BaseConnector):
    """Connector for Figshare datasets."""

    repository_name = "Figshare"

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
                self.log.exception("figshare_search_error", term=term)

        return candidates[: self.max_results]

    def _search(self, term: str) -> list[StudyCandidate]:
        cutoff = (date.today() - timedelta(days=self.lookback_days)).isoformat()
        payload = {
            "search_for": term,
            "item_type": 3,  # 3 = dataset
            "page_size": 25,
            "modified_since": cutoff,
            "order": "published_date",
            "order_direction": "desc",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(f"{_FIGSHARE_API}/articles/search", json=payload)
            resp.raise_for_status()
            items = resp.json()

        results = []
        for item in items:
            try:
                candidate = self._fetch_article(item["id"])
                if candidate:
                    results.append(candidate)
                time.sleep(0.2)
            except Exception:
                self.log.exception("figshare_article_error", article_id=item.get("id"))
        return results

    def _fetch_article(self, article_id: int) -> StudyCandidate | None:
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{_FIGSHARE_API}/articles/{article_id}")
            resp.raise_for_status()
            data = resp.json()

        doi = data.get("doi")
        accession = doi or f"figshare.{article_id}"

        pub_date_str = data.get("published_date", "")
        try:
            pub_date = date.fromisoformat(pub_date_str[:10])
        except (ValueError, TypeError):
            pub_date = date.today()

        authors = [a.get("full_name", "") for a in data.get("authors", [])]
        categories = [c.get("title", "") for c in data.get("categories", [])]
        file_formats = list({
            f.get("name", "").rsplit(".", 1)[-1].upper()
            for f in data.get("files", [])
            if "." in f.get("name", "")
        })

        license_info = data.get("license", {})
        license_name = license_info.get("name") if isinstance(license_info, dict) else None

        return StudyCandidate(
            title=data.get("title", ""),
            abstract=(data.get("description") or "")[:2000],
            authors=authors,
            source_repository=self.repository_name,
            accession_id=accession,
            doi=doi,
            pmid=None,
            publication_date=pub_date,
            data_types=categories or ["other"],
            file_formats=file_formats,
            sample_count=None,
            access_type="open",
            data_url=data.get("url_public_html", f"https://figshare.com/articles/{article_id}"),
            license=license_name,
            raw_metadata=data,
        )
