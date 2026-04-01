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


def _make_processed_studies_columns() -> list[Column]:
    return [
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


def _make_run_log_columns() -> list[Column]:
    return [
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
    table_prefix: str = "NF_DataContributor",
) -> dict[str, str]:
    """Return a dict with 'processed_studies' and 'run_log' Synapse table IDs.

    Creates the tables if they do not exist. Safe to call on every run.

    Args:
        syn: Authenticated Synapse client.
        state_project_id: Synapse project ID where state tables live.
        table_prefix: Prefix for table names, e.g. "NF_DataContributor" produces
            "NF_DataContributor_ProcessedStudies" and "NF_DataContributor_RunLog".
            Read from agent.state_table_prefix in config/settings.yaml.
    """
    processed_table_name = f"{table_prefix}_ProcessedStudies"
    run_log_table_name = f"{table_prefix}_RunLog"

    existing = {
        child.get("name"): child.get("id")  # type: ignore[union-attr]
        for child in syn.getChildren(state_project_id, includeTypes=["table"])
    }

    processed_id = existing.get(processed_table_name)
    if not processed_id:
        schema = Schema(
            name=processed_table_name,
            columns=_make_processed_studies_columns(),
            parent=state_project_id,
        )
        table = syn.store(schema)
        processed_id = table.id
        print(f"Created state table: {processed_table_name} ({processed_id})")
    else:
        print(f"Found state table: {processed_table_name} ({processed_id})")

    run_log_id = existing.get(run_log_table_name)
    if not run_log_id:
        schema = Schema(
            name=run_log_table_name,
            columns=_make_run_log_columns(),
            parent=state_project_id,
        )
        table = syn.store(schema)
        run_log_id = table.id
        print(f"Created state table: {run_log_table_name} ({run_log_id})")
    else:
        print(f"Found state table: {run_log_table_name} ({run_log_id})")

    return {
        "processed_studies": processed_id,
        "run_log": run_log_id,
    }
