"""Tests for lib/schema_properties.py — parsing + enum validation."""

from __future__ import annotations

from lib.schema_properties import (
    is_empty_enum,
    is_never_on_files,
    parse_schema_properties,
    validate_against_enum,
)


# ---------------- parse_schema_properties ----------------


def test_parse_simple_enum_property():
    schema = {
        "properties": {
            "sex": {"enum": ["Female", "Male", "Unknown"], "description": "biological sex"},
        }
    }
    props = parse_schema_properties(schema)
    assert props["sex"]["type"] == "enum"
    assert props["sex"]["enum"] == ["Female", "Male", "Unknown"]
    assert props["sex"]["description"] == "biological sex"


def test_parse_nested_defs_with_anyof_enum():
    schema = {
        "properties": {
            "platform": {
                "anyOf": [
                    {"enum": ["Illumina NovaSeq 6000", "Illumina HiSeq 2500"]},
                    {"type": "null"},
                ]
            }
        }
    }
    props = parse_schema_properties(schema)
    assert props["platform"]["type"] == "enum"
    assert "Illumina NovaSeq 6000" in props["platform"]["enum"]


def test_parse_recurses_into_defs():
    schema = {
        "properties": {
            "top": {"type": "string"},
        },
        "$defs": {
            "inner": {
                "properties": {
                    "nested_field": {"enum": ["a", "b"]},
                }
            }
        },
    }
    props = parse_schema_properties(schema)
    assert "top" in props
    assert "nested_field" in props
    assert props["nested_field"]["enum"] == ["a", "b"]


def test_parse_non_dict_definitions_are_safe():
    # Degenerate schema where a defs value is not a dict — must not crash
    schema = {
        "properties": {"x": {"type": "string"}},
        "definitions": {"garbage": "not a dict"},
    }
    props = parse_schema_properties(schema)
    assert props == {"x": {"type": "string", "description": ""}}


def test_parse_preserves_type_for_non_enum_fields():
    schema = {
        "properties": {
            "count": {"type": "integer", "description": "number of samples"},
            "name": {"type": "string"},
        }
    }
    props = parse_schema_properties(schema)
    assert props["count"]["type"] == "integer"
    assert props["name"]["type"] == "string"


# ---------------- validate_against_enum ----------------


SEX_FIELD = {"type": "enum", "enum": ["Female", "Male", "Unknown"]}
SPECIES_FIELD = {"type": "enum", "enum": ["Homo sapiens", "Mus musculus"]}
FREE_TEXT_FIELD = {"type": "string"}
PLATFORM_FIELD = {"type": "enum", "enum": ["Illumina NovaSeq 6000", "Illumina HiSeq 2500"]}


def test_validate_exact_match_preserves_enum_case():
    assert validate_against_enum("Female", SEX_FIELD) == "Female"
    # Case-insensitive match returns canonical enum casing
    assert validate_against_enum("female", SEX_FIELD) == "Female"
    assert validate_against_enum("FEMALE", SEX_FIELD) == "Female"


def test_validate_free_text_returns_stripped_raw():
    assert validate_against_enum("  anything goes  ", FREE_TEXT_FIELD) == "anything goes"


def test_validate_empty_inputs_return_none():
    assert validate_against_enum("", SEX_FIELD) is None
    assert validate_against_enum("   ", SEX_FIELD) is None
    assert validate_against_enum(None, SEX_FIELD) is None


def test_validate_substring_requires_substantial_overlap():
    # "Illumina NovaSeq 6000" is a substring of a longer description — accepted
    assert (
        validate_against_enum("Illumina NovaSeq 6000 (S4)", PLATFORM_FIELD)
        == "Illumina NovaSeq 6000"
    )
    # A tiny substring should NOT match (avoids spurious hits like "a" in "Male")
    assert validate_against_enum("a", SEX_FIELD) is None


def test_validate_universal_synonyms_only_if_in_enum():
    # "human" -> "Homo sapiens" when the candidate is in the enum
    assert validate_against_enum("human", SPECIES_FIELD) == "Homo sapiens"
    # If the enum doesn't contain the candidate, synonym mapping must NOT leak
    limited_species = {"type": "enum", "enum": ["Mus musculus"]}
    assert validate_against_enum("human", limited_species) is None


def test_validate_synonym_variants():
    assert validate_against_enum("F", SEX_FIELD) == "Female"
    assert validate_against_enum("m", SEX_FIELD) == "Male"
    assert validate_against_enum("N/A", SEX_FIELD) == "Unknown"
    assert validate_against_enum("not reported", SEX_FIELD) == "Unknown"


def test_validate_returns_none_for_no_match():
    assert validate_against_enum("XYZ_unknown_value", SEX_FIELD) is None
    assert validate_against_enum("Pacbio Revio", PLATFORM_FIELD) is None


# ---------------- is_empty_enum / is_never_on_files ----------------


def test_is_empty_enum():
    assert is_empty_enum({"type": "enum", "enum": []}) is True
    assert is_empty_enum({"type": "enum", "enum": ["a"]}) is False
    assert is_empty_enum({"type": "string"}) is False


def test_is_never_on_files_contains_forbidden_keys():
    forbidden = is_never_on_files()
    assert "resourceStatus" in forbidden
    assert "filename" in forbidden
    # It should be an immutable frozenset so callers can't mutate it
    assert isinstance(forbidden, frozenset)
