# Annotation Quality Standards

These rules apply to every project, regardless of domain. They describe
*principles* — the specific field names they apply to vary by schema and must
be discovered at runtime via `fetch_schema_properties(schema_uri)` from
`lib/schema_properties.py`.

Read this file whenever you are populating or auditing annotations on any
Synapse entity (project, dataset, or file). CLAUDE.md references these
standards by number; the authoritative text lives here.

---

## 1 — Schema enums are ground truth. Fetch them first.

Before writing any annotation, call `fetch_schema_properties(schema_uri)` to
retrieve every field the schema defines, along with its enum constraints. Never
use hardcoded field names or assume enum values from memory. If a source value
is not in the enum, do not invent a mapping — use the closest valid enum value
and flag it for human review in the GitHub curation comment.

**If a field's enum list is empty (`"enum": []`), do not set that field at
all — an empty enum means no valid value exists for it in the current schema
version.** Setting a field with no valid enum values will always fail
validation.

**Config-provided vocabulary lists can lag the live portal.** For any
controlled-vocabulary annotation (disease manifestation, disease focus, data
type, etc.), verify current valid values by querying the live Synapse portal
table at runtime rather than relying solely on values from
`config/settings.yaml`. The portal table is authoritative; config values are a
convenience cache that may be stale.

## 2 — Instrument/technology fields: use exact values from the source repository

When a schema defines a field for the instrument, platform, or sequencing
technology used, that field must contain the exact model name from the source
repository — not a generic vendor or category name (e.g., "Illumina HiSeq 2500"
not "Illumina"). The source of truth is:
- ENA/SRA filereport: `instrument_model` column
- GEO SOFT: `!Series_instrument_model` or `!Sample_instrument_model`
- PRIDE or other proteomics repos: instrument field in project metadata

Identify which schema field captures this concept by calling
`fetch_schema_properties()` and looking for fields named platform, instrument,
technology, or similar.

## 3 — Investigator fields: use paper authors, not repository submitters

Repository submitter fields (ENA, ArrayExpress, PRIDE, etc.) reflect whoever
deposited the files — often a research engineer or postdoc — not the principal
investigator or corresponding author. When a schema has a field for study
investigators, study leads, or principal investigators, derive it from the
PubMed AuthorList (first + last/corresponding author), not the repository
submitter. If no PMID is available, check BioStudies for an explicit `principal
investigator` role. Only fall back to the repository submitter if no other
source exists, and flag it for human review.

**Name format:** always write author names as `Firstname [Middle] Lastname` —
never `Lastname, Firstname` or `Lastname,F`. PubMed XML returns `<LastName>`
and `<ForeName>` separately; combine as `f"{fore_name} {last_name}"`. GEO
contributor fields may use `Lastname SP` style — reformat these before storing.

## 4 — Organism/species fields: always read from source metadata, never infer

Any disease can appear across multiple species (human patient samples, mouse
models, zebrafish, Drosophila, cell lines, etc.). When a schema defines an
organism or taxon field, always read it from the repository's organism/taxon
attribute — not from the disease context, model name, or study description.
GEO `!Series_sample_taxid`, ENA `scientific_name`, and BioStudies `Organism`
are authoritative. If the repository lists multiple species (e.g., human
xenograft in mouse), include all distinct values.

## 5 — Sample-varying fields: populate per-file from sample-level metadata, not study-level

Many schema fields vary between samples within a single study — not just
identifier fields, but also biological and technical attributes like genotype,
experimental condition, sex, age, tissue, cell type, preparation method, and
any treatment or perturbation fields. Setting a single study-level value for
all files is wrong whenever the study contains multiple sample groups.

For every file:
1. Map the file back to its source sample/run accession (SRR → SRX → GSM, or
   BioSample ID from ENA filereport)
2. Fetch that sample's individual metadata record (GEO GSM characteristics,
   SRA BioSample attributes, ENA sample record)
3. Populate each schema field from that sample's specific values, not from the
   study-level summary

For identifier fields (specimen ID, sample ID, individual ID, biobank ID, or
similar): the value must be unique per file — not a single shared value copied
to all files. Parse from the SRA run table / GEO GSM list, or from structured
repository metadata.

**Run accessions (SRR, ERR, DRR) identify sequencing runs, not biological
individuals or specimens.** Do not use run accessions as the value for
individual ID or specimen ID fields. Use the biological identifier from the
run's metadata instead — typically `sample_title`, `sample_alias`, or BioSample
accession (SAMN/SAME/SAMD) from the ENA filereport, or the GSM identifier from
GEO. For studies with a clear patient/sample nomenclature (e.g., patient IDs
from supplementary tables), use those.

**Signal that this rule was violated:** all files in the project have the same
value for a field that represents a biological property of a sample (genotype,
condition, sex, etc.) despite the study having multiple experimental groups.

## 6 — Assay subtype fields: verify from source metadata, not title

Publication titles often describe the biology, not the technology. When a
schema has an assay-type field with fine-grained values (e.g., distinguishing
single-cell from bulk RNA-seq, or ChIP-seq target type), the source of truth
is the repository's library metadata — not the paper title. Check ENA
`library_source`/`library_strategy`, GEO sample characteristics, or repository
experiment descriptions before setting these fields.

## 7 — Verify the paper actually generated the data

NCBI elink and Europe PMC annotations can return accessions from different
papers than the one being processed. Before using a linked accession, verify
ownership:
- GEO: `Entrez.esummary(db='gds', id=...)` → check `PubMedIds` matches the PMID
- SRA/ENA: filereport `study_title` should match the paper
- For repository-direct candidates: check the repository record's abstract
  matches the paper being processed

If the data belongs to a different paper, discard it and process it separately
under that paper's PMID.

## 8 — Cross-repository linking: add all related accessions

A single study often deposits data in multiple repositories (GEO + SRA +
BioProject, or PRIDE + MassIVE). Always populate `alternateDataRepository`
with all related accessions, not just the one you discovered it through. For
GEO series, check `!Series_relation` for linked SRA/BioProject accessions. For
ENA studies, check the study record for linked accessions.

## 9 — Controlled vocabulary gaps: flag, don't silently drop

When a concept from the study is not in the schema enum (e.g., a tumor type or
species not yet in the controlled vocabulary), use the closest available enum
value AND explicitly document the gap in the GitHub curation comment. This
ensures human reviewers know what was approximated and can request a
vocabulary update if warranted. Do not silently omit required fields — a
best-effort value with a flag is better than a missing field.

## 10 — Post-curation GitHub comment is required

After completing annotations for each project, post a GitHub comment on the
study-review issue. Use `scripts/post_curation_comment.py`, which renders a
`GapReport` JSON (produced by `lib/gap_report.py` during the initial pass in
Step C and the audit pass in Step 7b) into a structured markdown comment. The
rendered comment groups filled fields by tier (each row carries the source
name and a verification URL), lists controlled-vocabulary approximations with
the raw → mapped value, and surfaces remaining gaps with the tiers and
sources already attempted.

The comment must cover:
- Which fields were set and what values were chosen
- Which values were derived by reasoning vs. directly from source (the tier
  grouping makes this explicit)
- Any controlled vocabulary gaps or approximations made
- Any fields that could not be populated and why
- Items that require human review (ambiguous data, missing info, species
  mismatch, etc.)

Do not hand-format this comment — always go through the `GapReport` →
`post_curation_comment.py` path so every field carries a machine-readable
source trail.

This comment is the handoff from autonomous annotation to human review.
Without it, data managers cannot evaluate the quality of the curation or
identify what needs correction.

## 11 — Schema completeness check: exhaust all upstream sources before declaring a field unresolvable

After annotating files, run the full gap-fill algorithm from
`prompts/annotation_gap_fill.md` before considering annotation done. The
algorithm works through four tiers of sources in priority order — stopping at
the first tier that yields a valid value for each missing field:

- **Tier 1** — Structured repository metadata (ENA filereport with all
  columns, BioSample XML, ENA sample XML, GEO GSM characteristics, SRA RunInfo)
- **Tier 2** — Publication metadata (PMC full text methods section,
  supplementary tables downloaded and parsed, CrossRef funder info)
- **Tier 3** — Text extraction via reasoning (abstract, methods section,
  supplementary table rows — extract unambiguous values only)
- **Tier 4** — Data file inspection (h5ad/loom obs columns, BAM @RG tags,
  FASTQ headers, count matrix column headers)

Only after working through all four tiers should a field be documented as
unresolvable. For fields that genuinely cannot be determined from any source,
explicitly document them in the GitHub curation comment with the reason.

This check must happen before the Dataset entity `items` are finalized. It
also catches Standard 5 violations: if any field that should vary per sample
(genotype, condition, sex, age, tissue, cell type) has the same value on all
files in a multi-sample study, re-derive per-file values from per-sample
metadata.

## 12 — Schema template must match the actual data modality of the files

Before binding a metadata schema to a files folder, verify the assay type of
the files from repository library metadata (ENA `library_strategy`, GEO
`!Series_library_strategy`, repository experiment descriptions) — not from the
paper title or disease context. Bind the template that matches the primary
data modality of the files in that specific dataset.

Applying the wrong template (e.g., an RNA-seq schema to chromatin
accessibility data, or an epigenomics schema to transcriptomics data) causes
the wrong validation rules to apply and may result in required fields being
missed or inapplicable fields being populated. Each dataset in a multi-assay
project may require a different template.

**For model system studies** (cell lines, animal models, organoids): call
`fetch_schema_properties(schema_uri)` and populate every field that captures
the model system identity (typically: system/strain name, species, sex, age,
age unit, and any study-specific genotype or condition fields). These fields
vary per sample for experiments with multiple cell lines or genetic
backgrounds and must be populated per-file, not at study level.

## 13 — Disease annotation scoping: germline vs. somatic

When setting disease-focus or diagnosis annotations, distinguish between
germline disease (the patient or organism genetically carries the disease)
and somatic mutation in a disease-naive background:

- **Germline disease** — patient has the disease by diagnosis, or the model
  organism carries a germline mutation (e.g., `Nf1+/-` mouse model, NF1
  patient biopsy, NF2 patient tumor). → Use the disease annotation (e.g.,
  NF1, NF2, schwannomatosis).
- **Somatic mutation only** — cancer cell line with an acquired somatic
  NF1/NF2 loss, TCGA tumor that happens to carry an NF1 mutation, or general
  cancer cohort where NF1 loss is one of many driver events. → Use the
  appropriate cancer type annotation; **do not** add the germline disease
  annotation.

**Determining which applies:**
1. Check whether the study explicitly recruits NF patients or uses NF patient
   specimens.
2. Check model organism genotype: `Nf1+/-` (heterozygous germline) = germline
   model; `NF1 siRNA knockdown in MCF-7` = somatic model.
3. When uncertain, use the specific tumor/cancer type annotation and flag for
   human review.

This distinction matters because portal data consumers use disease annotations
to find data relevant to patients with inherited conditions — mixing in
somatic cancer data produces misleading search results.
