# NF Data Contributor Agent — Daily Run

**Today's date:** {{TODAY}}
**Search for datasets published since:** {{LOOKBACK_DATE}}

---

## Your Task

Run the full NF dataset discovery pipeline for today. Work through each step below in order. Write Python scripts to `/tmp/nf_agent/` and execute them. Use the context in CLAUDE.md for all reference information (APIs, auth patterns, annotation schemas, safety rules).

---

## Step 1 — Setup

```
mkdir -p /tmp/nf_agent
pip install synapseclient httpx biopython pyyaml scikit-learn anthropic --quiet
```

Then write and run a setup script that:
1. Authenticates with Synapse using `lib/synapse_login.py`
2. Calls `lib/state_bootstrap.py` to get (or create) the state table IDs
3. Loads all previously processed accession IDs from `NF_DataContributor_ProcessedStudies` into a Python set — this is your primary deduplication guard
4. Prints the count of previously processed accessions

Save the table IDs and accession set to files in `/tmp/nf_agent/` so subsequent scripts can read them without re-querying Synapse.

---

## Step 2 — Discover Candidates

Write and run a discovery script for **each repository** in the list below. For each repository:
- Query for datasets matching the NF/SWN search terms published since {{LOOKBACK_DATE}}
- Collect results into a standardized JSON format (see schema below)
- Save results to `/tmp/nf_agent/{repo_name}_candidates.json`
- Print: `{repo_name}: found N candidates`

Run repositories in this order (continue on error, log failures):
1. GEO (via NCBI Entrez, use `NCBI_API_KEY`)
2. SRA (via NCBI Entrez)
3. Zenodo
4. Figshare
5. OSF
6. ArrayExpress / BioStudies
7. EGA
8. PRIDE
9. MetaboLights
10. NCI PDC (GraphQL)

### Candidate JSON Schema
```json
{
  "title": "string",
  "abstract": "string (first 3000 chars)",
  "authors": ["string"],
  "source_repository": "string",
  "accession_id": "string",
  "doi": "string or null",
  "pmid": "string or null",
  "publication_date": "YYYY-MM-DD",
  "data_types": ["string"],
  "file_formats": ["string"],
  "sample_count": "integer or null",
  "access_type": "open | controlled | embargoed",
  "data_url": "string",
  "license": "string or null"
}
```

---

## Step 3 — Deduplicate

Write and run a deduplication script that:
1. Loads all `*_candidates.json` files from `/tmp/nf_agent/`
2. Deduplicates within the batch (same accession ID from multiple repos = keep one)
3. Filters out any accession already in the processed-accessions set from Step 1
4. Queries the portal tables (`syn52694652`) to filter out known accessions, DOIs, and PMIDs (**read-only SELECT only**)
5. Runs fuzzy title matching (TF-IDF cosine similarity ≥ 0.85) against portal study titles — candidates that match are logged as `rejected_duplicate` and skipped
6. Saves the surviving candidates to `/tmp/nf_agent/candidates_for_scoring.json`
7. Prints: `After deduplication: N candidates to score`

---

## Step 4 — Score Relevance

Write and run a scoring script that:
1. Loads `candidates_for_scoring.json`
2. For each candidate, calls the Claude API (`claude-sonnet-4-6`) using the scoring prompt in CLAUDE.md
3. Applies the filters: relevance ≥ 0.70, is_primary_data = true, sample_count ≥ 3 (if known), access_type ∈ {open, controlled}
4. Saves passing candidates + their scoring results to `/tmp/nf_agent/candidates_approved.json`
5. Saves rejected candidates with rejection reason to `/tmp/nf_agent/candidates_rejected.json`
6. Prints: `Scoring complete: N approved, M rejected`

---

## Step 5 — Create Synapse Projects

Write and run a Synapse creation script that:
1. Loads `candidates_approved.json`
2. For each candidate (stopping at 50 total):
   a. Creates a Synapse project named `EXT_{repository}_{accession_id}`
   b. Applies all required annotations (project level and entity level) — see CLAUDE.md
   c. Creates the standard folder hierarchy (`Raw Data`, `Analysis`, `Source Metadata`)
   d. Creates a dataset subfolder and ExternalLink pointer to `data_url` inside `Raw Data`
   e. Sets provenance on the ExternalLink
   f. Creates the wiki page using the template in CLAUDE.md
3. Saves a results map `{accession_id: synapse_project_id}` to `/tmp/nf_agent/created_projects.json`
4. Prints each created project: `Created: EXT_{repo}_{acc} -> {project_id}`

---

## Step 6 — Send JIRA Notifications

Write and run a notification script that:
1. Loads `created_projects.json` and `candidates_approved.json`
2. For each created project, creates a JIRA ticket using the pattern in CLAUDE.md
3. If JIRA credentials are not available (env vars missing), logs a warning and skips
4. Saves `{accession_id: jira_issue_key}` to `/tmp/nf_agent/jira_tickets.json`
5. Prints each ticket: `JIRA: {issue_key} created for {accession_id}`

---

## Step 7 — Update State Tables

Write and run a state update script that:
1. Loads all result files from `/tmp/nf_agent/`
2. For every candidate evaluated (approved, rejected, errored), appends a row to `NF_DataContributor_ProcessedStudies`
3. Appends one row to `NF_DataContributor_RunLog` with today's summary counts
4. Prints the final run summary:
   ```
   === NF Data Contributor Agent — Run Complete ===
   Date: {{TODAY}}
   Repositories queried: 10
   Candidates found: N
   After deduplication: N
   After relevance scoring: N approved
   Synapse projects created: N
   JIRA tickets created: N
   Errors: N
   ```

---

## Error Handling

- If any individual step script exits with a non-zero code, log the error, continue to the next step if possible, and ensure Step 7 still runs to record what did complete.
- If Synapse project creation fails for a specific candidate, log it as `status=error` in the state table, skip that candidate, and continue.
- If the Claude API call fails after 3 retries with exponential backoff, log the candidate as `status=error` and continue.
- If the JIRA step fails entirely, log a warning but do not fail the run.

---

## Done

When all 7 steps are complete, print "Agent run finished successfully." and exit.
