"""Smoke tests for the lib/ helper modules."""

from __future__ import annotations

from unittest.mock import MagicMock, call

from lib.state_bootstrap import (
    PROCESSED_STUDIES_TABLE_NAME,
    RUN_LOG_TABLE_NAME,
    get_or_create_state_tables,
)


def _make_syn(existing_tables: dict[str, str]) -> MagicMock:
    """Build a mock Synapse client with the given existing table name→id mapping."""
    syn = MagicMock()
    children = [
        {"name": name, "id": tid} for name, tid in existing_tables.items()
    ]
    syn.getChildren.return_value = iter(children)

    def store_side_effect(schema):
        mock_table = MagicMock()
        mock_table.id = f"syn_new_{schema.name}"
        return mock_table

    syn.store.side_effect = store_side_effect
    return syn


def test_creates_both_tables_when_none_exist() -> None:
    syn = _make_syn({})
    result = get_or_create_state_tables(syn, "syn_state_project")

    assert result["processed_studies"].startswith("syn_new_")
    assert result["run_log"].startswith("syn_new_")
    assert syn.store.call_count == 2


def test_returns_existing_ids_when_tables_present() -> None:
    syn = _make_syn({
        PROCESSED_STUDIES_TABLE_NAME: "syn_existing_processed",
        RUN_LOG_TABLE_NAME: "syn_existing_log",
    })
    result = get_or_create_state_tables(syn, "syn_state_project")

    assert result["processed_studies"] == "syn_existing_processed"
    assert result["run_log"] == "syn_existing_log"
    syn.store.assert_not_called()


def test_creates_only_missing_table() -> None:
    syn = _make_syn({
        PROCESSED_STUDIES_TABLE_NAME: "syn_existing_processed",
    })
    result = get_or_create_state_tables(syn, "syn_state_project")

    assert result["processed_studies"] == "syn_existing_processed"
    assert result["run_log"].startswith("syn_new_")
    assert syn.store.call_count == 1
