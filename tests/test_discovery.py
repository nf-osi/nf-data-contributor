"""Tests for discovery connectors and the StudyCandidate model."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.discovery.base import StudyCandidate
from src.discovery.zenodo import ZenodoConnector
from src.discovery.figshare import FigshareConnector
from src.discovery.osf import OsfConnector


# ---------------------------------------------------------------------------
# StudyCandidate validation
# ---------------------------------------------------------------------------


def test_study_candidate_unique_key() -> None:
    candidate = StudyCandidate(
        title="Test Study",
        abstract="Abstract text.",
        authors=["Smith J"],
        source_repository="GEO",
        accession_id="GSE12345",
        doi=None,
        pmid=None,
        publication_date=date(2024, 1, 15),
        data_types=["rnaSeq"],
        file_formats=["FASTQ"],
        sample_count=10,
        access_type="open",
        data_url="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE12345",
        license=None,
    )
    assert candidate.unique_key == "GEO:GSE12345"


def test_study_candidate_invalid_access_type() -> None:
    with pytest.raises(ValueError, match="Invalid access_type"):
        StudyCandidate(
            title="Bad",
            abstract="",
            authors=[],
            source_repository="GEO",
            accession_id="GSE99",
            doi=None,
            pmid=None,
            publication_date=date.today(),
            data_types=[],
            file_formats=[],
            sample_count=None,
            access_type="PUBLIC",  # invalid
            data_url="https://example.com",
            license=None,
        )


# ---------------------------------------------------------------------------
# Zenodo connector
# ---------------------------------------------------------------------------


def _make_zenodo_hit(record_id: int = 1234, doi: str = "10.5281/zenodo.1234") -> dict:
    return {
        "id": record_id,
        "doi": doi,
        "metadata": {
            "title": "NF1 RNA-seq dataset",
            "description": "Transcriptomic analysis of NF1 tumors.",
            "publication_date": "2024-03-01",
            "access_right": "open",
            "license": {"id": "cc-by"},
            "resource_type": {"type": "dataset"},
            "creators": [{"name": "Smith, Jane"}],
        },
        "files": [{"type": "fastq"}],
    }


@patch("src.discovery.zenodo.httpx.Client")
def test_zenodo_parse_hit(mock_client_cls: MagicMock) -> None:
    connector = ZenodoConnector(
        search_terms=["neurofibromatosis"],
        max_results=5,
        lookback_days=30,
    )
    hit = _make_zenodo_hit()
    result = connector._parse_hit(hit)
    assert result is not None
    assert result.source_repository == "Zenodo"
    assert result.accession_id == "10.5281/zenodo.1234"
    assert result.access_type == "open"
    assert result.license == "cc-by"


def test_zenodo_parse_hit_non_dataset_returns_none() -> None:
    connector = ZenodoConnector(search_terms=["NF1"], max_results=5, lookback_days=30)
    hit = _make_zenodo_hit()
    hit["metadata"]["resource_type"]["type"] = "publication"
    result = connector._parse_hit(hit)
    assert result is None


# ---------------------------------------------------------------------------
# BaseConnector error handling
# ---------------------------------------------------------------------------


def test_connector_run_returns_empty_on_exception() -> None:
    connector = ZenodoConnector(search_terms=["NF1"], max_results=5, lookback_days=30)

    with patch.object(connector, "fetch_candidates", side_effect=RuntimeError("boom")):
        results = connector.run()

    assert results == []
