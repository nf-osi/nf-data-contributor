"""Schema property fetching and enum validation.

Extracted from ``prompts/synapse_workflow.md`` and ``prompts/annotation_gap_fill.md``
so the agent (and tests) can import them directly instead of the LLM
re-authoring them every run.

Two public functions:

    fetch_schema_properties(schema_uri) -> dict
        Returns {field_name: {"type", "enum"?, "description"}} for every
        property defined by a registered Synapse JSON schema. The schema is
        the authoritative source of what file-annotation fields exist —
        never hardcode field names against it.

    validate_against_enum(raw_value, field_props) -> str | None
        Maps a free-form source value to a valid enum entry (exact case)
        or returns None if no reasonable match. The same call is used
        during both initial annotation and the audit gap-fill re-pass.

Both are intentionally side-effect-free and synchronous so they compose
cleanly inside per-file loops.
"""

from __future__ import annotations

from typing import Any

import httpx


SCHEMA_REGISTRY_URL = (
    "https://repo-prod.prod.sagebase.org/repo/v1/schema/type/registered/{uri}"
)


def fetch_schema_properties(schema_uri: str, *, timeout: float = 15.0) -> dict[str, dict]:
    """Return a dict of all property names defined in a registered Synapse schema.

    Keys are field names; values are ``{"type", "enum"?, "description"}``.

    Must traverse the ``properties`` layer rather than arbitrary keys, or
    unrelated sub-object enums (e.g. a clinical questionnaire block inside a
    behavioral template) will leak through and be incorrectly matched.
    """
    url = SCHEMA_REGISTRY_URL.format(uri=schema_uri)
    resp = httpx.get(url, timeout=timeout)
    resp.raise_for_status()
    return parse_schema_properties(resp.json())


def parse_schema_properties(schema: dict) -> dict[str, dict]:
    """Parse a JSON Schema dict and return the flat property map.

    Kept separate from the HTTP call so unit tests can feed in fixtures.
    """
    props: dict[str, dict] = {}

    def collect(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        for field_name, field_def in obj.get("properties", {}).items():
            if not isinstance(field_def, dict):
                continue
            entry: dict[str, Any] = {}
            if "enum" in field_def:
                entry["enum"] = field_def["enum"]
                entry["type"] = "enum"
            else:
                for sub_key in ("anyOf", "oneOf"):
                    for sub in field_def.get(sub_key, []):
                        if isinstance(sub, dict) and "enum" in sub:
                            entry["enum"] = sub["enum"]
                            entry["type"] = "enum"
                entry.setdefault("type", field_def.get("type", "string"))
            entry["description"] = field_def.get("description", "")
            props[field_name] = entry
        for defs_key in ("definitions", "$defs"):
            for defn in obj.get(defs_key, {}).values():
                collect(defn)

    collect(schema)
    return props


# Universal synonyms that apply across life-science portals regardless of
# schema. Schema-specific synonyms should be derived from each enum list at
# runtime — do not add portal-specific strings here.
_UNIVERSAL_SYNONYMS: dict[str, str] = {
    "homo sapiens": "Homo sapiens",
    "human": "Homo sapiens",
    "humans": "Homo sapiens",
    "mus musculus": "Mus musculus",
    "mouse": "Mus musculus",
    "mice": "Mus musculus",
    "rattus norvegicus": "Rattus norvegicus",
    "rat": "Rattus norvegicus",
    "female": "Female",
    "f": "Female",
    "male": "Male",
    "m": "Male",
    "unknown": "Unknown",
    "not reported": "Unknown",
    "not collected": "Unknown",
    "n/a": "Unknown",
    "na": "Unknown",
    "not applicable": "Unknown",
    "paired": "Paired",
    "paired-end": "Paired",
    "single": "Single",
    "single-end": "Single",
    "fresh frozen": "Fresh Frozen",
    "ffpe": "FFPE",
    "reverse stranded": "SecondStranded",
    "forward stranded": "FirstStranded",
    "unstranded": "Unstranded",
}


def validate_against_enum(raw_value: Any, field_props: dict) -> str | None:
    """Return a valid enum entry (exact case) for a raw source value, or None.

    Three-stage match:
        1. Case-insensitive exact match against the schema enum.
        2. Substring / prefix match for common abbreviations.
        3. Universal synonym table (human/mouse/female/male/etc.) — the
           candidate is ONLY accepted if it actually appears in the field's
           enum, so stale synonyms cannot introduce invalid values.

    For free-text (non-enum) fields, returns the stripped raw value unchanged
    when it is non-empty, else None.

    Callers should record a ``GapReport.add_approximation(...)`` entry when
    this function returns a mapped value that differs from the raw input, so
    reviewers can see what was mapped.
    """
    if raw_value is None:
        return None
    raw_str = str(raw_value).strip()
    if not raw_str:
        return None

    enum_list = field_props.get("enum")
    if not enum_list:
        # Free-text field — any non-empty string is valid
        return raw_str

    raw_norm = raw_str.lower()

    # 1. Exact (case-insensitive)
    for entry in enum_list:
        if str(entry).lower() == raw_norm:
            return str(entry)

    # 2. Substring / prefix — only accept when the substring is a substantial
    # fraction of the shorter string, to avoid spurious matches like "a" in
    # "analysis".
    for entry in enum_list:
        entry_norm = str(entry).lower()
        if raw_norm in entry_norm or entry_norm in raw_norm:
            shorter = min(len(raw_norm), len(entry_norm))
            longer = max(len(raw_norm), len(entry_norm))
            if shorter >= 3 and shorter / longer >= 0.5:
                return str(entry)

    # 3. Universal synonyms — candidate must appear in the enum or it is not
    # a valid mapping for this schema version.
    candidate = _UNIVERSAL_SYNONYMS.get(raw_norm)
    if candidate and candidate in enum_list:
        return candidate

    return None


def is_empty_enum(field_props: dict) -> bool:
    """True when the schema defines an enum but provides no valid values.

    Setting such a field always fails validation — skip it entirely.
    """
    return field_props.get("type") == "enum" and not field_props.get("enum")


def is_never_on_files() -> frozenset[str]:
    """Annotation keys that must never be set on File entities.

    Returning the set through a function keeps the single source of truth
    collocated with the schema helpers.
    """
    return frozenset({"resourceStatus", "filename"})
