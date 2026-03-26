"""State tracking via Synapse tables.

Persists every study the agent has ever evaluated in a Synapse table within
the agent's own dedicated project. This prevents re-processing on subsequent
runs and provides an audit trail of all agent activity.

Tables maintained:
  - processed_studies: one row per evaluated study
  - agent_run_log: one row per daily execution
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import synapseclient
from synapseclient import Schema, Column, Table, RowSet, Row
import structlog

logger = structlog.get_logger(__name__)

# -------------------------------------------------------------------------
# Table schemas
# -------------------------------------------------------------------------

PROCESSED_STUDIES_COLUMNS: list[Column] = [
    Column(name="accession_id",      columnType="STRING",  maximumSize=128),
    Column(name="doi",               columnType="STRING",  maximumSize=256),
    Column(name="pmid",              columnType="STRING",  maximumSize=32),
    Column(name="source_repo",       columnType="STRING",  maximumSize=64),
    Column(name="run_date",          columnType="DATE"),
    Column(name="synapse_project_id",columnType="STRING",  maximumSize=32),
    Column(name="status",            columnType="STRING",  maximumSize=64),
    Column(name="relevance_score",   columnType="DOUBLE"),
    Column(name="disease_focus",     columnType="STRING",  maximumSize=256),
]

AGENT_RUN_LOG_COLUMNS: list[Column] = [
    Column(name="run_id",            columnType="STRING",  maximumSize=64),
    Column(name="run_date",          columnType="DATE"),
    Column(name="studies_found",     columnType="INTEGER"),
    Column(name="studies_created",   columnType="INTEGER"),
    Column(name="studies_skipped",   columnType="INTEGER"),
    Column(name="errors",            columnType="INTEGER"),
]

# Valid status values for processed_studies
class StudyStatus:
    DISCOVERED = "discovered"
    REJECTED_RELEVANCE = "rejected_relevance"
    REJECTED_DUPLICATE = "rejected_duplicate"
    SYNAPSE_CREATED = "synapse_created"
    APPROVED = "approved"
    ERROR = "error"


@dataclass
class RunSummary:
    studies_found: int = 0
    studies_created: int = 0
    studies_skipped: int = 0
    errors: int = 0


class StateTracker:
    """Manages state persistence for the NF Data Contributor Agent."""

    def __init__(
        self,
        syn: synapseclient.Synapse,
        state_project_id: str,
        processed_table_id: str | None = None,
        run_log_table_id: str | None = None,
    ) -> None:
        self.syn = syn
        self.state_project_id = state_project_id
        self.log = structlog.get_logger(self.__class__.__name__)

        self.processed_table_id = processed_table_id or self._get_or_create_table(
            "NF_DataContributor_ProcessedStudies",
            PROCESSED_STUDIES_COLUMNS,
        )
        self.run_log_table_id = run_log_table_id or self._get_or_create_table(
            "NF_DataContributor_RunLog",
            AGENT_RUN_LOG_COLUMNS,
        )
        self._run_id = str(uuid.uuid4())[:8]

    # ------------------------------------------------------------------
    # Table bootstrap
    # ------------------------------------------------------------------

    def _get_or_create_table(self, name: str, columns: list[Column]) -> str:
        """Return existing table ID or create a new one."""
        results = self.syn.tableQuery(
            f"SELECT id FROM syn.tables WHERE name = '{name}' AND parentId = '{self.state_project_id}'"
        ) if False else None  # placeholder: Synapse doesn't support this query

        try:
            children = list(self.syn.getChildren(self.state_project_id, includeTypes=["table"]))
            for child in children:
                if child["name"] == name:
                    self.log.info("table_found", name=name, id=child["id"])
                    return child["id"]
        except Exception:
            self.log.exception("table_lookup_error", name=name)

        schema = Schema(name=name, columns=columns, parent=self.state_project_id)
        table = self.syn.store(schema)
        self.log.info("table_created", name=name, id=table.id)
        return table.id

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def record_study(
        self,
        accession_id: str,
        source_repo: str,
        status: str,
        doi: str | None = None,
        pmid: str | None = None,
        synapse_project_id: str | None = None,
        relevance_score: float | None = None,
        disease_focus: list[str] | None = None,
    ) -> None:
        """Append one row to the processed_studies table."""
        row_data = [
            accession_id,
            doi or "",
            pmid or "",
            source_repo,
            int(date.today().strftime("%s")) * 1000,  # Synapse DATE = epoch ms
            synapse_project_id or "",
            status,
            relevance_score or 0.0,
            ", ".join(disease_focus or []),
        ]
        try:
            row_set = RowSet(
                tableId=self.processed_table_id,
                headers=[Column(name=c.name) for c in PROCESSED_STUDIES_COLUMNS],
                rows=[Row(row_data)],
            )
            self.syn.store(row_set)
        except Exception:
            self.log.exception("record_study_error", accession=accession_id)

    def log_run(self, summary: RunSummary) -> None:
        """Append a run summary row to the agent_run_log table."""
        row_data = [
            self._run_id,
            int(date.today().strftime("%s")) * 1000,
            summary.studies_found,
            summary.studies_created,
            summary.studies_skipped,
            summary.errors,
        ]
        try:
            row_set = RowSet(
                tableId=self.run_log_table_id,
                headers=[Column(name=c.name) for c in AGENT_RUN_LOG_COLUMNS],
                rows=[Row(row_data)],
            )
            self.syn.store(row_set)
            self.log.info(
                "run_logged",
                run_id=self._run_id,
                created=summary.studies_created,
                skipped=summary.studies_skipped,
                errors=summary.errors,
            )
        except Exception:
            self.log.exception("log_run_error")
