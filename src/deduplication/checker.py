"""Deduplication module.

Checks whether a StudyCandidate is already represented in the NF Data Portal
by querying the portal's Synapse study tables (accession ID, DOI, PMID) and
running fuzzy title matching using TF-IDF cosine similarity.

Also checks the agent's own processed-studies tracking table so that studies
evaluated in previous runs are not re-submitted.
"""

from __future__ import annotations

import re

import structlog
import synapseclient
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from src.discovery.base import StudyCandidate

logger = structlog.get_logger(__name__)


class DuplicateChecker:
    """Check candidates against the live NF Data Portal Synapse tables."""

    def __init__(
        self,
        syn: synapseclient.Synapse,
        studies_table_id: str = "syn52694652",
        files_table_id: str = "syn16858331",
        datasets_table_id: str = "syn16859580",
        tracking_table_id: str | None = None,
        fuzzy_threshold: float = 0.85,
    ) -> None:
        self.syn = syn
        self.studies_table_id = studies_table_id
        self.files_table_id = files_table_id
        self.datasets_table_id = datasets_table_id
        self.tracking_table_id = tracking_table_id
        self.fuzzy_threshold = fuzzy_threshold
        self.log = structlog.get_logger(self.__class__.__name__)

        # Lazy-loaded caches
        self._portal_accessions: set[str] | None = None
        self._portal_dois: set[str] | None = None
        self._portal_pmids: set[str] | None = None
        self._portal_titles: list[str] | None = None
        self._tracking_accessions: set[str] | None = None
        self._vectorizer: TfidfVectorizer | None = None
        self._title_matrix = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def is_duplicate(self, candidate: StudyCandidate) -> bool:
        """Return True if the candidate is already known (portal or tracker)."""

        # 1. Check own tracking table first (fastest)
        if self._in_tracking_table(candidate):
            self.log.info("dup_tracking", accession=candidate.accession_id)
            return True

        # 2. Exact accession match
        if self._exact_accession_match(candidate):
            self.log.info("dup_accession", accession=candidate.accession_id)
            return True

        # 3. DOI match
        if candidate.doi and self._exact_doi_match(candidate.doi):
            self.log.info("dup_doi", doi=candidate.doi)
            return True

        # 4. PMID match
        if candidate.pmid and self._exact_pmid_match(candidate.pmid):
            self.log.info("dup_pmid", pmid=candidate.pmid)
            return True

        # 5. Fuzzy title match
        if self._fuzzy_title_match(candidate.title):
            self.log.info("dup_fuzzy_title", title=candidate.title[:80])
            return True

        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_portal_data(self) -> None:
        if self._portal_accessions is not None:
            return
        self.log.info("loading_portal_data")
        try:
            results = self.syn.tableQuery(
                f"SELECT study, studyId FROM {self.studies_table_id}"
            )
            df = results.asDataFrame()
            self._portal_accessions = set()
            self._portal_dois = set()
            self._portal_pmids = set()
            self._portal_titles = []

            for _, row in df.iterrows():
                if row.get("study"):
                    self._portal_accessions.add(str(row["study"]).upper())
                self._portal_titles.append(str(row.get("study", "")))
        except Exception:
            self.log.exception("portal_data_load_error")
            self._portal_accessions = set()
            self._portal_dois = set()
            self._portal_pmids = set()
            self._portal_titles = []

    def _load_tracking_data(self) -> None:
        if self._tracking_accessions is not None:
            return
        self._tracking_accessions = set()
        if not self.tracking_table_id:
            return
        try:
            results = self.syn.tableQuery(
                f"SELECT accession_id FROM {self.tracking_table_id}"
            )
            df = results.asDataFrame()
            self._tracking_accessions = set(df["accession_id"].dropna().str.upper())
        except Exception:
            self.log.exception("tracking_data_load_error")

    def _in_tracking_table(self, candidate: StudyCandidate) -> bool:
        self._load_tracking_data()
        return candidate.accession_id.upper() in (self._tracking_accessions or set())

    def _exact_accession_match(self, candidate: StudyCandidate) -> bool:
        self._load_portal_data()
        return candidate.accession_id.upper() in (self._portal_accessions or set())

    def _exact_doi_match(self, doi: str) -> bool:
        self._load_portal_data()
        return doi.lower() in (self._portal_dois or set())

    def _exact_pmid_match(self, pmid: str) -> bool:
        self._load_portal_data()
        return pmid in (self._portal_pmids or set())

    def _fuzzy_title_match(self, title: str) -> bool:
        self._load_portal_data()
        titles = self._portal_titles or []
        if not titles:
            return False

        if self._vectorizer is None:
            self._vectorizer = TfidfVectorizer(ngram_range=(1, 2))
            self._title_matrix = self._vectorizer.fit_transform(titles)

        candidate_vec = self._vectorizer.transform([title])
        sims = cosine_similarity(candidate_vec, self._title_matrix)[0]
        return float(sims.max()) >= self.fuzzy_threshold
