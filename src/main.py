"""NF Data Contributor Agent — main orchestrator.

Executed daily by the GitHub Actions workflow. Runs each module in sequence:
  1. Discovery  — query all configured repositories
  2. Deduplication — filter out already-known studies
  3. Relevance scoring — use Claude API to assess NF/SWN relevance
  4. Synapse project creation — provision pointer projects
  5. State tracking — log all results
  6. Notifications — create JIRA tickets for new studies
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import timedelta, date
from pathlib import Path

import structlog
import synapseclient
import yaml

from src.discovery.pubmed_geo import PubMedGeoConnector
from src.discovery.sra_dbgap import SraDbGapConnector
from src.discovery.zenodo import ZenodoConnector
from src.discovery.figshare import FigshareConnector
from src.discovery.osf import OsfConnector
from src.discovery.arrayexpress import ArrayExpressConnector
from src.discovery.ega import EgaConnector
from src.discovery.pride import PrideConnector
from src.discovery.metabolights import MetaboLightsConnector
from src.discovery.pdc import PdcConnector
from src.discovery.base import StudyCandidate
from src.relevance.scorer import RelevanceScorer
from src.deduplication.checker import DuplicateChecker
from src.synapse.project_creator import SynapseProjectCreator
from src.state.tracker import StateTracker, RunSummary, StudyStatus
from src.notifications.jira import JiraNotifier

# ---------------------------------------------------------------------------
# Structured logging setup
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

log = structlog.get_logger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
_KEYWORDS_PATH = Path(__file__).parent.parent / "config" / "nf_keywords.yaml"


def load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_keywords() -> list[str]:
    with open(_KEYWORDS_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("search_terms", [])


def build_connectors(
    cfg: dict,
    keywords: list[str],
    lookback_days: int,
    ncbi_email: str,
) -> list:
    max_results = cfg["discovery"]["max_results_per_connector"]
    common = dict(
        search_terms=keywords,
        max_results=max_results,
        lookback_days=lookback_days,
    )
    conn_cfg = cfg.get("connectors", {})
    connectors = []

    if conn_cfg.get("pubmed_geo", {}).get("enabled", True):
        connectors.append(PubMedGeoConnector(**common, ncbi_email=ncbi_email))
    if conn_cfg.get("sra", {}).get("enabled", True):
        connectors.append(SraDbGapConnector(**common, ncbi_email=ncbi_email))
    if conn_cfg.get("zenodo", {}).get("enabled", True):
        connectors.append(ZenodoConnector(**common))
    if conn_cfg.get("figshare", {}).get("enabled", True):
        connectors.append(FigshareConnector(**common))
    if conn_cfg.get("osf", {}).get("enabled", True):
        connectors.append(OsfConnector(**common))
    if conn_cfg.get("arrayexpress", {}).get("enabled", True):
        connectors.append(ArrayExpressConnector(**common))
    if conn_cfg.get("ega", {}).get("enabled", True):
        connectors.append(EgaConnector(**common))
    if conn_cfg.get("pride", {}).get("enabled", True):
        connectors.append(PrideConnector(**common))
    if conn_cfg.get("metabolights", {}).get("enabled", True):
        connectors.append(MetaboLightsConnector(**common))
    if conn_cfg.get("pdc", {}).get("enabled", True):
        connectors.append(PdcConnector(**common))

    return connectors


def run() -> None:
    cfg = load_config()
    keywords = load_keywords()

    # ---- Synapse login ----
    syn = synapseclient.Synapse()
    syn.login(authToken=os.environ["SYNAPSE_AUTH_TOKEN"], silent=True)
    log.info("synapse_login_ok")

    # ---- Determine lookback window ----
    state_project_id = cfg["agent"].get("state_project_id")
    if not state_project_id:
        # First run: provision the state project
        import synapseclient
        state_project = syn.store(
            synapseclient.Project(name="NF Data Contributor Agent — State")
        )
        state_project_id = state_project.id
        log.info("state_project_created", id=state_project_id)

    tracker = StateTracker(syn, state_project_id=state_project_id)

    lookback_days = cfg["discovery"]["initial_lookback_days"]  # simplified; tracker can refine

    # ---- Dedup checker ----
    dedup_cfg = cfg["deduplication"]
    checker = DuplicateChecker(
        syn=syn,
        studies_table_id=dedup_cfg["studies_table_id"],
        files_table_id=dedup_cfg["files_table_id"],
        datasets_table_id=dedup_cfg["datasets_table_id"],
        tracking_table_id=tracker.processed_table_id,
        fuzzy_threshold=dedup_cfg["fuzzy_title_threshold"],
    )

    # ---- Relevance scorer ----
    rel_cfg = cfg["relevance"]
    scorer = RelevanceScorer(
        model=rel_cfg["model"],
        threshold=rel_cfg["threshold"],
        require_primary_data=rel_cfg["require_primary_data"],
        min_sample_count=rel_cfg.get("min_sample_count"),
    )

    # ---- Synapse project creator ----
    syn_cfg = cfg["synapse"]
    creator = SynapseProjectCreator(
        syn=syn,
        parent_id=syn_cfg.get("pending_review_parent_id"),
        model_version=rel_cfg["model"],
    )

    # ---- JIRA notifier ----
    notifier = JiraNotifier(
        project_key=cfg["notifications"]["jira"]["project_key"],
        issue_type=cfg["notifications"]["jira"]["issue_type"],
        assignee_email=cfg["notifications"]["jira"]["assignee_email"],
    )

    # ---- Discovery ----
    ncbi_email = cfg.get("connectors", {}).get("pubmed_geo", {}).get(
        "email", "nf-data-contributor@sagebionetworks.org"
    )
    connectors = build_connectors(cfg, keywords, lookback_days, ncbi_email)

    all_candidates: list[StudyCandidate] = []
    for connector in connectors:
        candidates = connector.run()
        all_candidates.extend(candidates)
        log.info(
            "connector_results",
            repo=connector.repository_name,
            n=len(candidates),
        )

    log.info("discovery_complete", total_candidates=len(all_candidates))

    # ---- Main pipeline loop ----
    summary = RunSummary(studies_found=len(all_candidates))
    allowed_access = set(cfg["relevance"].get("allowed_access_types", ["open", "controlled"]))

    for candidate in all_candidates:
        try:
            # Access type filter
            if candidate.access_type not in allowed_access:
                log.info("skipped_access_type", accession=candidate.accession_id)
                summary.studies_skipped += 1
                tracker.record_study(
                    accession_id=candidate.accession_id,
                    source_repo=candidate.source_repository,
                    status=StudyStatus.REJECTED_RELEVANCE,
                )
                continue

            # Deduplication check
            if checker.is_duplicate(candidate):
                summary.studies_skipped += 1
                tracker.record_study(
                    accession_id=candidate.accession_id,
                    source_repo=candidate.source_repository,
                    status=StudyStatus.REJECTED_DUPLICATE,
                )
                continue

            # Relevance scoring
            result = scorer.score(candidate)
            if result is None:
                summary.errors += 1
                tracker.record_study(
                    accession_id=candidate.accession_id,
                    source_repo=candidate.source_repository,
                    status=StudyStatus.ERROR,
                )
                continue

            if not scorer.passes_filters(candidate, result):
                summary.studies_skipped += 1
                tracker.record_study(
                    accession_id=candidate.accession_id,
                    source_repo=candidate.source_repository,
                    status=StudyStatus.REJECTED_RELEVANCE,
                    relevance_score=result.relevance_score,
                    disease_focus=result.disease_focus,
                )
                continue

            # Create Synapse project
            project_id = creator.create_project(candidate, result)
            summary.studies_created += 1

            tracker.record_study(
                accession_id=candidate.accession_id,
                source_repo=candidate.source_repository,
                status=StudyStatus.SYNAPSE_CREATED,
                doi=candidate.doi,
                pmid=candidate.pmid,
                synapse_project_id=project_id,
                relevance_score=result.relevance_score,
                disease_focus=result.disease_focus,
            )

            # JIRA notification
            notifier.notify_new_study(
                study_name=result.suggested_study_name,
                repository=candidate.source_repository,
                accession_id=candidate.accession_id,
                synapse_project_id=project_id,
                relevance_score=result.relevance_score,
                disease_focus=result.disease_focus,
            )

            log.info(
                "study_created",
                accession=candidate.accession_id,
                project=project_id,
                score=result.relevance_score,
            )

        except Exception:
            summary.errors += 1
            log.exception("pipeline_error", accession=candidate.accession_id)
            tracker.record_study(
                accession_id=candidate.accession_id,
                source_repo=candidate.source_repository,
                status=StudyStatus.ERROR,
            )

    # ---- Log run summary ----
    tracker.log_run(summary)
    log.info(
        "run_complete",
        found=summary.studies_found,
        created=summary.studies_created,
        skipped=summary.studies_skipped,
        errors=summary.errors,
    )


if __name__ == "__main__":
    run()
