"""JIRA notification module.

Creates a JIRA ticket for each new Synapse project that is pending data manager
review. Uses the Atlassian REST API v3.
"""

from __future__ import annotations

import os

import httpx
import structlog

logger = structlog.get_logger(__name__)


class JiraNotifier:
    """Creates JIRA issues for pending-review studies."""

    def __init__(
        self,
        base_url: str | None = None,
        email: str | None = None,
        api_token: str | None = None,
        project_key: str = "NFOSI",
        issue_type: str = "Task",
        assignee_email: str = "nf-osi@sagebionetworks.org",
    ) -> None:
        self.base_url = (base_url or os.environ.get("JIRA_BASE_URL", "")).rstrip("/")
        self.email = email or os.environ.get("JIRA_USER_EMAIL", "")
        self.api_token = api_token or os.environ.get("JIRA_API_TOKEN", "")
        self.project_key = project_key
        self.issue_type = issue_type
        self.assignee_email = assignee_email
        self.log = structlog.get_logger(self.__class__.__name__)
        self.enabled = bool(self.base_url and self.email and self.api_token)

    def notify_new_study(
        self,
        study_name: str,
        repository: str,
        accession_id: str,
        synapse_project_id: str,
        relevance_score: float,
        disease_focus: list[str],
    ) -> str | None:
        """Create a JIRA ticket and return the issue key (e.g. 'NFOSI-42')."""
        if not self.enabled:
            self.log.info("jira_disabled_skipping", accession=accession_id)
            return None

        summary = (
            f"Review auto-discovered study: {study_name} "
            f"({repository}:{accession_id})"
        )
        description = self._build_description(
            study_name, repository, accession_id,
            synapse_project_id, relevance_score, disease_focus,
        )

        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary[:254],
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": description}],
                        }
                    ],
                },
                "issuetype": {"name": self.issue_type},
            }
        }

        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(
                    f"{self.base_url}/rest/api/3/issue",
                    json=payload,
                    auth=(self.email, self.api_token),
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                issue_key = resp.json().get("key")
                self.log.info("jira_created", issue=issue_key, accession=accession_id)
                return issue_key
        except Exception:
            self.log.exception("jira_create_error", accession=accession_id)
            return None

    def _build_description(
        self,
        study_name: str,
        repository: str,
        accession_id: str,
        synapse_project_id: str,
        relevance_score: float,
        disease_focus: list[str],
    ) -> str:
        synapse_url = f"https://www.synapse.org/#!Synapse:{synapse_project_id}"
        return (
            f"The NF Data Contributor Agent automatically discovered a new external dataset "
            f"that is pending data manager review.\n\n"
            f"Study Name: {study_name}\n"
            f"Repository: {repository}\n"
            f"Accession ID: {accession_id}\n"
            f"Relevance Score: {relevance_score:.2f}\n"
            f"Disease Focus: {', '.join(disease_focus)}\n"
            f"Synapse Project: {synapse_url}\n\n"
            f"Please review the Synapse project, verify the metadata, and update "
            f"resourceStatus to 'approved' or 'rejected' accordingly.\n\n"
            f"Assignee: {self.assignee_email}"
        )
