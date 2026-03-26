"""NF Data Portal folder structure and annotation templates.

All Synapse projects created by the agent follow the standard NF folder
hierarchy and annotation schema defined here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Standard top-level folder names for every NF Data Portal project
NF_FOLDER_STRUCTURE: list[str] = [
    "Raw Data",
    "Analysis",
    "Source Metadata",
]

# Sub-folder created inside Raw Data for each discovered dataset
DATASET_SUBFOLDER = "Dataset"


@dataclass
class ProjectAnnotations:
    """Standard NF Portal annotations applied at the project level."""

    study: str                    # Portal study identifier
    resource_type: str = "experimentalData"
    resource_status: str = "pendingReview"
    funding_agency: str = "Not Applicable (External Study)"
    access_type: str = "open"
    external_accession_id: str = ""
    external_repository: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "study": self.study,
            "resourceType": self.resource_type,
            "resourceStatus": self.resource_status,
            "fundingAgency": self.funding_agency,
            "accessType": self.access_type,
            "externalAccessionID": self.external_accession_id,
            "externalRepository": self.external_repository,
        }


@dataclass
class FileAnnotations:
    """Standard NF Portal annotations applied to each pointer file entity."""

    study: str
    data_type: str = ""
    data_subtype: str = "raw"
    assay: str = ""
    species: str = ""
    tumor_type: str = ""
    diagnosis: str = ""
    file_format: str = ""
    resource_type: str = "experimentalData"
    content_type: str = "dataset"
    access_type: str = "open"
    external_accession_id: str = ""
    external_repository: str = ""
    resource_status: str = "pendingReview"

    def as_dict(self) -> dict[str, str]:
        return {k: v for k, v in {
            "study": self.study,
            "dataType": self.data_type,
            "dataSubtype": self.data_subtype,
            "assay": self.assay,
            "species": self.species,
            "tumorType": self.tumor_type,
            "diagnosis": self.diagnosis,
            "fileFormat": self.file_format,
            "resourceType": self.resource_type,
            "contentType": self.content_type,
            "accessType": self.access_type,
            "externalAccessionID": self.external_accession_id,
            "externalRepository": self.external_repository,
            "resourceStatus": self.resource_status,
        }.items() if v}


def build_project_name(repository: str, accession_id: str) -> str:
    """Return a standardized Synapse project name.

    Format: EXT_{Repository}_{AccessionID}
    """
    safe_accession = accession_id.replace("/", "_").replace(":", "_")
    return f"EXT_{repository}_{safe_accession}"


WIKI_TEMPLATE = """\
## Auto-Discovered External Dataset

**Source Repository:** {repository}
**Accession ID:** {accession_id}
**Data URL:** {data_url}

---

### Abstract

{abstract}

---

### Dataset Details

| Field | Value |
|-------|-------|
| Data Types | {data_types} |
| File Formats | {file_formats} |
| Sample Count | {sample_count} |
| Access Type | {access_type} |
| License | {license} |
| Discovery Date | {discovery_date} |

---

### NF Relevance Assessment

| Field | Value |
|-------|-------|
| Relevance Score | {relevance_score} |
| Disease Focus | {disease_focus} |
| Assay Types | {assay_types} |
| Species | {species} |
| Tissue Types | {tissue_types} |

---

> **Note:** This project was created automatically by the NF Data Contributor Agent
> and is **pending data manager review**. Please verify the metadata and approve or
> reject by updating `resourceStatus` accordingly.
>
> Model used for metadata extraction: `{model_version}`
"""
