"""Synapse project creator.

Creates a new Synapse project following the NF Data Portal folder hierarchy,
populates it with ExternalLink pointer entities for each known data asset,
annotates all entities with NF-standard metadata, and creates a wiki page
summarizing the auto-discovered study.
"""

from __future__ import annotations

import json
from datetime import date

import synapseclient
from synapseclient import Project, Folder, Link, Wiki, Activity
import structlog

from src.discovery.base import StudyCandidate
from src.relevance.scorer import RelevanceResult
from .annotator import (
    build_project_annotations,
    build_file_annotations,
    apply_annotations,
)
from .templates import (
    NF_FOLDER_STRUCTURE,
    WIKI_TEMPLATE,
    build_project_name,
)

logger = structlog.get_logger(__name__)


class SynapseProjectCreator:
    """Creates NF Data Portal-compliant Synapse projects for discovered studies."""

    def __init__(
        self,
        syn: synapseclient.Synapse,
        parent_id: str | None = None,
        model_version: str = "claude-sonnet-4-6",
    ) -> None:
        self.syn = syn
        self.parent_id = parent_id
        self.model_version = model_version
        self.log = structlog.get_logger(self.__class__.__name__)

    def create_project(
        self,
        candidate: StudyCandidate,
        result: RelevanceResult,
    ) -> str:
        """Create a full Synapse project for a validated study.

        Returns the Synapse project ID (e.g. 'syn12345678').
        """
        project_name = build_project_name(
            candidate.source_repository, candidate.accession_id
        )
        self.log.info("creating_project", name=project_name)

        # 1. Create the project
        project_kwargs: dict = {"name": project_name}
        if self.parent_id:
            project_kwargs["parentId"] = self.parent_id

        project = self.syn.store(Project(**project_kwargs))
        project_id = project.id

        # 2. Apply project-level annotations
        project_annots = build_project_annotations(
            candidate, result.suggested_study_name
        )
        apply_annotations(self.syn, project_id, project_annots.as_dict())

        # 3. Create standard folder hierarchy
        folder_ids = self._create_folders(project_id)

        # 4. Create pointer entity (ExternalLink) in Raw Data folder
        self._create_pointer_entities(
            candidate, result, folder_ids["Raw Data"]
        )

        # 5. Store raw API metadata in Source Metadata folder
        self._store_source_metadata(
            candidate, folder_ids["Source Metadata"]
        )

        # 6. Create wiki page
        self._create_wiki(project_id, candidate, result)

        self.log.info("project_created", project_id=project_id, name=project_name)
        return project_id

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _create_folders(self, project_id: str) -> dict[str, str]:
        folder_ids: dict[str, str] = {}
        for folder_name in NF_FOLDER_STRUCTURE:
            folder = self.syn.store(
                Folder(name=folder_name, parentId=project_id)
            )
            folder_ids[folder_name] = folder.id
        return folder_ids

    def _create_pointer_entities(
        self,
        candidate: StudyCandidate,
        result: RelevanceResult,
        raw_data_folder_id: str,
    ) -> None:
        # Create a dataset sub-folder inside Raw Data
        dataset_folder = self.syn.store(
            Folder(
                name=f"{candidate.source_repository}_{candidate.accession_id}",
                parentId=raw_data_folder_id,
            )
        )
        dataset_folder_id = dataset_folder.id

        # Create one ExternalLink pointing to the data source URL
        link_entity = self.syn.store(
            Link(
                targetId=candidate.data_url,
                name=f"Source: {candidate.accession_id}",
                parentId=dataset_folder_id,
            )
        )

        # Annotate the link
        file_annots = build_file_annotations(candidate, result, result.suggested_study_name)
        apply_annotations(self.syn, link_entity.id, file_annots.as_dict())

        # Add provenance
        activity = Activity(
            name="NF Data Contributor Agent — auto-discovery",
            description=(
                f"Automatically discovered from {candidate.source_repository}. "
                f"Metadata extracted by {self.model_version}."
            ),
            used=[candidate.data_url],
        )
        self.syn.setProvenance(link_entity.id, activity)

    def _store_source_metadata(
        self,
        candidate: StudyCandidate,
        source_metadata_folder_id: str,
    ) -> None:
        """Save the raw API response JSON as a Synapse wiki or annotation."""
        metadata_json = json.dumps(candidate.raw_metadata, indent=2, default=str)
        wiki = Wiki(
            title="Original Source Metadata",
            owner=source_metadata_folder_id,
            markdown=f"```json\n{metadata_json[:50000]}\n```",
        )
        try:
            self.syn.store(wiki)
        except Exception:
            self.log.exception("wiki_metadata_store_error")

    def _create_wiki(
        self,
        project_id: str,
        candidate: StudyCandidate,
        result: RelevanceResult,
    ) -> None:
        wiki_content = WIKI_TEMPLATE.format(
            repository=candidate.source_repository,
            accession_id=candidate.accession_id,
            data_url=candidate.data_url,
            abstract=candidate.abstract[:2000],
            data_types=", ".join(candidate.data_types),
            file_formats=", ".join(candidate.file_formats),
            sample_count=candidate.sample_count or "Unknown",
            access_type=candidate.access_type,
            license=candidate.license or "Not specified",
            discovery_date=date.today().isoformat(),
            relevance_score=f"{result.relevance_score:.2f}",
            disease_focus=", ".join(result.disease_focus),
            assay_types=", ".join(result.assay_types),
            species=", ".join(result.species),
            tissue_types=", ".join(result.tissue_types),
            model_version=self.model_version,
        )
        wiki = Wiki(
            title="Study Overview",
            owner=project_id,
            markdown=wiki_content,
        )
        try:
            self.syn.store(wiki)
        except Exception:
            self.log.exception("wiki_create_error", project_id=project_id)
