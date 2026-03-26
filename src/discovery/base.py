"""Abstract base class and shared data model for repository connectors."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class StudyCandidate:
    """Canonical representation of a candidate study from any repository."""

    title: str
    abstract: str
    authors: list[str]
    source_repository: str          # e.g. 'GEO', 'Zenodo'
    accession_id: str               # e.g. 'GSE123456', '10.5281/zenodo.1234'
    doi: str | None
    pmid: str | None
    publication_date: date
    data_types: list[str]           # e.g. ['rnaSeq', 'proteomics']
    file_formats: list[str]         # e.g. ['FASTQ', 'mzML']
    sample_count: int | None
    access_type: str                # 'open' | 'controlled' | 'embargoed'
    data_url: str                   # Direct link to the dataset
    license: str | None
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.access_type not in {"open", "controlled", "embargoed"}:
            raise ValueError(f"Invalid access_type: {self.access_type!r}")

    @property
    def unique_key(self) -> str:
        """Return a stable string key for deduplication."""
        return f"{self.source_repository}:{self.accession_id}"


class BaseConnector(ABC):
    """Abstract base for all repository connectors.

    Subclasses must implement :meth:`fetch_candidates`.
    """

    #: Short identifier used in logs and in StudyCandidate.source_repository
    repository_name: str = "UNKNOWN"

    def __init__(
        self,
        search_terms: list[str],
        max_results: int = 50,
        lookback_days: int = 30,
    ) -> None:
        self.search_terms = search_terms
        self.max_results = max_results
        self.lookback_days = lookback_days
        self.log = structlog.get_logger(self.__class__.__name__)

    @abstractmethod
    def fetch_candidates(self) -> list[StudyCandidate]:
        """Query the repository and return a list of StudyCandidates."""
        ...

    def run(self) -> list[StudyCandidate]:
        """Public entry point with top-level error handling."""
        self.log.info("connector_start", repository=self.repository_name)
        try:
            candidates = self.fetch_candidates()
            self.log.info(
                "connector_done",
                repository=self.repository_name,
                count=len(candidates),
            )
            return candidates
        except Exception:
            self.log.exception(
                "connector_error", repository=self.repository_name
            )
            return []
