"""Annotation helpers for Synapse entities.

Maps Claude extraction results and repository metadata onto the NF Data Portal
controlled-vocabulary annotation keys.
"""

from __future__ import annotations

import synapseclient
import structlog

from src.discovery.base import StudyCandidate
from src.relevance.scorer import RelevanceResult
from .templates import FileAnnotations, ProjectAnnotations

logger = structlog.get_logger(__name__)


# NF Portal controlled vocabulary mapping for assay types
_ASSAY_VOCAB_MAP: dict[str, str] = {
    "rnaseq": "rnaSeq",
    "rna-seq": "rnaSeq",
    "bulk rnaseq": "rnaSeq",
    "scrna-seq": "scrnaSeq",
    "scrna": "scrnaSeq",
    "single cell rna": "scrnaSeq",
    "chipseq": "ChIPSeq",
    "chip-seq": "ChIPSeq",
    "atacseq": "ATACSeq",
    "atac-seq": "ATACSeq",
    "wgs": "wholeGenomeSeq",
    "whole genome": "wholeGenomeSeq",
    "wes": "wholeExomeSeq",
    "whole exome": "wholeExomeSeq",
    "microarray": "geneExpressionArray",
    "methylation": "methylationArray",
    "bisulfite": "bisulfiteSeq",
    "lc-ms": "LC-MS",
    "mass spec": "LC-MS",
    "proteomics": "LC-MS",
    "metabolomics": "metabolomics",
    "mirna": "miRNASeq",
    "mirna-seq": "miRNASeq",
    "snp array": "SNPArray",
    "snp": "SNPArray",
    "immunoassay": "immunoassay",
    "immunofluorescence": "immunofluorescence",
    "western blot": "westernBlot",
    "flow cytometry": "flowCytometry",
}

_SPECIES_VOCAB_MAP: dict[str, str] = {
    "human": "Human",
    "homo sapiens": "Human",
    "mouse": "Mouse",
    "mus musculus": "Mouse",
    "rat": "Rat",
    "rattus norvegicus": "Rat",
    "zebrafish": "Zebrafish",
    "danio rerio": "Zebrafish",
    "drosophila": "Drosophila melanogaster",
}


def normalize_assay(raw: str) -> str:
    return _ASSAY_VOCAB_MAP.get(raw.lower(), raw)


def normalize_species(raw: str) -> str:
    return _SPECIES_VOCAB_MAP.get(raw.lower(), raw)


def build_project_annotations(
    candidate: StudyCandidate,
    study_name: str,
) -> ProjectAnnotations:
    return ProjectAnnotations(
        study=study_name,
        resource_type="experimentalData",
        resource_status="pendingReview",
        funding_agency="Not Applicable (External Study)",
        access_type=candidate.access_type,
        external_accession_id=candidate.accession_id,
        external_repository=candidate.source_repository,
    )


def build_file_annotations(
    candidate: StudyCandidate,
    result: RelevanceResult,
    study_name: str,
    file_format: str = "",
) -> FileAnnotations:
    assay = normalize_assay(result.assay_types[0]) if result.assay_types else ""
    species = normalize_species(result.species[0]) if result.species else ""
    tumor_type = result.tissue_types[0] if result.tissue_types else ""
    diagnosis = result.disease_focus[0] if result.disease_focus else ""

    return FileAnnotations(
        study=study_name,
        data_type="Genomic" if assay else "Other",
        data_subtype="raw",
        assay=assay,
        species=species,
        tumor_type=tumor_type,
        diagnosis=diagnosis,
        file_format=file_format,
        resource_type="experimentalData",
        content_type="dataset",
        access_type=candidate.access_type,
        external_accession_id=candidate.accession_id,
        external_repository=candidate.source_repository,
        resource_status="pendingReview",
    )


def apply_annotations(
    syn: synapseclient.Synapse,
    entity_id: str,
    annotations: dict[str, str],
) -> None:
    """Write annotation key-value pairs to a Synapse entity."""
    log = structlog.get_logger(__name__)
    try:
        entity = syn.get(entity_id, downloadFile=False)
        entity.annotations.update(annotations)
        syn.store(entity)
        log.info("annotations_applied", entity_id=entity_id)
    except Exception:
        log.exception("annotations_failed", entity_id=entity_id)
