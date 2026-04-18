"""Tests for lib/gap_report.py — round-trip, stats, and rendering."""

from __future__ import annotations

import json

import pytest

from lib.gap_report import (
    EnumApproximation,
    FilledField,
    GapField,
    GapReport,
    SourceRef,
)


def _sample_report() -> GapReport:
    r = GapReport(project_id="syn12345678", schema_uri="org.example.rnaseq", pass_="initial")
    r.add_filled(
        "platform",
        "Illumina NovaSeq 6000",
        SourceRef(
            name="ENA filereport",
            tier=1,
            url="https://www.ebi.ac.uk/ena/portal/api/filereport?accession=PRJNA1",
            field_in_source="instrument_model",
        ),
    )
    r.add_filled(
        "species",
        "Homo sapiens",
        SourceRef(name="ENA filereport", tier=1, field_in_source="scientific_name"),
    )
    r.add_filled(
        "sex",
        "Female",
        SourceRef(
            name="PMC PMC0000001 methods",
            tier=3,
            url="https://www.ncbi.nlm.nih.gov/pmc/articles/PMC0000001/",
            notes="all patients female",
        ),
        entity_id="syn99999",
    )
    r.add_approximation(
        field_name="dissociationMethod",
        raw_value="enzymatic dissociation (Collagenase IV)",
        mapped_to="Enzymatic",
        available_enums=["Enzymatic", "Mechanical", "Unknown"],
        source=SourceRef(name="PMC PMC0000001 methods", tier=3),
    )
    r.add_gap(
        field_name="ageUnit",
        tiers_attempted=[1, 2, 3],
        sources_attempted=["ENA filereport", "GEO GSM", "PMC methods", "supp Table S1"],
        reason="ages given as ranges without numeric units",
    )
    r.add_note("Species verified from ENA scientific_name for all 3 runs")
    return r


def test_sourceref_rejects_invalid_tier():
    with pytest.raises(ValueError):
        SourceRef(name="x", tier=5)


def test_add_methods_append_entries():
    r = _sample_report()
    assert len(r.filled) == 3
    assert len(r.approximations) == 1
    assert len(r.gaps) == 1
    assert len(r.notes) == 1


def test_stats_counts_by_tier():
    r = _sample_report()
    s = r.stats
    assert s["tier1"] == 2
    assert s["tier3"] == 1
    assert s["tier2"] == 0
    assert s["tier4"] == 0
    assert s["filled_total"] == 3
    assert s["approximations"] == 1
    assert s["gaps"] == 1
    assert s["gaps_needing_human"] == 1


def test_json_round_trip_preserves_structure():
    r = _sample_report()
    j = r.to_json()
    # Must be valid JSON
    parsed = json.loads(j)
    assert parsed["project_id"] == "syn12345678"
    assert parsed["pass_"] == "initial"
    # Round-trip to GapReport
    r2 = GapReport.from_json(j)
    assert r2.project_id == r.project_id
    assert r2.schema_uri == r.schema_uri
    assert len(r2.filled) == len(r.filled)
    # Source refs preserved
    assert r2.filled[0].source.tier == 1
    assert r2.filled[0].source.url.startswith("https://www.ebi.ac.uk")
    assert r2.filled[2].entity_id == "syn99999"
    # Approximations preserved
    assert r2.approximations[0].mapped_to == "Enzymatic"
    # Gaps preserved
    assert r2.gaps[0].tiers_attempted == [1, 2, 3]
    assert r2.gaps[0].needs_human is True


def test_completeness_excludes_empty_enum_fields():
    r = _sample_report()
    schema = {
        "platform": {"type": "enum", "enum": ["Illumina NovaSeq 6000", "Other"]},
        "species": {"type": "enum", "enum": ["Homo sapiens", "Mus musculus"]},
        "sex": {"type": "enum", "enum": ["Female", "Male", "Unknown"]},
        # empty-enum fields must be excluded from the denominator
        "noValidValues": {"type": "enum", "enum": []},
        "dissociationMethod": {"type": "enum", "enum": ["Enzymatic", "Mechanical"]},
        "ageUnit": {"type": "enum", "enum": ["Weeks", "Months", "Years"]},
    }
    # 3 populated / 5 applicable
    assert r.completeness(schema) == pytest.approx(3 / 5)


def test_completeness_returns_none_without_schema():
    r = _sample_report()
    assert r.completeness(None) is None
    assert r.completeness({}) is None


def test_render_markdown_contains_key_sections():
    r = _sample_report()
    md = r.render_markdown(
        synapse_project_url="https://www.synapse.org/Synapse:syn12345678",
        schema_props={
            "platform": {"type": "enum", "enum": ["Illumina NovaSeq 6000"]},
            "species": {"type": "enum", "enum": ["Homo sapiens"]},
            "sex": {"type": "enum", "enum": ["Female"]},
            "dissociationMethod": {"type": "enum", "enum": ["Enzymatic"]},
            "ageUnit": {"type": "enum", "enum": ["Years"]},
        },
    )
    # Header
    assert "NADIA curation summary" in md
    assert "https://www.synapse.org/Synapse:syn12345678" in md
    assert "org.example.rnaseq" in md
    # Tier sections
    assert "Tier 1" in md
    assert "Tier 3" in md
    # Filled field rows
    assert "`platform`" in md
    assert "Illumina NovaSeq 6000" in md
    # Source URL rendered as link
    assert "ena/portal/api/filereport" in md
    assert "instrument_model" in md
    # Approximation section
    assert "Controlled vocabulary approximations" in md
    assert "`dissociationMethod`" in md
    assert "Enzymatic" in md
    # Gap section
    assert "Gaps requiring human review" in md
    assert "`ageUnit`" in md
    # Completeness
    assert "Completeness:" in md
    # JSON blob for tooling
    assert "```json" in md


def test_render_summary_line():
    r = _sample_report()
    line = r.render_summary_line()
    assert "T1=2" in line
    assert "T3=1" in line
    assert "approx=1" in line
    assert "gaps=1" in line


def test_fields_populated_is_unique_set():
    r = GapReport(project_id="syn1")
    r.add_filled("foo", "a", SourceRef(name="x", tier=1))
    r.add_filled("foo", "b", SourceRef(name="x", tier=2))  # same field re-filled
    r.add_filled("bar", "c", SourceRef(name="x", tier=1))
    assert r.fields_populated() == {"foo", "bar"}


def test_gap_needs_human_false_is_preserved_on_round_trip():
    r = GapReport(project_id="syn1")
    r.add_gap(
        "notApplicable",
        tiers_attempted=[1],
        sources_attempted=["schema inspection"],
        reason="field not applicable for this study type",
        needs_human=False,
    )
    r2 = GapReport.from_json(r.to_json())
    assert r2.gaps[0].needs_human is False


def test_render_markdown_handles_empty_report():
    r = GapReport(project_id="syn1")
    md = r.render_markdown()
    # Should still have a header and the JSON blob, even if no tables
    assert "NADIA curation summary" in md
    assert "```json" in md
    # No tier tables
    assert "Tier 1" not in md
    assert "Tier 4" not in md


# ---------------- weakest_fields (completeness gate input) ----------------


def test_weakest_fields_prioritizes_needs_human_gaps():
    r = GapReport(project_id="syn1")
    r.add_approximation(
        "f_approx", "raw", "Mapped", ["Mapped", "Other"],
        source=SourceRef(name="x", tier=3),
    )
    r.add_gap(
        "f_needs_human",
        tiers_attempted=[1, 2],
        sources_attempted=["ENA filereport", "PMC methods"],
        reason="not reported anywhere",
        needs_human=True,
    )
    out = r.weakest_fields(limit=3)
    assert out[0][0] == "f_needs_human"
    assert "tiers 1, 2 tried" in out[0][1]
    assert "not reported anywhere" in out[0][1]


def test_weakest_fields_orders_unmapped_before_mapped_approximations():
    r = GapReport(project_id="syn1")
    r.add_approximation(
        "f_mapped", "raw-close", "Close", ["Close"],
        source=SourceRef(name="x", tier=3),
    )
    r.add_approximation(
        "f_unmapped", "nothing-like-enum", None, ["A", "B"],
        source=SourceRef(name="x", tier=3),
    )
    out = r.weakest_fields(limit=3)
    assert [name for name, _ in out] == ["f_unmapped", "f_mapped"]
    assert "unmapped" in out[0][1]
    assert "`nothing-like-enum`" in out[0][1]
    assert "approximated" in out[1][1]


def test_weakest_fields_respects_limit():
    r = GapReport(project_id="syn1")
    for i in range(5):
        r.add_gap(
            f"f{i}",
            tiers_attempted=[1],
            sources_attempted=["ENA"],
            reason="missing",
            needs_human=True,
        )
    out = r.weakest_fields(limit=2)
    assert len(out) == 2
    assert out[0][0] == "f0"
    assert out[1][0] == "f1"


def test_weakest_fields_includes_non_human_gaps_last():
    r = GapReport(project_id="syn1")
    r.add_gap(
        "f_not_applicable",
        tiers_attempted=[1],
        sources_attempted=["schema inspection"],
        reason="field not applicable",
        needs_human=False,
    )
    out = r.weakest_fields(limit=3)
    assert out == [("f_not_applicable", "gap: field not applicable (tier 1 tried)")]


def test_weakest_fields_empty_report_returns_empty_list():
    r = GapReport(project_id="syn1")
    assert r.weakest_fields() == []


# ---------------- Completeness banner (scripts/post_curation_comment.py) ----------------


def _load_post_curation_comment():
    """Import scripts/post_curation_comment.py without it being a package."""
    import importlib.util
    from pathlib import Path

    mod_path = Path(__file__).resolve().parent.parent / "scripts" / "post_curation_comment.py"
    spec = importlib.util.spec_from_file_location("post_curation_comment", mod_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_completeness_banner_renders_weakest_fields():
    pcc = _load_post_curation_comment()
    r = _sample_report()
    banner = pcc.build_completeness_banner(
        r, completeness=0.42, threshold=0.60, label_applied=True
    )
    # Header line
    assert "Low completeness: 42%" in banner
    assert "below 60% threshold" in banner
    assert "`low-completeness` label applied" in banner
    # Lists the actual weak field from the sample report
    assert "`ageUnit`" in banner
    # Mentions the approximation seen in the sample report
    assert "`dissociationMethod`" in banner


def test_completeness_banner_notes_when_label_not_applied():
    pcc = _load_post_curation_comment()
    r = GapReport(project_id="syn1")
    banner = pcc.build_completeness_banner(
        r, completeness=0.30, threshold=0.60, label_applied=False
    )
    # No label mention when not applied
    assert "label applied" not in banner
    # Empty report → explicit fallback text
    assert "No specific gaps recorded" in banner


def test_build_comment_skips_banner_when_above_threshold():
    pcc = _load_post_curation_comment()
    r = _sample_report()
    schema = {
        "platform": {"type": "enum", "enum": ["Illumina NovaSeq 6000"]},
        "species": {"type": "enum", "enum": ["Homo sapiens"]},
        "sex": {"type": "enum", "enum": ["Female"]},
    }
    # 3/3 populated → completeness 1.0, way above 0.6
    body = pcc.build_comment(
        r,
        synapse_project_id="syn12345678",
        schema_props=schema,
        completeness_threshold=0.60,
        label_applied=False,
    )
    assert "Low completeness" not in body
    assert "NADIA curation summary" in body


def test_build_comment_prepends_banner_when_below_threshold():
    pcc = _load_post_curation_comment()
    r = _sample_report()
    schema = {
        "platform": {"type": "enum", "enum": ["Illumina NovaSeq 6000"]},
        "species": {"type": "enum", "enum": ["Homo sapiens"]},
        "sex": {"type": "enum", "enum": ["Female"]},
        "a": {"type": "string"}, "b": {"type": "string"},
        "c": {"type": "string"}, "d": {"type": "string"},
        "e": {"type": "string"}, "f": {"type": "string"},
        "g": {"type": "string"},
    }
    # 3/10 populated = 30%, below default 0.6 threshold
    body = pcc.build_comment(
        r,
        synapse_project_id="syn12345678",
        schema_props=schema,
        completeness_threshold=0.60,
        label_applied=True,
    )
    # Banner appears before the main summary
    assert body.index("Low completeness") < body.index("NADIA curation summary")
