# Freshworks → Jira Migration: Backup Check & Update Toolkit

A collection of **standalone post-migration scripts** used after the main Freshworks (Freshrelease) → Jira migration pipeline has run. They do three jobs:

1. **Validate** what was uploaded to Jira against the local backup artifacts (`issues.json`, `id_mapping.json`, `versions_mapping.xlsx`) and against the source ("backend") system.
2. **Detect** discrepancies: missing issues, wrong or duplicate fix-versions, wrong statuses, broken parent links, dead service-account tokens, oversized content.
3. **Repair** them: fix versions, sprints, statuses, descriptions and custom fields directly in Jira, and rebuild the local mapping files when they are lost or stale.

> **How "backup" is used here.** In this toolkit the *backup* is the set of files the main pipeline produced when it uploaded the data (the payload JSON, the source-ID → Jira-key map, the version map), plus the original source system. Most scripts compare live Jira against those and then fix **Jira**. A smaller group rebuilds or heals the **local backup files** themselves (`getback.py`, `payload_tomapping.py`, and the "healing" of `id_mapping.json` inside `assign_versions_remaining_projects.py`).

These scripts are companions to the two main pipeline repos (*FRESHWORKS-TO-JIRA-MIGRATION-USING-API* and *…-USING-JSON-DATA*). They are "standalone" in the sense that each one is run on its own from the command line, but **many of them import modules from the main pipeline** (see [Dependencies](#dependencies-on-the-main-pipeline)).

---

## Table of contents

1. [What is in the repo](#what-is-in-the-repo)
2. [Script catalogue](#script-catalogue)
3. [Which script do I need? (symptom → script)](#which-script-do-i-need-symptom--script)
4. [Recommended workflow](#recommended-workflow)
5. [Dependencies on the main pipeline](#dependencies-on-the-main-pipeline)
6. [Setup and configuration](#setup-and-configuration)
7. [Safety: which scripts write to Jira](#safety-which-scripts-write-to-jira)
8. [Concurrency and service accounts](#concurrency-and-service-accounts)
9. [Inputs and outputs](#inputs-and-outputs)
10. [Known issues and cleanup](#known-issues-and-cleanup)

---

## What is in the repo

All files live in `for_fs/final_draft_code/final_draft_code/` (29 Python scripts, about 16,000 lines including old commented-out versions). No `.env`, `requirements.txt` or sample data is committed, and no credentials are hard-coded.

Tech stack: Python 3.10+, `requests`, `pandas`, `openpyxl`, `python-dotenv`, `beautifulsoup4`, `concurrent.futures` thread pools, Jira Cloud REST API v3 (including the `search/jql` endpoint), and the Freshworks/Freshrelease backend API.

```bash
pip install requests pandas openpyxl python-dotenv beautifulsoup4
```

---

## Script catalogue

Legend: 🔍 read-only (never writes to Jira) · ✏️ writes to Jira · 🗂️ rebuilds local backup/mapping files · 🩺 diagnostic

### A. Account and connectivity diagnostics

| Script | Type | What it does |
|--------|:----:|--------------|
| `check_jira_accounts.py` | 🩺🔍 | Calls `/rest/api/3/myself` once for every service account in the `jira_credentials.py` pool and reports which are healthy (200), expired (401), forbidden (403) or unreachable. `--verbose` prints full error bodies. Tokens are masked in output. |
| `check_accounts_live.py` | 🩺🔍 | A deeper live check for when you see 401s mid-run. Tests every account against `/myself` **and** a real issue (`python check_accounts_live.py FSDRY-75950`), because some setups pass `/myself` but fail on specific project data. |

### B. Validation and audit (find discrepancies, change nothing)

| Script | Type | What it checks |
|--------|:----:|----------------|
| `error.fin.py` | 🔍 | **Payload vs. id_mapping.** For every issue in `issues.json`, checks whether it was actually created in Jira (found in `id_mapping.json`). Writes an Excel report with Jira key, id, type, title and created date for mapped issues, and flags the unmapped ones. (Docstring calls it `payload_vs_id_mapping_check.py`.) |
| `error_parent.py` | 🔍 | **Why did an issue fail?** For each failed issue in `errors.json`, looks up its parent/epic in the payload and checks whether that parent was itself created in Jira. Produces an Excel/CSV explaining which failures are caused by a missing parent. (Docstring: `failed_issues_parent_check.py`.) |
| `check_duplicate_version.py` | 🔍 | **Fix-version audit.** For every issue in `id_mapping`, resolves the version it *should* have (using `versions_mapping.xlsx` as ground truth, keyed `source_release_id::project`), fetches the live `fixVersions` from Jira and classifies each issue: `OK`, `MISSING`, `WRONG_ONLY`, `CORRECT_PLUS_EXTRA`, `MULTIPLE_WRONG`, `UNRESOLVED`, `NO_META`, `GET_FAILED`. Flags issues with 2+ or 3+ versions. Also reports where `id_mapping` disagrees with the Excel. Output: `mapping/check_duplicate_version.xlsx/.json`. Options: `--projects CEMIG,PLAT`, `--only-mismatches`. |
| `audit_wrong_versions.py` | 🔍 | A second, per-issue version audit. Writes `wrong_versions.json` (issues needing a fix, with exactly which version ids to remove/add) and `unresolved_versions.json` (needs manual attention). You review/edit these files before applying a fix. Skips issues already in `known_correct_versions.json`. Options: `--limit`, `--workers`. |
| `check_issue_status.py` | 🔍 | **Status audit using one JQL search** instead of one call per issue. Finds issues for a given FR project, issue types, status and creator accounts; for each, compares live status with the **payload status**, finds when status last changed via the changelog, and sets `Last Update = Yes` if the status was never touched after the migration. Output: `issue_status_report.xlsx/.json`. Every filter can be overridden (`--status-filter`, `--fr-project`, `--issue-type-filter`, `--creator-ids`, `--jql`). |
| `analyze_i2p_links.py` | 🔍 | **Content-size / link audit** for the backend field `cf_i2p_document`. For each Jira key, reads the FR key from Jira, fetches the original value from the backend, counts `<a href>` links (excluding `target="_blank"`), flags hrefs over 250 characters, and reports total / excluded / net character counts to Excel. 15 parallel workers by default. |
| `jql_search.py` | 🔍 | Interactive. Asks for a text value (for example `cf_release_quarter`) and an optional extra JQL clause, searches the "Default FR Field" paragraph field, then maps each matched value to a user id/status using an exported Jira users file (`export-users.csv`). Writes an Excel to `jql_output/`. 20 workers. |
| `fetch_issue_links.py` | 🔍 | Reads Jira keys from an Excel (or a `*_failed_keys.json`), fetches each issue's links from Jira and writes `jira_source_key | jira_target_key | link_type | direction`. Logs every issue to a per-issue log, retries 429s, and writes failure files only if there are failures so the failed keys can be fed straight back in with `--input`. |
| `getsummary.py` | 🔍 | Exports a 3-column report (`jira_key`, `issue_type`, custom field value, default `customfield_13038` = FR key) for every project in the `TRANSFER_KEY` env var, joining `id_mapping.json` with the payload. Useful as a migrated-issue inventory. (Docstring: `export_customfield_report.py`.) |
| `debug_single_issue.py` | 🩺 (🔍 by default) | Deep-dive on **one issue's** fix-version. Prints each resolution step (Excel lookup, live validation, name-match fallback). Inspect-only unless you pass `--apply`. Example: `python debug_single_issue.py 87246` |

### C. Rebuilding the local backup / mapping files

| Script | Type | What it does |
|--------|:----:|--------------|
| `getback.py` | 🗂️🔍 | **Recovers `id_mapping` from live Jira.** Reads an Excel (`Source ID`, `Jira Key`, optional `Issue Type`, `Title`, `Sprint`) and rebuilds issues, comments, versions, sprints and links by querying Jira. Writes `mapping/id_mapping1.json` (a new file, so the original is not overwritten). Supports OAuth service-account tokens or email + API token. Flags: `--skip-comments --skip-links --skip-versions --skip-sprints --workers`. |
| `getback1.py` | 🗂️🔍 | Simpler variant of `getback.py`: email + API token only (no OAuth), 5 workers. Same output. |
| `payload_tomapping.py` | 🗂️ | Re-exports **every** row of `versions.json` to `versions_mapping.xlsx` (one row per `(source_release_id, project)`), looking up the Jira version id in `id_mapping` (composite key first, then the legacy key). Fixes the old export that dropped rows when several projects shared one release id. Unmatched rows are counted as a warning. (Docstring: `export_versions_from_payload.py`.) |

### D. Repair scripts (write to Jira)

**Versions**

| Script | What it does |
|--------|--------------|
| `post_versions_to_jira.py` | Posts versions using a composite key (`source_release_id::project`) so the same source release gets its own version in each Jira project, then assigns them. Flags: `--skip-assign`, `--assign-only`. Fixes the "version already exists in this project" error caused by an id keyed only on the source release. (Docstring: `fix_multiproject_versions.py`.) |
| `assign_versions_remaining_projects.py` | Assigns fix-versions only for selected projects (default CEMIG, PLAT) so you don't re-touch thousands of already-correct issues. Resolves from `id_mapping`, then the Excel, then **creates** the version in the target project if none exists. On "Specify a valid value for fixVersions" it looks up or creates the right version by name, heals `id_mapping`, and retries once. `--projects`, `--dry-run`. |
| `fix_version_issues.py` | For every issue with a source release id: resolves the correct version, and **removes only the wrong ones** while keeping the correct one. Issues already correct are skipped with no write. Confirmed issues are remembered in `known_correct_versions.json` so later runs make zero API calls for them. Outputs `fix_versions_report.json`, `fix_versions_failed.json`. `--dry-run`, `--limit`. |
| `apply_wrong_versions_fix.py` | Applies exactly what is in the reviewed `wrong_versions.json` (from `audit_wrong_versions.py`): one PUT per issue, removing the listed ids and adding the right one. Nothing is recomputed; delete an entry and that issue is never touched. 10 workers. `--dry-run`, `--limit`, `--input`. |
| `fix_duplicate_version.py` | Reads `check_duplicate_version.json` and fixes every non-correct row with a **full replace** of `fixVersions` to exactly one id, using a fixed priority: Excel id → `id_mapping` id → already present → single valid existing version → otherwise `NEEDS_MANUAL_REVIEW`. Re-fetches after each PUT and flags `STILL_WRONG`. Writes a timestamped log to `fix_log/`. `--projects`, `--input`, `--dry-run`. (Docstring: `fix_from_check_duplicate.py`.) |
| `run_version_assign_only.py` | Re-runs only fix-version assignment (same as `post_versions_to_jira.py --assign-only`). Safe to repeat; adding an existing version is a no-op. |

**Sprints and statuses**

| Script | What it does |
|--------|--------------|
| `run_sprint_assign_only.py` | Re-runs only sprint assignment (Phase 3A). Failures are logged as `sprint_assign` in `errors.json` with Jira's real error. Most common cause: the sprint's board is not associated with the issue's project. |
| `run_status_assign_only.py` | Re-applies status transitions. **Checks the current status first**, so an issue already in the target status counts as a clean no-op rather than a false "no transition available" failure. Single-hop only; multi-step workflows are reported as failures with the available transitions listed. 6 workers. |
| `python run_status_assign_only.py` | ⚠️ A stray file whose name accidentally starts with `python `. It looks like a **newer copy** of `run_status_assign_only.py` that adds an auth-error circuit breaker (stops after 15 consecutive 401/403s instead of burning through the whole list). Rename it and keep one. |
| `update_backlog_to_done.py` | Fixes issues migrated as **Backlog** whose real final status in the payload is **Done**. Four checks per key: current status is Backlog → payload final status is Done → (optional `--check-changelog`) nobody changed it since creation → transition to Done and re-read. **Dry run by default**; add `--apply`. Sends `notifyUsers=false`. |

**Descriptions and custom fields**

| Script | What it does |
|--------|--------------|
| `run_patch_descriptions_fixed.py` | Fixed version of the "patch descriptions with attachment links" phase. The original read stale per-issue cache files, so the PUT sent an identical description and did nothing. This version rebuilds payloads **in memory**, compares against the current Jira description first (skipping and flagging no-ops), and runs on a thread pool of 6. |
| `append_i2p_document.py` | For each Jira key: reads the FR key, fetches `cf_i2p_document` (HTML) from the backend, converts it to an ADF paragraph labelled `cf_i2p_document : …`, **appends** it to the rich-text field `customfield_13034`, and clears `customfield_13041`. `notifyUsers=false`. `--dry-run`, `--keys-file`. |
| `update_release_quarter.py` | Same idea for `cf_release_quarter`, but it **replaces** the existing labelled block in place (or appends if absent). If the backend value is empty, it removes the block entirely. `--dry-run`, `--keys-file`. |
| `removei2p.py` | Undoes `append_i2p_document.py`: finds and strips the paragraphs labelled `cf_i2p_document : ` from `customfield_13034`, leaving everything else untouched. `--dry-run`, `--keys-file`. |
| `freshwork_req.py` | Reads a TSV (FR Project / FR Subproject / current squad / target squad), builds a JQL filter for each row, and updates the **child** value of the Squad cascading field (`customfield_10973`) while leaving the parent untouched. Also writes one Excel per target squad in `project_wise/`. **Dry run by default**; `--apply` to write. `--auth basic|bearer` (Bearer is needed for `api.atlassian.com/ex/jira/<cloudId>` URLs). (Docstring: `fetch_jira_issues.py`.) |

---

## Which script do I need? (symptom → script)

| Symptom | Start with | Then |
|---------|-----------|------|
| Seeing 401/403 mid-run | `check_jira_accounts.py` → `check_accounts_live.py <KEY>` | Replace the bad token in `.env` |
| Not sure every source issue made it into Jira | `error.fin.py` | Re-run the loader for the unmapped ones |
| Child issues missing a parent | `error_parent.py` | Re-run the parent first, then the child |
| `id_mapping.json` lost or corrupted | `getback.py` (or `getback1.py`) | Review `id_mapping1.json`, then swap it in |
| `versions_mapping.xlsx` missing rows for some projects | `payload_tomapping.py` | — |
| Issues have wrong, missing or multiple fix-versions | `check_duplicate_version.py` → `fix_duplicate_version.py --dry-run` → real run | Or `audit_wrong_versions.py` → edit `wrong_versions.json` → `apply_wrong_versions_fix.py` |
| One stubborn issue won't take its version | `debug_single_issue.py <source_id>` | `--apply` once the cause is clear |
| Whole projects never got versions | `assign_versions_remaining_projects.py --projects X,Y` | — |
| Issues not in sprints | `run_sprint_assign_only.py` | Check `errors.json` for `sprint_assign` |
| Issues stuck in the wrong status | `check_issue_status.py` → `run_status_assign_only.py` | Backlog→Done specifically: `update_backlog_to_done.py` |
| Descriptions missing attachment links | `run_patch_descriptions_fixed.py` | — |
| Backend field values need to be copied into Jira | `append_i2p_document.py` / `update_release_quarter.py` | Undo with `removei2p.py` |
| Need link data from Jira | `fetch_issue_links.py` | Retry failures with `*_failed_keys.json` |
| Need a list of migrated issues with their FR keys | `getsummary.py` | — |
| Squad values need correcting in bulk | `freshwork_req.py` (dry run first) | — |

---

## Recommended workflow

1. **Health check:** `check_jira_accounts.py`. Fix any 401/403 before anything else.
2. **Audit, read-only:** `error.fin.py`, `check_duplicate_version.py`, `check_issue_status.py`. Review the Excel reports.
3. **Dry-run the repair:** run the matching fix script with `--dry-run` (or without `--apply`).
4. **Apply:** run for real, ideally on a small `--limit` first.
5. **Re-audit:** run the audit script again and confirm the discrepancy count dropped to zero. `fix_duplicate_version.py` also verifies each issue immediately after writing.
6. **Refresh the backup:** if `id_mapping` or `versions_mapping.xlsx` changed or was healed, re-export with `payload_tomapping.py` so the local backup matches Jira.

---

## Dependencies on the main pipeline

These scripts are **not** fully self-contained. Copy them into the main pipeline folder (or add that folder to `PYTHONPATH`).

| Type | Scripts |
|------|---------|
| Import `bulk_load_to_jira` as `core` (shared `.env`, credential pool, `jira_get/jira_put`, `id_mapping`, rate monitor) | `run_sprint_assign_only`, `run_version_assign_only`, `run_status_assign_only`, `run_patch_descriptions_fixed`, `post_versions_to_jira`, `assign_versions_remaining_projects`, `check_duplicate_version`, `fix_duplicate_version`, `check_issue_status`, `update_backlog_to_done` (read-only use) |
| Also import `prepare_payload`, `mappings` | `run_patch_descriptions_fixed` (`prepare_payload`), `getback*` (`mappings`, optional) |
| Use the `jira_credentials` service-account pool | `check_jira_accounts`, `check_accounts_live`, `fetch_issue_links`; optional fallback in `audit_wrong_versions`, `fix_version_issues`, `apply_wrong_versions_fix`, `debug_single_issue` |
| Truly standalone (only `.env` + Jira/backend) | `analyze_i2p_links`, `append_i2p_document`, `update_release_quarter`, `removei2p`, `jql_search`, `getsummary`, `error.fin`, `error_parent`, `payload_tomapping`, `freshwork_req` |

The runner scripts also call helpers such as `core.validate_all_accounts()`, `core.save_error_log()` and `core.start_rate_monitor()`. Make sure your copy of `bulk_load_to_jira.py` defines them.

---

## Setup and configuration

Create a `.env` in the folder you run from. **There are two naming conventions**, so you may need both sets of variables.

**Pipeline-style scripts** (the ones that import `bulk_load_to_jira` / `jira_credentials`):

```env
JIRA_URL=https://your-site.atlassian.net
JIRA_EMAIL=service-account-1@yourcompany.com
JIRA_API_TOKEN=...
JIRA_EMAIL_2=...            # optional extra service accounts, contiguous numbering
JIRA_API_TOKEN_2=...
project_key=FS
```

**Standalone-style scripts** (`analyze_i2p_links`, `append_i2p_document`, `update_release_quarter`, `removei2p`, `jql_search`, `update_backlog_to_done`):

```env
JIRA_BASE_URL=https://your-site.atlassian.net     # note: BASE_URL, not JIRA_URL
JIRA_EMAIL=you@yourcompany.com
JIRA_API_TOKEN=...
# JIRA_PAT=...                                   # alternative: bearer token

BACKEND_BASE_URL=https://backend.example.com      # Freshworks/Freshrelease source API
BACKEND_AUTH_TOKEN=...                            # sent as "Authorization: Token <value>"
```

**Other variables**

| Variable | Used by |
|----------|---------|
| `TRANSFER_KEY` (JSON array or comma list of project keys) | `getsummary.py` |
| `PROJECT_KEY` / `project_key` | `getback*.py`, `check_*`, pipeline-style scripts |
| `JIRA_ACCESS_TOKEN` or `JIRA_CLIENT_ID` + `JIRA_CLIENT_SECRET` | `getback.py` (OAuth service account) |
| `VERSION_AUDIT_MAX_WORKERS`, `VERSION_FIX_MAX_WORKERS`, `VERSION_APPLY_MAX_WORKERS` (default 10) | version audit / fix / apply |
| `STATUS_ASSIGN_MAX_WORKERS`, `DESC_PATCH_MAX_WORKERS` (default 6) | status and description runners |

**Hard-coded Jira custom fields** you must adapt for a different Jira site:

| Field id | Meaning |
|----------|---------|
| `customfield_13038` | FR Key (source-system key, e.g. `FD-197277`) |
| `customfield_13034` | "Default FR Field" (ADF paragraph field that receives backend values) |
| `customfield_13041` | FR Roadmap classification migration (plain text) |
| `customfield_10973` | Squad (cascading select) |

---

## Safety: which scripts write to Jira

| Behaviour | Scripts |
|-----------|---------|
| **Dry run by default** (need `--apply` to write) | `update_backlog_to_done.py`, `freshwork_req.py`, `debug_single_issue.py` |
| **Writes by default**, `--dry-run` available | `fix_version_issues.py`, `apply_wrong_versions_fix.py`, `fix_duplicate_version.py`, `assign_versions_remaining_projects.py`, `append_i2p_document.py`, `update_release_quarter.py`, `removei2p.py` |
| **Writes with no dry-run flag** | `run_sprint_assign_only.py`, `run_version_assign_only.py`, `run_status_assign_only.py`, `run_patch_descriptions_fixed.py`, `post_versions_to_jira.py` (use `--skip-assign` to post versions only) |
| **Read-only** | `check_*`, `audit_wrong_versions.py`, `error.fin.py`, `error_parent.py`, `analyze_i2p_links.py`, `jql_search.py`, `fetch_issue_links.py`, `getsummary.py`, `getback*.py`, `payload_tomapping.py` (writes a local Excel only) |

Good habits already built in: most field updates send `notifyUsers=false` so watchers get no emails; `known_correct_versions.json` prevents re-touching confirmed issues; `getback` writes to `id_mapping1.json` instead of overwriting `id_mapping.json`; `apply_wrong_versions_fix.py` only applies the entries you left in the reviewed file.

**Before running any write script on a large set:** take a Jira backup or export, run with `--dry-run` or `--limit 20` first, and confirm the report looks right.

---

## Concurrency and service accounts

| Script | Parallelism |
|--------|-------------|
| `fetch_issue_links.py` | **Per service account:** `--workers-per-account` (default 5). With 50 accounts that is 250 threads, each account backing about 5. On a 429 it waits at least 30 s (or `Retry-After` if larger) and retries up to 10 times per issue. |
| `getback.py` | `MAX_WORKERS` is 20 in code, but the `--workers` flag (default **5**) overrides it, so the effective default is 5. Request delay 0.05 s. |
| `getback1.py` | 5 workers |
| `audit_wrong_versions.py`, `fix_version_issues.py`, `apply_wrong_versions_fix.py` | 10 workers each (env-tunable), 0.3 s delay, 3 retries |
| `analyze_i2p_links.py` | 15 workers (`--workers`) |
| `jql_search.py` | 20 workers |
| `run_status_assign_only.py`, `run_patch_descriptions_fixed.py` | 6 workers each |
| `run_sprint_assign_only.py`, `run_version_assign_only.py` | `SPRINT_ASSIGN_MAX_WORKERS` / `VERSION_ASSIGN_MAX_WORKERS` from the main loader (default 6) |
| Standalone single-account scripts (`append_i2p_document`, `update_release_quarter`, `removei2p`, `update_backlog_to_done`, `freshwork_req`) | Sequential, one account |

**Service accounts needed:** one is enough for everything. Extra accounts only help the scripts that use the shared pool, mainly `fetch_issue_links.py`. As in the main pipeline, Atlassian's rate limits may be shared across accounts, so more accounts do not guarantee more throughput. Lower the worker counts if you see repeated 429s.

---

## Inputs and outputs

| Artifact | Produced by | Consumed by |
|----------|-------------|-------------|
| `{project_key}_data/payload/issues.json`, `issues_meta.json`, `versions.json` | main pipeline | audits, `error*.py`, `getsummary`, `update_backlog_to_done`, `check_issue_status` |
| `{project_key}_data/mapping/id_mapping.json` | main pipeline / `getback.py` | most scripts |
| `mapping/versions_mapping.xlsx` | main pipeline / `payload_tomapping.py` | version audit and fix scripts |
| `mapping/errors.json`, `failed_issues.json` | main pipeline | `error_parent.py` |
| `wrong_versions.json`, `unresolved_versions.json` | `audit_wrong_versions.py` | `apply_wrong_versions_fix.py` |
| `known_correct_versions.json` | fix scripts | audit and fix scripts (skip list) |
| `mapping/check_duplicate_version.json/.xlsx` | `check_duplicate_version.py` | `fix_duplicate_version.py` |
| `mapping/id_mapping1.json` | `getback*.py` | you (review, then replace `id_mapping.json`) |
| `{project}_issue_status/…/*.xlsx/.json` | `check_issue_status.py`, `update_backlog_to_done.py` | review |
| `transfer_summary/…xlsx` | `getsummary.py` | `fetch_issue_links.py` |
| `jql_output/*.xlsx`, `project_wise/*.xlsx` | `jql_search.py`, `freshwork_req.py` | review |
| `fix_log/*.log` | `fix_duplicate_version.py` | review |

---

## Known issues and cleanup

- **Hard-coded absolute paths.** Six scripts (`audit_wrong_versions`, `fix_version_issues`, `apply_wrong_versions_fix`, `debug_single_issue`, `error.fin`, `error_parent`, `fetch_issue_links` defaults) point at a specific developer's home directory such as `/home/<user>/fs_code/…/FS_data`. Edit `BASE_FS_DATA` and the `DEFAULT_*` paths, or switch them to `project_key`-relative paths, before running anywhere else.
- **Hard-coded Jira account ids.** `check_issue_status.py` (creator filter) and `update_backlog_to_done.py` (`MIGRATION_ACCOUNT_IDS`) contain two Jira account ids tied to the original migration accounts. Replace them with your own.
- **Docstring names don't match file names** because files were renamed:

  | File | Docstring calls it |
  |------|--------------------|
  | `error.fin.py` | `payload_vs_id_mapping_check.py` |
  | `error_parent.py` | `failed_issues_parent_check.py` |
  | `payload_tomapping.py` | `export_versions_from_payload.py` |
  | `getsummary.py` | `export_customfield_report.py` |
  | `post_versions_to_jira.py` | `fix_multiproject_versions.py` |
  | `fix_version_issues.py` | `fix_issue_versions.py` |
  | `fix_duplicate_version.py` | `fix_from_check_duplicate.py` |
  | `freshwork_req.py` | `fetch_jira_issues.py` |

- **Dead commented-out code.** `jql_search.py`, `append_i2p_document.py`, `assign_versions_remaining_projects.py`, `fix_version_issues.py`, `fix_duplicate_version.py`, `check_issue_status.py` and `freshwork_req.py` each begin with one or more complete older versions commented out (roughly 40–70% of the file). Only the last, uncommented block is live. Removing the old blocks (Git history keeps them) would shrink the repo by thousands of lines.
- **Stray file** `python run_status_assign_only.py` should be renamed or removed (see the catalogue).
- **Two env naming schemes** (`JIRA_URL` vs `JIRA_BASE_URL`, and `AUTH_TOKEN`/`BASE_URL` in the main pipeline vs `BACKEND_AUTH_TOKEN`/`BACKEND_BASE_URL` here) are easy to mix up. Standardising them would remove a common setup error.
- **No tests, `requirements.txt` or `.env.sample`.**
- **No licence file.**
