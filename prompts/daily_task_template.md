# NF Data Contributor Agent — Daily Run

**Today's date:** {{TODAY}}
**Search for datasets published since:** {{LOOKBACK_DATE}}

---

## Your Task

Run the full NF dataset discovery pipeline. Write Python scripts to `/tmp/nf_agent/` and execute them. Refer to CLAUDE.md for all reference information (APIs, auth, annotation schemas, safety rules, dedup logic).

---

## Step 1 — Setup

Install dependencies and authenticate:
```
mkdir -p /tmp/nf_agent
pip install synapseclient httpx biopython pyyaml scikit-learn anthropic --quiet
```

Write and run `/tmp/nf_agent/setup.py`:
1. Authenticate with Synapse via `lib/synapse_login.py` — print the logged-in username
2. Call `lib/state_bootstrap.py` to get or create state table IDs (if `STATE_PROJECT_ID` env var is empty, create a Synapse project named "NF DataContributor Agent State" and use that)
3. Load all previously processed accession IDs from `NF_DataContributor_ProcessedStudies` into a set
4. Save table IDs, the accession set, and the state project ID to `/tmp/nf_agent/state.json`
5. Print: "Setup complete. Previously processed: N accessions"

---

## Step 2 — Discover Candidates

Write and run one script per repository. For each, search for NF/SWN terms published since {{LOOKBACK_DATE}}, fetch up to 10 results, and save to `/tmp/nf_agent/{repo}_candidates.json` using the standard candidate schema from CLAUDE.md.

Run repositories in order (log errors and continue on failure):
1. GEO — NCBI Entrez, use `NCBI_API_KEY` env var if set
2. SRA — NCBI Entrez
3. Zenodo
4. Figshare
5. OSF
6. ArrayExpress / BioStudies
7. EGA
8. PRIDE
9. MetaboLights
10. NCI PDC (GraphQL)

For each repository, print: `{repo}: found N candidates`

---

## Step 3 — Enrich with Publication Metadata and Group by Publication

Write and run `/tmp/nf_agent/group_by_publication.py`:

1. Load all `*_candidates.json` files, combine into a flat list, deduplicate by accession_id
2. **Enrich with publication metadata**: For candidates with a PMID, fetch the full PubMed record via Entrez to get the paper title and abstract (often richer than the repository description). For GEO accessions without a PMID, check the GEO record for linked publications.
3. **Group by publication** using the logic in CLAUDE.md:
   - Primary key: shared PMID
   - Secondary key: shared DOI
   - Tertiary: fuzzy title similarity ≥ 0.85 across candidates
   - Each candidate with no match becomes its own group
4. Save publication groups to `/tmp/nf_agent/publication_groups.json`
5. Print: "N candidates → M publication groups"
   - Show each group: `[PMID:12345] "Paper Title" — 2 datasets: GEO:GSE123, SRA:SRP456`

---

## Step 4 — Deduplicate Against Portal

Write and run `/tmp/nf_agent/dedup.py`:

1. Load `publication_groups.json` and `state.json`
2. **Inspect the portal schema first**: query `SELECT * FROM syn52694652 LIMIT 1` and `SELECT * FROM syn16858331 LIMIT 1` to see the actual column names before writing any matching queries
3. Classify each publication group as NEW, ADD, or SKIP using the three-outcome logic in CLAUDE.md:
   - Check by PMID (exact match against portal)
   - Check by DOI (case-insensitive)
   - Check by accession ID in portal files table
   - Check by fuzzy title (TF-IDF cosine similarity)
   - If similarity 0.70–0.84: classify as NEW but log a "near-match warning" with the portal study name
4. Save results to `/tmp/nf_agent/dedup_results.json`:
   ```json
   {
     "new": [ {publication_group} ],
     "add": [ {"group": {publication_group}, "existing_project_id": "syn...", "new_datasets": [...]} ],
     "skip": [ {"group": {publication_group}, "reason": "..."} ],
     "near_matches": [ {"group": {publication_group}, "portal_study": "...", "similarity": 0.77} ]
   }
   ```
5. Print summary:
   ```
   Dedup results: N new, M add-to-existing, K skip, J near-matches flagged
   ```

---

## Step 5 — Score Relevance

Write and run `/tmp/nf_agent/score.py`:

1. Load `dedup_results.json` — score all groups in `new` and `add` lists
2. For each publication group, call `claude-sonnet-4-6` using the publication-level scoring prompt in CLAUDE.md (use the PubMed abstract if available, fall back to the richest repository abstract)
3. Apply filters: relevance ≥ 0.70, is_primary_data = true, sample_count ≥ 3 (if known)
4. Save to `/tmp/nf_agent/scored.json` with relevance results attached to each group
5. Print each result:
   ```
   [NEW] [APPROVED] "Gene Expression Profiling of NF1 MPNSTs" — score: 0.95 — GEO:GSE301187
   [ADD] [APPROVED] "NF2 Schwann Cell Proteomics" — score: 0.88 — adding PRIDE:PXD012345 to syn12345
   [NEW] [REJECTED] "KRAS plasma samples" — score: 0.05 — low relevance
   ```

---

## Step 6 — Create / Update Synapse Projects

Write and run `/tmp/nf_agent/synapse_actions.py`:

For each approved publication group (stopping at 50 total write operations):

**For NEW groups:**
1. Create a Synapse project named after the publication title (`suggested_project_name` from Claude)
2. Create the standard folder hierarchy: `Raw Data/`, `Analysis/`, `Source Metadata/`
3. For each dataset in the group, create a `{Repository}_{AccessionID}/` subfolder inside `Raw Data/` with an ExternalLink pointer and dataset-level annotations
4. Apply project-level annotations (study, resourceType, resourceStatus, pmid, doi)
5. Create provenance on each ExternalLink
6. Create the wiki page using the template in CLAUDE.md
7. Print: `Created: "{project_name}" ({project_id}) — {N} datasets`

**For ADD groups:**
- If the existing project is an agent-created project (found in agent state table): find its `Raw Data/` folder and add the new dataset subfolder(s) following the same pattern
- If the existing project is a portal-managed project (found only in portal table, no agent state entry): **do not write to it** — create a JIRA ticket flagged "[Manual]" and log as `dataset_added` with a note
- Print: `Added: {Repository}:{AccessionID} → existing project {project_id}`

Save results to `/tmp/nf_agent/created_projects.json`:
```json
[
  {"action": "created", "project_id": "syn...", "project_name": "...", "accession_ids": [...]},
  {"action": "added",   "project_id": "syn...", "project_name": "...", "accession_ids": [...]}
]
```

---

## Step 7 — Send JIRA Notifications

Write and run `/tmp/nf_agent/notify.py`:

For each created or updated project:
- NEW project: `"Review auto-discovered study: {project_name}"`
- Dataset added to agent project: `"New dataset linked to existing study: {project_name} — {repo}:{accession}"`
- Dataset requiring manual link to portal project: `"[Manual] Link external dataset to portal study: {project_name} — {repo}:{accession}"`

If JIRA credentials are missing/invalid, log a warning and skip.
Save ticket results to `/tmp/nf_agent/jira_tickets.json`.

---

## Step 8 — Update State Tables

Write and run `/tmp/nf_agent/update_state.py`:

1. For every accession evaluated, append a row to `NF_DataContributor_ProcessedStudies`
2. Append one run summary row to `NF_DataContributor_RunLog`
3. Print the final run summary:
   ```
   === NF Data Contributor Agent — Run Complete ===
   Date: {{TODAY}}
   Candidates found: N
   Publication groups: N
   Dedup — new: N | add: N | skip: N | near-matches flagged: N
   After relevance scoring: N approved
   Synapse projects created: N
   Datasets added to existing projects: N
   JIRA tickets created: N
   Errors: N
   ================================================
   ```

---

## Error Handling

- If any step script exits non-zero, log the error and continue to the next step where possible
- Always run Step 8 to record what did complete, even after failures
- If Synapse project creation fails for a specific group, log it as `status=error` and continue
- If the Claude API fails after 3 retries with exponential backoff, log as `status=error` and continue
