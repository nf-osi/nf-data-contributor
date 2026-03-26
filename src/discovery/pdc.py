"""NCI Proteomics Data Commons (PDC) connector.

Uses the PDC GraphQL API to discover clinical proteomics studies related to NF/SWN.
"""

from __future__ import annotations

import time
from datetime import date

import httpx
import structlog

from .base import BaseConnector, StudyCandidate

_PDC_API = "https://pdc.cancer.gov/graphql"

_STUDIES_QUERY = """
query Studies {
  allPrograms {
    program_id
    program_name
    projects {
      project_id
      project_name
      studies {
        study_id
        study_name
        study_description
        primary_site
        disease_type
        analytical_fraction
        acquisition_type
        submitter_id_name
        pdc_study_id
      }
    }
  }
}
"""


class PdcConnector(BaseConnector):
    """Connector for NCI Proteomics Data Commons."""

    repository_name = "PDC"

    def fetch_candidates(self) -> list[StudyCandidate]:
        candidates: list[StudyCandidate] = []
        seen: set[str] = set()

        try:
            all_studies = self._fetch_all_studies()
            for study in all_studies:
                if len(candidates) >= self.max_results:
                    break
                if not self._is_relevant(study):
                    continue
                candidate = self._parse_study(study)
                if candidate and candidate.accession_id not in seen:
                    seen.add(candidate.accession_id)
                    candidates.append(candidate)
        except Exception:
            self.log.exception("pdc_fetch_error")

        return candidates

    def _fetch_all_studies(self) -> list[dict]:
        with httpx.Client(timeout=60) as client:
            resp = client.post(_PDC_API, json={"query": _STUDIES_QUERY})
            resp.raise_for_status()
            data = resp.json()

        studies = []
        for program in data.get("data", {}).get("allPrograms", []):
            for project in program.get("projects", []):
                for study in project.get("studies", []):
                    study["_program_name"] = program.get("program_name", "")
                    study["_project_name"] = project.get("project_name", "")
                    studies.append(study)
        return studies

    def _is_relevant(self, study: dict) -> bool:
        text = " ".join([
            study.get("study_name", ""),
            study.get("study_description", ""),
            study.get("disease_type", ""),
            study.get("primary_site", ""),
        ]).lower()
        return any(term.lower() in text for term in self.search_terms)

    def _parse_study(self, study: dict) -> StudyCandidate | None:
        pdc_id = study.get("pdc_study_id", "") or study.get("study_id", "")
        title = study.get("study_name", "")
        description = (study.get("study_description") or "")[:2000]

        return StudyCandidate(
            title=title,
            abstract=description,
            authors=[],
            source_repository=self.repository_name,
            accession_id=pdc_id,
            doi=None,
            pmid=None,
            publication_date=date.today(),
            data_types=["proteomics"],
            file_formats=["mzML", "RAW"],
            sample_count=None,
            access_type="open",
            data_url=f"https://pdc.cancer.gov/pdc/study/{pdc_id}",
            license=None,
            raw_metadata=study,
        )
