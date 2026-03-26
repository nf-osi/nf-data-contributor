"""State table bootstrap helper.

Ensures the two agent state tables exist in the given Synapse project,
creating them on first run. Returns their IDs.

This is pre-written stable boilerplate because:
- The table schema is a stable contract (changes require migrations)
- The create-if-not-exists pattern must be idempotent across retries
- Getting Synapse table creation wrong can cause duplicate tables
"""

from __future__ import annotations

import synapseclient
from synapseclient import Schema, Column


PROCESSED_STUDIES_TABLE_NAME = "NF_DataContributor_ProcessedStudies"
RUN_LOG_TABLE_NAME = "NF_DataContributor_RunLog"

PROCESSED_STUDIES_COLUMNS: list[Column] = [
    Column(name="accession_id",       columnType="STRING", maximumSize=128),
    Column(name="doi",                columnType="STRING", maximumSize=256),
    Column(name="pmid",               columnType="STRING", maximumSize=32),
    Column(name="source_repo",        columnType="STRING", maximumSize=64),
    Column(name="run_date",           columnType="DATE"),
    Column(name="synapse_project_id", columnType="STRING", maximumSize=32),
    Column(name="status",             columnType="STRING", maximumSize=64),
    Column(name="relevance_score",    columnType="DOUBLE"),
    Column(name="disease_focus",      columnType="STRING", maximumSize=256),
]

RUN_LOG_COLUMNS: list[Column] = [
    Column(name="run_id",           columnType="STRING",  maximumSize=64),
    Column(name="run_date",         columnType="DATE"),
    Column(name="studies_found",    columnType="INTEGER"),
    Column(name="projects_created", columnType="INTEGER"),
    Column(name="datasets_added",   columnType="INTEGER"),
    Column(name="studies_skipped",  columnType="INTEGER"),
    Column(name="errors",           columnType="INTEGER"),
]


def get_or_create_state_tables(
    syn: synapseclient.Synapse,
    state_project_id: str,
) -> dict[str, str]:
    """Return a dict with 'processed_studies' and 'run_log' Synapse table IDs.

    Creates the tables if they do not exist. Safe to call on every run.
    """
    existing = {
        child.get("name"): child.get("id")  # type: ignore[union-attr]
        for child in syn.getChildren(state_project_id, includeTypes=["table"])
    }

    processed_id = existing.get(PROCESSED_STUDIES_TABLE_NAME)
    if not processed_id:
        schema = Schema(
            name=PROCESSED_STUDIES_TABLE_NAME,
            columns=PROCESSED_STUDIES_COLUMNS,
            parent=state_project_id,
        )
        table = syn.store(schema)
        processed_id = table.id
        print(f"Created state table: {PROCESSED_STUDIES_TABLE_NAME} ({processed_id})")
    else:
        print(f"Found state table: {PROCESSED_STUDIES_TABLE_NAME} ({processed_id})")

    run_log_id = existing.get(RUN_LOG_TABLE_NAME)
    if not run_log_id:
        schema = Schema(
            name=RUN_LOG_TABLE_NAME,
            columns=RUN_LOG_COLUMNS,
            parent=state_project_id,
        )
        table = syn.store(schema)
        run_log_id = table.id
        print(f"Created state table: {RUN_LOG_TABLE_NAME} ({run_log_id})")
    else:
        print(f"Found state table: {RUN_LOG_TABLE_NAME} ({run_log_id})")

    return {
        "processed_studies": processed_id,
        "run_log": run_log_id,
    }
