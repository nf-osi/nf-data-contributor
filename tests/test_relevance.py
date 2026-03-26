"""Tests for the relevance scoring module."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.discovery.base import StudyCandidate
from src.relevance.scorer import RelevanceResult, RelevanceScorer


def _make_candidate(**overrides) -> StudyCandidate:
    defaults = dict(
        title="RNA-seq of NF1 plexiform neurofibromas",
        abstract="We profiled 30 NF1 patient plexiform neurofibroma samples by RNA-seq.",
        authors=["Smith J", "Doe A"],
        source_repository="GEO",
        accession_id="GSE99999",
        doi=None,
        pmid="12345678",
        publication_date=date(2024, 6, 1),
        data_types=["rnaSeq"],
        file_formats=["FASTQ"],
        sample_count=30,
        access_type="open",
        data_url="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE99999",
        license=None,
    )
    defaults.update(overrides)
    return StudyCandidate(**defaults)


def _make_scorer(**overrides) -> RelevanceScorer:
    defaults = dict(
        model="claude-sonnet-4-6",
        threshold=0.70,
        require_primary_data=True,
        min_sample_count=3,
        api_key="test-key",
    )
    defaults.update(overrides)
    return RelevanceScorer(**defaults)


def _mock_claude_response(payload: dict) -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text=json.dumps(payload))]
    return msg


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


@patch("src.relevance.scorer.anthropic.Anthropic")
def test_score_returns_result(mock_anthropic_cls: MagicMock) -> None:
    payload = {
        "relevance_score": 0.92,
        "disease_focus": ["NF1"],
        "assay_types": ["rnaSeq"],
        "species": ["Human"],
        "tissue_types": ["plexiform neurofibroma"],
        "is_primary_data": True,
        "access_notes": "",
        "suggested_study_name": "NF1_PN_rnaSeq_2024",
    }
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_claude_response(payload)
    mock_anthropic_cls.return_value = mock_client

    scorer = _make_scorer()
    candidate = _make_candidate()
    result = scorer.score(candidate)

    assert result is not None
    assert result.relevance_score == 0.92
    assert "NF1" in result.disease_focus
    assert result.is_primary_data is True


@patch("src.relevance.scorer.anthropic.Anthropic")
def test_score_returns_none_on_bad_json(mock_anthropic_cls: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text="not valid json")]
    )
    mock_anthropic_cls.return_value = mock_client

    scorer = _make_scorer()
    result = scorer.score(_make_candidate())
    assert result is None


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_passes_filters_high_score() -> None:
    scorer = _make_scorer()
    candidate = _make_candidate()
    result = RelevanceResult(
        relevance_score=0.85,
        disease_focus=["NF1"],
        assay_types=["rnaSeq"],
        species=["Human"],
        tissue_types=["plexiform neurofibroma"],
        is_primary_data=True,
        access_notes="",
        suggested_study_name="Test",
    )
    assert scorer.passes_filters(candidate, result) is True


def test_passes_filters_low_score() -> None:
    scorer = _make_scorer()
    candidate = _make_candidate()
    result = RelevanceResult(
        relevance_score=0.50,
        disease_focus=[],
        assay_types=[],
        species=[],
        tissue_types=[],
        is_primary_data=True,
        access_notes="",
        suggested_study_name="Test",
    )
    assert scorer.passes_filters(candidate, result) is False


def test_passes_filters_not_primary_data() -> None:
    scorer = _make_scorer(require_primary_data=True)
    candidate = _make_candidate()
    result = RelevanceResult(
        relevance_score=0.95,
        disease_focus=["NF1"],
        assay_types=["rnaSeq"],
        species=["Human"],
        tissue_types=[],
        is_primary_data=False,
        access_notes="",
        suggested_study_name="Test",
    )
    assert scorer.passes_filters(candidate, result) is False


def test_passes_filters_too_few_samples() -> None:
    scorer = _make_scorer(min_sample_count=10)
    candidate = _make_candidate(sample_count=2)
    result = RelevanceResult(
        relevance_score=0.90,
        disease_focus=["NF1"],
        assay_types=["rnaSeq"],
        species=["Human"],
        tissue_types=[],
        is_primary_data=True,
        access_notes="",
        suggested_study_name="Test",
    )
    assert scorer.passes_filters(candidate, result) is False
