"""post_curation_comment.py — Post a source-tracked curation comment.

Called by the agent after each project's Step C gap-fill (first pass) and
again after Step 7b (audit remediation). Renders a GapReport JSON file as a
markdown comment with per-field source provenance and posts it to the
project's GitHub study-review issue.

Usage (from agent-generated scripts or shell):

    python scripts/post_curation_comment.py \\
        --issue-number 123 \\
        --gap-report-file /tmp/nf_agent/gap_report_synXXX.json \\
        --synapse-project-id synXXX

Environment:
    GITHUB_TOKEN, GITHUB_REPOSITORY — set automatically in Actions; otherwise
    the script prints the rendered comment to stdout instead of posting.

Exit code is 0 even if posting fails — curation-comment failure is non-fatal
to the overall daily run, matching the github_issue.py convention.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _ensure_imports() -> None:
    """Make the lib/ and scripts/ directories importable when invoked as a script."""
    repo_root = Path(
        os.environ.get("AGENT_REPO_ROOT") or Path(__file__).resolve().parent.parent
    )
    for sub in ("lib", "scripts"):
        p = str(repo_root / sub)
        if p not in sys.path:
            sys.path.insert(0, p)


_ensure_imports()

from gap_report import GapReport  # noqa: E402
from schema_properties import fetch_schema_properties  # noqa: E402


def build_comment(
    gap_report: GapReport,
    *,
    synapse_project_id: str | None,
    schema_props: dict | None,
) -> str:
    """Assemble the full markdown body to post on the GitHub issue."""
    project_url = (
        f"https://www.synapse.org/Synapse:{synapse_project_id}"
        if synapse_project_id
        else None
    )
    return gap_report.render_markdown(
        synapse_project_url=project_url,
        schema_props=schema_props,
    )


def post(issue_number: int, body: str) -> str | None:
    """Post the comment via github_issue.post_issue_comment. Return URL or None."""
    try:
        from github_issue import post_issue_comment
    except ImportError as e:
        print(f"  Could not import github_issue: {e}", file=sys.stderr)
        return None

    if not os.environ.get("GITHUB_TOKEN") or not os.environ.get("GITHUB_REPOSITORY"):
        # Local / dry-run mode — print and return without posting
        print("  GITHUB_TOKEN or GITHUB_REPOSITORY not set — printing comment instead of posting:")
        print("-" * 72)
        print(body)
        print("-" * 72)
        return None

    try:
        return post_issue_comment(issue_number, body)
    except Exception as e:  # non-fatal
        print(f"  WARN: post_issue_comment failed: {e}", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post a source-tracked NADIA curation comment on a GitHub issue."
    )
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument(
        "--gap-report-file",
        required=True,
        help="Path to a GapReport JSON produced by lib/gap_report.py",
    )
    parser.add_argument(
        "--synapse-project-id",
        default=None,
        help="Used to render a Synapse project link in the comment header.",
    )
    parser.add_argument(
        "--skip-schema-fetch",
        action="store_true",
        help="Skip fetching schema to compute completeness (faster, offline testing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the comment and print it, do not post to GitHub.",
    )
    args = parser.parse_args()

    try:
        with open(args.gap_report_file) as f:
            report = GapReport.from_json(f.read())
    except Exception as e:
        print(f"  ERROR: could not load gap report: {e}", file=sys.stderr)
        return 1

    schema_props: dict | None = None
    if report.schema_uri and not args.skip_schema_fetch:
        try:
            schema_props = fetch_schema_properties(report.schema_uri)
        except Exception as e:
            print(f"  WARN: could not fetch schema_props for completeness: {e}", file=sys.stderr)

    body = build_comment(
        report,
        synapse_project_id=args.synapse_project_id or report.project_id,
        schema_props=schema_props,
    )

    if args.dry_run:
        print(body)
        return 0

    comment_url = post(args.issue_number, body)
    result = {
        "issue_number": args.issue_number,
        "comment_url": comment_url,
        "stats": report.stats,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
