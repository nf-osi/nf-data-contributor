"""post_curation_comment.py — Post a source-tracked curation comment.

Called by the agent after each project's Step C gap-fill (first pass) and
again after Step 7b (audit remediation). Renders a GapReport JSON file as a
markdown comment with per-field source provenance and posts it to the
project's GitHub study-review issue.

When the report's completeness falls below ``--completeness-threshold``
(default 0.60), the script prepends a warning banner to the comment,
lists the three weakest fields, and applies the ``low-completeness``
label to the issue. When completeness recovers above the threshold on a
later pass, the label is removed automatically.

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


LOW_COMPLETENESS_LABEL = "low-completeness"


def build_completeness_banner(
    report: GapReport,
    completeness: float,
    threshold: float,
    *,
    label_applied: bool,
) -> str:
    """Return a markdown blockquote banner flagging low completeness.

    Called only when ``completeness < threshold``. Lists the three weakest
    fields so a reviewer knows where to look before scrolling into the main
    tables.
    """
    weakest = report.weakest_fields(limit=3)
    lines = [
        f"> ⚠️ **Low completeness: {completeness:.0%}** "
        f"(below {threshold:.0%} threshold)"
        + (f" — `{LOW_COMPLETENESS_LABEL}` label applied." if label_applied else "."),
        ">",
    ]
    if weakest:
        lines.append("> Top fields needing attention:")
        for field_name, reason in weakest:
            lines.append(f"> - `{field_name}` — {reason}")
    else:
        lines.append("> No specific gaps recorded — review the full report below.")
    lines.append("")
    return "\n".join(lines)


def build_comment(
    gap_report: GapReport,
    *,
    synapse_project_id: str | None,
    schema_props: dict | None,
    completeness_threshold: float,
    label_applied: bool,
) -> str:
    """Assemble the full markdown body to post on the GitHub issue."""
    project_url = (
        f"https://www.synapse.org/Synapse:{synapse_project_id}"
        if synapse_project_id
        else None
    )
    body = gap_report.render_markdown(
        synapse_project_url=project_url,
        schema_props=schema_props,
    )
    completeness = gap_report.completeness(schema_props)
    if completeness is not None and completeness < completeness_threshold:
        banner = build_completeness_banner(
            gap_report,
            completeness,
            completeness_threshold,
            label_applied=label_applied,
        )
        return banner + "\n" + body
    return body


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


def apply_completeness_label(
    issue_number: int,
    completeness: float | None,
    threshold: float,
) -> bool:
    """Add or remove the low-completeness label based on the current value.

    Returns True if the label is (now) applied, False otherwise. Silent when
    running without GitHub credentials or when completeness is None.
    """
    if completeness is None:
        return False
    if not os.environ.get("GITHUB_TOKEN") or not os.environ.get("GITHUB_REPOSITORY"):
        # Local / dry-run: just report what would happen
        return completeness < threshold

    try:
        from github_issue import add_issue_label, remove_issue_label
    except ImportError as e:
        print(f"  Could not import github_issue label helpers: {e}", file=sys.stderr)
        return False

    if completeness < threshold:
        return add_issue_label(issue_number, LOW_COMPLETENESS_LABEL)
    remove_issue_label(issue_number, LOW_COMPLETENESS_LABEL)
    return False


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
        "--completeness-threshold",
        type=float,
        default=0.60,
        help="Applies the low-completeness label + banner when completeness is below this (0-1).",
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

    completeness = report.completeness(schema_props)

    # Apply / remove the label BEFORE posting the comment so the banner text
    # accurately reflects whether the label is in place.
    label_applied = False
    if not args.dry_run:
        label_applied = apply_completeness_label(
            args.issue_number, completeness, args.completeness_threshold
        )
    else:
        label_applied = (
            completeness is not None and completeness < args.completeness_threshold
        )

    body = build_comment(
        report,
        synapse_project_id=args.synapse_project_id or report.project_id,
        schema_props=schema_props,
        completeness_threshold=args.completeness_threshold,
        label_applied=label_applied,
    )

    if args.dry_run:
        print(body)
        return 0

    comment_url = post(args.issue_number, body)
    result = {
        "issue_number": args.issue_number,
        "comment_url": comment_url,
        "completeness": completeness,
        "low_completeness_label_applied": label_applied,
        "stats": report.stats,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
