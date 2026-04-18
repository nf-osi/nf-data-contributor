"""Gap report — structured record of annotation provenance.

Captures, per schema field, whether it was populated and from what source, or
whether it remains a gap and what sources were tried. Replaces the loose
`filled_tierN: ["field = value (source)"]` string format that humans couldn't
verify without re-opening the paper.

Every filled field carries a SourceRef so the GitHub curation comment can
render a row with a clickable verification link. Gaps carry the list of tiers
and sources that were actually attempted so a reviewer can see that work was
done, not just absence.

Data model:
    GapReport
      ├── filled:         list[FilledField]       — populated fields + provenance
      ├── approximations: list[EnumApproximation] — raw value mapped to enum
      └── gaps:           list[GapField]          — unresolved after exhaust

Serialize with ``to_json()`` (stable on-disk format) and render the curation
comment with ``render_markdown()``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


TIER_NAMES = {
    1: "structured repository metadata",
    2: "publication metadata",
    3: "text extraction (reasoning)",
    4: "data file inspection",
}


@dataclass
class SourceRef:
    """Where a value came from. Just enough for a reviewer to verify."""

    name: str                        # "ENA filereport", "PMC PMC12345 methods", etc.
    tier: int                        # 1..4 — see TIER_NAMES
    url: str | None = None           # clickable link to the source
    field_in_source: str | None = None  # e.g. "instrument_model" (ENA column name)
    notes: str | None = None         # free-form, e.g. "mapped via synonym"

    def __post_init__(self) -> None:
        if self.tier not in TIER_NAMES:
            raise ValueError(f"SourceRef.tier must be 1-4, got {self.tier}")


@dataclass
class FilledField:
    field: str
    value: Any
    source: SourceRef
    entity_id: str | None = None     # file/dataset/project entity; None = study-level


@dataclass
class EnumApproximation:
    """Raw source value didn't match the enum exactly — record what was mapped to what."""

    field: str
    raw_value: str
    mapped_to: str | None            # None if no close match and field stayed unset
    available_enums: list[str] = field(default_factory=list)
    source: SourceRef | None = None
    entity_id: str | None = None


@dataclass
class GapField:
    """Field that could not be populated after exhausting sources."""

    field: str
    tiers_attempted: list[int] = field(default_factory=list)
    sources_attempted: list[str] = field(default_factory=list)
    reason: str = ""
    needs_human: bool = True
    entity_id: str | None = None


@dataclass
class GapReport:
    project_id: str
    schema_uri: str | None = None
    pass_: str = "initial"           # "initial" (Step C) or "audit" (Step 7b)
    filled: list[FilledField] = field(default_factory=list)
    approximations: list[EnumApproximation] = field(default_factory=list)
    gaps: list[GapField] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ---------------- Mutators ----------------

    def add_filled(
        self,
        field_name: str,
        value: Any,
        source: SourceRef,
        entity_id: str | None = None,
    ) -> None:
        self.filled.append(FilledField(field_name, value, source, entity_id))

    def add_approximation(
        self,
        field_name: str,
        raw_value: str,
        mapped_to: str | None,
        available_enums: list[str],
        source: SourceRef | None = None,
        entity_id: str | None = None,
    ) -> None:
        self.approximations.append(
            EnumApproximation(
                field=field_name,
                raw_value=raw_value,
                mapped_to=mapped_to,
                available_enums=list(available_enums)[:10],
                source=source,
                entity_id=entity_id,
            )
        )

    def add_gap(
        self,
        field_name: str,
        *,
        tiers_attempted: list[int],
        sources_attempted: list[str],
        reason: str,
        needs_human: bool = True,
        entity_id: str | None = None,
    ) -> None:
        self.gaps.append(
            GapField(
                field=field_name,
                tiers_attempted=list(tiers_attempted),
                sources_attempted=list(sources_attempted),
                reason=reason,
                needs_human=needs_human,
                entity_id=entity_id,
            )
        )

    def add_note(self, note: str) -> None:
        self.notes.append(note)

    # ---------------- Query ----------------

    @property
    def stats(self) -> dict[str, int]:
        by_tier = {f"tier{i}": 0 for i in TIER_NAMES}
        for f in self.filled:
            by_tier[f"tier{f.source.tier}"] += 1
        return {
            **by_tier,
            "filled_total": len(self.filled),
            "approximations": len(self.approximations),
            "gaps": len(self.gaps),
            "gaps_needing_human": sum(1 for g in self.gaps if g.needs_human),
        }

    def fields_populated(self) -> set[str]:
        return {f.field for f in self.filled}

    def completeness(self, schema_props: dict[str, dict] | None) -> float | None:
        """Return the fraction of applicable schema fields that were populated.

        Excludes fields with empty enum (no valid value exists in the schema).
        Returns None when schema_props is unavailable.
        """
        if not schema_props:
            return None
        applicable = {
            name
            for name, props in schema_props.items()
            if not (props.get("type") == "enum" and not props.get("enum"))
        }
        if not applicable:
            return None
        populated = self.fields_populated() & applicable
        return len(populated) / len(applicable)

    def weakest_fields(self, limit: int = 3) -> list[tuple[str, str]]:
        """Return (field_name, human-readable reason) for the fields most needing review.

        Ordering priority:
          1. Unresolved gaps that need human follow-up
          2. Approximations where the raw value could not be mapped into the enum
          3. Approximations that were mapped to a close-but-inexact enum value
          4. Remaining gaps (needs_human=False)

        Used by the completeness gate to direct reviewer attention at the top
        of the curation comment instead of burying concerns in long tables.
        """
        result: list[tuple[str, str]] = []

        def _tier_suffix(tiers: list[int]) -> str:
            if not tiers:
                return ""
            label = ", ".join(str(t) for t in tiers)
            return f" (tier{'s' if len(tiers) > 1 else ''} {label} tried)"

        for g in self.gaps:
            if not g.needs_human:
                continue
            reason = g.reason or "no reachable source"
            result.append((g.field, f"gap: {reason}{_tier_suffix(g.tiers_attempted)}"))
            if len(result) >= limit:
                return result

        for a in self.approximations:
            if a.mapped_to is None:
                result.append((a.field, f"unmapped: `{a.raw_value}` not in enum"))
                if len(result) >= limit:
                    return result

        for a in self.approximations:
            if a.mapped_to is not None:
                result.append(
                    (a.field, f"approximated: `{a.raw_value}` → `{a.mapped_to}`")
                )
                if len(result) >= limit:
                    return result

        for g in self.gaps:
            if g.needs_human:
                continue
            reason = g.reason or "not applicable"
            result.append((g.field, f"gap: {reason}{_tier_suffix(g.tiers_attempted)}"))
            if len(result) >= limit:
                return result

        return result

    # ---------------- Serialization ----------------

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=_json_default, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "GapReport":
        def _src(s: dict | None) -> SourceRef | None:
            return SourceRef(**s) if s else None

        return cls(
            project_id=d["project_id"],
            schema_uri=d.get("schema_uri"),
            pass_=d.get("pass_", "initial"),
            filled=[
                FilledField(
                    field=f["field"],
                    value=f.get("value"),
                    source=SourceRef(**f["source"]),
                    entity_id=f.get("entity_id"),
                )
                for f in d.get("filled", [])
            ],
            approximations=[
                EnumApproximation(
                    field=a["field"],
                    raw_value=a["raw_value"],
                    mapped_to=a.get("mapped_to"),
                    available_enums=a.get("available_enums", []),
                    source=_src(a.get("source")),
                    entity_id=a.get("entity_id"),
                )
                for a in d.get("approximations", [])
            ],
            gaps=[
                GapField(
                    field=g["field"],
                    tiers_attempted=g.get("tiers_attempted", []),
                    sources_attempted=g.get("sources_attempted", []),
                    reason=g.get("reason", ""),
                    needs_human=g.get("needs_human", True),
                    entity_id=g.get("entity_id"),
                )
                for g in d.get("gaps", [])
            ],
            notes=list(d.get("notes", [])),
        )

    @classmethod
    def from_json(cls, s: str) -> "GapReport":
        return cls.from_dict(json.loads(s))

    # ---------------- Rendering ----------------

    def render_summary_line(self) -> str:
        s = self.stats
        return (
            f"T1={s['tier1']} T2={s['tier2']} T3={s['tier3']} T4={s['tier4']} "
            f"approx={s['approximations']} gaps={s['gaps']}"
        )

    def render_markdown(
        self,
        *,
        synapse_project_url: str | None = None,
        schema_props: dict[str, dict] | None = None,
        max_items_per_section: int = 50,
    ) -> str:
        """Render the gap report as a GitHub comment body.

        Sections:
          1. Summary line + completeness %
          2. Populated fields grouped by tier, each with source + URL
          3. Enum approximations (controlled vocabulary mappings)
          4. Gaps that require human review
          5. Notes
          6. Hidden JSON blob for downstream tooling
        """
        lines: list[str] = []
        lines.append("## NADIA curation summary")
        lines.append("")
        if synapse_project_url:
            lines.append(f"**Project:** {synapse_project_url}")
        if self.schema_uri:
            lines.append(f"**Schema:** `{self.schema_uri}`")
        lines.append(f"**Pass:** {self.pass_}")

        completeness = self.completeness(schema_props)
        stats = self.stats
        if completeness is not None:
            lines.append(
                f"**Completeness:** {completeness:.0%} "
                f"({stats['filled_total']} populated, {stats['gaps']} gaps, "
                f"{stats['approximations']} enum approximations)"
            )
        else:
            lines.append(
                f"**Populated:** {stats['filled_total']} fields "
                f"({self.render_summary_line()})"
            )
        lines.append("")

        # Filled fields — one section per tier that has entries
        for tier in sorted(TIER_NAMES):
            entries = [f for f in self.filled if f.source.tier == tier]
            if not entries:
                continue
            lines.append(f"### Filled from Tier {tier} — {TIER_NAMES[tier]}")
            lines.append("")
            lines.append("| Field | Value | Source |")
            lines.append("|---|---|---|")
            for f in entries[:max_items_per_section]:
                value_str = _fmt_value(f.value)
                src = _fmt_source(f.source)
                scope = f" · _file {f.entity_id}_" if f.entity_id else ""
                lines.append(f"| `{f.field}` | {value_str} | {src}{scope} |")
            if len(entries) > max_items_per_section:
                lines.append(
                    f"| _…{len(entries) - max_items_per_section} more — see attached JSON_ | | |"
                )
            lines.append("")

        # Enum approximations
        if self.approximations:
            lines.append("### Controlled vocabulary approximations")
            lines.append("")
            lines.append(
                "These raw values did not match the schema enum exactly. "
                "Review to confirm the mapping is acceptable or flag a vocabulary gap."
            )
            lines.append("")
            lines.append("| Field | Raw value | Mapped to | Enum options | Source |")
            lines.append("|---|---|---|---|---|")
            for a in self.approximations[:max_items_per_section]:
                enum_preview = ", ".join(f"`{e}`" for e in a.available_enums[:4])
                if len(a.available_enums) > 4:
                    enum_preview += f", …({len(a.available_enums)} total)"
                mapped = f"`{a.mapped_to}`" if a.mapped_to else "_(unset)_"
                src = _fmt_source(a.source) if a.source else "—"
                lines.append(
                    f"| `{a.field}` | `{a.raw_value}` | {mapped} | {enum_preview} | {src} |"
                )
            lines.append("")

        # Gaps — most important section for humans, put it prominently
        if self.gaps:
            lines.append("### Gaps requiring human review")
            lines.append("")
            lines.append("| Field | Reason | Tiers tried | Sources tried |")
            lines.append("|---|---|---|---|")
            for g in self.gaps[:max_items_per_section]:
                tiers = ", ".join(str(t) for t in g.tiers_attempted) or "—"
                sources = ", ".join(g.sources_attempted) or "—"
                scope = f" _(file {g.entity_id})_" if g.entity_id else ""
                lines.append(f"| `{g.field}`{scope} | {g.reason or '—'} | {tiers} | {sources} |")
            lines.append("")

        # Free-form notes
        if self.notes:
            lines.append("### Notes")
            lines.append("")
            for note in self.notes:
                lines.append(f"- {note}")
            lines.append("")

        # Machine-readable blob at the end
        lines.append("<details>")
        lines.append("<summary>NADIA gap report JSON (for tooling)</summary>")
        lines.append("")
        lines.append("```json")
        lines.append(self.to_json())
        lines.append("```")
        lines.append("")
        lines.append("</details>")

        return "\n".join(lines)


# ---------------- Helpers ----------------


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError(f"not JSON-serializable: {type(obj).__name__}")


def _fmt_value(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, list):
        if not v:
            return "`[]`"
        return ", ".join(f"`{x}`" for x in v[:5]) + (f" _+{len(v) - 5} more_" if len(v) > 5 else "")
    s = str(v)
    if len(s) > 120:
        s = s[:117] + "…"
    return f"`{s}`"


def _fmt_source(src: SourceRef) -> str:
    label = src.name
    if src.field_in_source:
        label += f" · `{src.field_in_source}`"
    if src.url:
        label = f"[{label}]({src.url})"
    if src.notes:
        label += f" _({src.notes})_"
    return label
