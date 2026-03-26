"""Tests for the deduplication module."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.discovery.base import StudyCandidate
from src.deduplication.checker import DuplicateChecker


def _make_candidate(**overrides) -> StudyCandidate:
    defaults = dict(
        title="NF1 transcriptomics study",
        abstract="RNA-seq of NF1 tumors.",
        authors=["Doe A"],
        source_repository="GEO",
        accession_id="GSE11111",
        doi="10.1234/test.doi",
        pmid="99999999",
        publication_date=date(2024, 1, 1),
        data_types=["rnaSeq"],
        file_formats=["FASTQ"],
        sample_count=15,
        access_type="open",
        data_url="https://example.com/data",
        license=None,
    )
    defaults.update(overrides)
    return StudyCandidate(**defaults)


def _make_checker(
    portal_accessions: set[str] | None = None,
    portal_titles: list[str] | None = None,
    tracking_accessions: set[str] | None = None,
) -> DuplicateChecker:
    syn = MagicMock()
    checker = DuplicateChecker(syn=syn)
    checker._portal_accessions = portal_accessions or set()
    checker._portal_dois = set()
    checker._portal_pmids = set()
    checker._portal_titles = portal_titles or []
    checker._tracking_accessions = tracking_accessions or set()
    return checker


# ---------------------------------------------------------------------------
# Exact accession match
# ---------------------------------------------------------------------------


def test_exact_accession_match_found() -> None:
    checker = _make_checker(portal_accessions={"GSE11111"})
    assert checker.is_duplicate(_make_candidate()) is True


def test_exact_accession_match_not_found() -> None:
    checker = _make_checker(portal_accessions={"GSE99999"})
    assert checker.is_duplicate(_make_candidate()) is False


# ---------------------------------------------------------------------------
# Tracking table match
# ---------------------------------------------------------------------------


def test_tracking_table_match() -> None:
    checker = _make_checker(tracking_accessions={"GSE11111"})
    assert checker.is_duplicate(_make_candidate()) is True


# ---------------------------------------------------------------------------
# Fuzzy title match
# ---------------------------------------------------------------------------


def test_fuzzy_title_exact_match() -> None:
    title = "NF1 transcriptomics study"
    checker = _make_checker(portal_titles=[title])
    candidate = _make_candidate(accession_id="GSE22222", title=title)
    assert checker.is_duplicate(candidate) is True


def test_fuzzy_title_different_title() -> None:
    checker = _make_checker(portal_titles=["Unrelated immunology study"])
    candidate = _make_candidate(accession_id="GSE33333", title="NF1 RNA-seq of schwannomas")
    # Should not be a duplicate (very different title)
    assert checker.is_duplicate(candidate) is False


# ---------------------------------------------------------------------------
# No duplicate
# ---------------------------------------------------------------------------


def test_no_duplicate() -> None:
    checker = _make_checker(
        portal_accessions={"GSE00001"},
        portal_titles=["Some other study"],
    )
    candidate = _make_candidate(accession_id="GSE99999", title="Brand new NF2 dataset")
    assert checker.is_duplicate(candidate) is False
