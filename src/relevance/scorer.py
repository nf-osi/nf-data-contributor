"""Relevance scoring via the Anthropic Claude API.

Each StudyCandidate is evaluated by Claude which returns a structured JSON
assessment of NF/SWN relevance, disease focus, and extracted metadata.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import anthropic
import structlog

from src.discovery.base import StudyCandidate

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are an expert biomedical curator for the Neurofibromatosis (NF) Data Portal.
Your task is to assess whether a scientific dataset is relevant to NF1, NF2, schwannomatosis (SWN),
or related conditions (MPNST, plexiform neurofibroma, vestibular schwannoma, etc.).

You MUST respond with valid JSON only — no additional text before or after the JSON object.
"""

_USER_TEMPLATE = """Evaluate the following dataset for inclusion in the NF Data Portal.

Title: {title}

Abstract/Description:
{abstract}

Source Repository: {repository}
Accession ID: {accession_id}

Return a JSON object with exactly these fields:
{{
  "relevance_score": <float 0.0-1.0, how central NF/SWN is to this study>,
  "disease_focus": <list of strings from: ["NF1", "NF2", "SWN", "MPNST", "NF-general"]>,
  "assay_types": <list of NF Portal vocab terms e.g. ["rnaSeq", "wholeGenomeSeq", "LC-MS"]>,
  "species": <list e.g. ["Human", "Mouse"]>,
  "tissue_types": <list e.g. ["neurofibroma", "schwannoma", "Schwann cell"]>,
  "is_primary_data": <bool, true if original experimental data vs review/meta-analysis>,
  "access_notes": <string, any caveats about data availability or restrictions>,
  "suggested_study_name": <string, clean descriptive name following NF Portal conventions>
}}
"""


@dataclass
class RelevanceResult:
    """Structured output from the Claude relevance scorer."""

    relevance_score: float
    disease_focus: list[str]
    assay_types: list[str]
    species: list[str]
    tissue_types: list[str]
    is_primary_data: bool
    access_notes: str
    suggested_study_name: str


class RelevanceScorer:
    """Scores StudyCandidates using the Claude API."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        threshold: float = 0.70,
        require_primary_data: bool = True,
        min_sample_count: int | None = 3,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.threshold = threshold
        self.require_primary_data = require_primary_data
        self.min_sample_count = min_sample_count
        self.client = anthropic.Anthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"]
        )
        self.log = structlog.get_logger(self.__class__.__name__)

    def score(self, candidate: StudyCandidate) -> RelevanceResult | None:
        """Call Claude and return a RelevanceResult, or None on failure."""
        user_msg = _USER_TEMPLATE.format(
            title=candidate.title,
            abstract=candidate.abstract[:3000],
            repository=candidate.source_repository,
            accession_id=candidate.accession_id,
        )
        for attempt in range(3):
            try:
                message = self.client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_msg}],
                )
                text = message.content[0].text
                data = json.loads(text)
                return RelevanceResult(
                    relevance_score=float(data.get("relevance_score", 0.0)),
                    disease_focus=data.get("disease_focus", []),
                    assay_types=data.get("assay_types", []),
                    species=data.get("species", []),
                    tissue_types=data.get("tissue_types", []),
                    is_primary_data=bool(data.get("is_primary_data", True)),
                    access_notes=str(data.get("access_notes", "")),
                    suggested_study_name=str(data.get("suggested_study_name", candidate.title)),
                )
            except json.JSONDecodeError:
                self.log.warning("scorer_json_parse_error", attempt=attempt + 1)
            except anthropic.RateLimitError:
                self.log.warning("scorer_rate_limit", attempt=attempt + 1)
                time.sleep(2 ** attempt)
            except Exception:
                self.log.exception("scorer_error", accession=candidate.accession_id)
                break
        return None

    def passes_filters(
        self,
        candidate: StudyCandidate,
        result: RelevanceResult,
    ) -> bool:
        """Return True if the candidate meets all configured thresholds."""
        if result.relevance_score < self.threshold:
            self.log.info(
                "filtered_low_relevance",
                accession=candidate.accession_id,
                score=result.relevance_score,
            )
            return False

        if self.require_primary_data and not result.is_primary_data:
            self.log.info(
                "filtered_not_primary_data",
                accession=candidate.accession_id,
            )
            return False

        if (
            self.min_sample_count is not None
            and candidate.sample_count is not None
            and candidate.sample_count < self.min_sample_count
        ):
            self.log.info(
                "filtered_too_few_samples",
                accession=candidate.accession_id,
                n=candidate.sample_count,
            )
            return False

        return True
