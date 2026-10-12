# """
# fix_issue_versions.py

# Standalone script — goes through every issue in issues_meta.json that has a
# non-empty `source_release_id` ("has a value for version"), resolves it to a
# Jira issue key via id_mapping.json, resolves the correct Jira version id via
# versions_mapping.xlsx (strict lookup by (release_id, project) — project is
# derived from the issue's own jira_key, e.g. "EX-202958" -> "EX"), and
# reassigns fixVersions on the live issue.

# If an issue already has MORE THAN ONE fixVersion set, only the WRONG one(s)
# are removed — the correct one, if already present, is left exactly as is
# and never re-added. If the issue's fixVersions are already exactly
# [correct_id] alone, it is skipped entirely (no PUT sent).

# Once an issue is confirmed correct (either found already-correct, or fixed
# by a PUT), it's recorded in known_correct_versions.json next to this
# script. On every subsequent run, that file is checked FIRST — a confirmed
# issue is skipped with ZERO API calls and is never touched again, unless the
# expected version for it changes (e.g. the Excel mapping is updated).

# Runs with a ThreadPoolExecutor (10 workers).

# Usage:
#     python fix_issue_versions.py                 # run for real
#     python fix_issue_versions.py --dry-run        # show what would change, no writes
#     python fix_issue_versions.py --limit 50        # only process first 50 matching issues

# Inputs (edit the paths below if yours differ):
#     ISSUES_META_PATH     .../FS_data/payload/issues_meta.json
#     ID_MAPPING_PATH       .../FS_data/mapping/id_mapping.json
#     VERSIONS_XLSX_PATH   .../FS_data/mapping/versions_mapping.xlsx

# Output (all written next to this script):
#     fix_versions_report.json           summary + timing
#     fix_versions_failed.json           anything that failed, with Jira's real error
#     known_correct_versions.json        persistent "don't touch again" memory
#     duplicate_version_id_warnings.json only written if the Excel has release ids
#                                         whose EX/PLAT/CEMIG rows share one id
# """

# import os
# import sys
# import json
# import time
# import re
# import argparse
# import concurrent.futures
# from threading import Lock

# import requests
# import pandas as pd
# from dotenv import load_dotenv

# # jira_credentials.py gives per-thread service-account pinning, same as the
# # main migration script. Falls back to single JIRA_EMAIL/JIRA_API_TOKEN if
# # that module isn't importable from wherever this script is run.
# try:
#     import jira_credentials
#     _HAVE_CREDENTIALS_MODULE = True
# except Exception:
#     _HAVE_CREDENTIALS_MODULE = False

# sys.stdout.reconfigure(encoding="utf-8")
# sys.stderr.reconfigure(encoding="utf-8")

# load_dotenv()

# # =============================================================================
# # CONFIG — edit these paths if your layout differs
# # =============================================================================

# BASE_FS_DATA = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data"

# ISSUES_META_PATH   = os.path.join(BASE_FS_DATA, "payload", "issues_meta.json")
# ID_MAPPING_PATH     = os.path.join(BASE_FS_DATA, "mapping", "id_mapping.json")
# VERSIONS_XLSX_PATH = os.path.join(BASE_FS_DATA, "mapping", "versions_mapping.xlsx")

# JIRA_URL       = os.getenv("JIRA_URL")
# JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
# JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

# JIRA_HEADERS = {
#     "Accept":       "application/json",
#     "Content-Type": "application/json",
# }

# MAX_WORKERS   = int(os.getenv("VERSION_FIX_MAX_WORKERS", "10"))
# SINGLE_TIMEOUT = 60
# RETRY_DELAY    = 5
# MAX_RETRIES    = 3
# RATE_LIMIT_DELAY = 0.3

# REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_versions_report.json")
# FAILED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_versions_failed.json")

# # Persistent "already confirmed correct — never touch again" memory.
# # Keyed by jira_key -> the version_id we confirmed is correctly (and
# # solely) set on it. On every run, before touching an issue at all, we
# # check this file: if the expected version_id for that issue still
# # matches what's recorded here, we skip it with ZERO API calls (no GET,
# # no PUT). This is what makes re-runs over the same 39k issues cheap and
# # guarantees a confirmed-correct issue is never re-touched.
# KNOWN_CORRECT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_correct_versions.json")
# KNOWN_CORRECT_SAVE_EVERY = 50

# print_lock  = Lock()
# counts_lock = Lock()
# counts = {
#     "already_correct_cached": 0,   # skipped via known_correct.json, zero API calls
#     "already_correct_live":   0,   # GET showed it was already exactly right
#     "reassigned":              0,  # correct version added (none of it was set before)
#     "cleaned":                 0,  # correct version was already present alongside
#                                     # wrong one(s) -- only the wrong one(s) removed
#     "skipped_no_version":      0,  # source_release_id empty/nan
#     "failed":                  0,
# }
# failed_items = []
# failed_lock = Lock()

# known_correct = {}
# known_correct_lock = Lock()
# _known_correct_dirty_count = 0


# def load_known_correct():
#     global known_correct
#     if os.path.exists(KNOWN_CORRECT_PATH):
#         try:
#             with open(KNOWN_CORRECT_PATH, "r", encoding="utf-8") as f:
#                 known_correct = json.load(f)
#         except (json.JSONDecodeError, OSError):
#             known_correct = {}
#     return known_correct


# def save_known_correct(force=False):
#     global _known_correct_dirty_count
#     with known_correct_lock:
#         _known_correct_dirty_count += 1
#         if not force and _known_correct_dirty_count % KNOWN_CORRECT_SAVE_EVERY != 0:
#             return
#         with open(KNOWN_CORRECT_PATH, "w", encoding="utf-8") as f:
#             json.dump(known_correct, f, indent=2)


# def mark_known_correct(jira_key, version_id):
#     with known_correct_lock:
#         known_correct[jira_key] = version_id
#     save_known_correct()


# def log(msg):
#     with print_lock:
#         print(msg)


# def bump(key):
#     with counts_lock:
#         counts[key] += 1


# def record_failure(source_issue_id, jira_key, reason):
#     with failed_lock:
#         failed_items.append({
#             "source_issue_id": source_issue_id,
#             "jira_key": jira_key,
#             "reason": reason,
#         })
#     bump("failed")


# # =============================================================================
# # AUTH / HTTP
# # =============================================================================

# def _get_auth():
#     if _HAVE_CREDENTIALS_MODULE:
#         try:
#             return jira_credentials.get_auth_for_this_thread()
#         except Exception:
#             pass
#     return (JIRA_EMAIL, JIRA_API_TOKEN)


# def jira_request(method, endpoint, payload=None, retries=MAX_RETRIES):
#     url = f"{JIRA_URL}/rest/{endpoint.lstrip('/')}"
#     auth = _get_auth()

#     for attempt in range(retries):
#         try:
#             if method == "GET":
#                 resp = requests.get(url, headers=JIRA_HEADERS, auth=auth, timeout=SINGLE_TIMEOUT)
#             elif method == "PUT":
#                 resp = requests.put(url, json=payload, headers=JIRA_HEADERS, auth=auth, timeout=SINGLE_TIMEOUT)
#             else:
#                 raise ValueError(f"Unsupported method: {method}")

#             if resp.status_code == 429:
#                 retry_after = int(resp.headers.get("Retry-After", RETRY_DELAY))
#                 log(f"  [RATE LIMIT] 429 on {method} {endpoint} — sleeping {retry_after}s")
#                 time.sleep(retry_after)
#                 continue

#             if resp.status_code in (500, 502, 503) and attempt < retries - 1:
#                 time.sleep(RETRY_DELAY * (attempt + 1))
#                 continue

#             time.sleep(RATE_LIMIT_DELAY)
#             return (resp.json() if resp.content else {}), resp.status_code

#         except requests.exceptions.Timeout:
#             if attempt < retries - 1:
#                 time.sleep(RETRY_DELAY)
#                 continue
#             return None, 0
#         except requests.exceptions.ConnectionError:
#             time.sleep(RETRY_DELAY)
#         except Exception as e:
#             return {"_exception": str(e)}, 0

#     return None, 0


# def jira_get(endpoint):
#     return jira_request("GET", endpoint)


# def jira_put(endpoint, payload):
#     return jira_request("PUT", endpoint, payload)


# # =============================================================================
# # LOAD INPUTS
# # =============================================================================

# def norm_release_id(value):
#     """
#     Normalize a release/version-source id so "42296.0" (issues_meta.json,
#     often float-stringified) and 42296 (Excel int) compare equal.
#     Returns None for blank/NaN-like values.
#     """
#     if value is None:
#         return None
#     s = str(value).strip()
#     if s in ("", "nan", "None", "NaT"):
#         return None
#     try:
#         return str(int(float(s)))
#     except (ValueError, TypeError):
#         return s


# def load_issues_meta(path):
#     with open(path, "r", encoding="utf-8") as f:
#         data = json.load(f)
#     # Support either a bare list, or a dict with an "issues"/"data" wrapper.
#     if isinstance(data, dict):
#         for key in ("issues", "data", "items"):
#             if key in data and isinstance(data[key], list):
#                 return data[key]
#         raise ValueError(f"Could not find a list of issues inside {path}")
#     return data


# def load_id_mapping(path):
#     with open(path, "r", encoding="utf-8") as f:
#         data = json.load(f)
#     # Matches the migration script's id_mapping.json shape:
#     # { "issues": { "<source_issue_id>": {"jira_key": ..., ...} }, ... }
#     if "issues" in data:
#         return data["issues"]
#     return data  # already flat {source_id: {...}}


# def load_versions_excel(path):
#     """
#     Returns a tuple:
#       by_key:        { (norm_release_id, PROJECT_UPPER): {"version_id", "version_name"} }
#                       — the exact lookup used for assignment.
#       by_release_id: { norm_release_id: [row, ...] }
#                       — kept only so we can flag suspicious duplicate ids
#                       across projects for the same release (see
#                       find_duplicate_id_warnings below); not used to pick
#                       the version that actually gets assigned.
#     """
#     df = pd.read_excel(path, dtype=str)
#     df.columns = [c.strip() for c in df.columns]

#     col_map = {c.lower(): c for c in df.columns}
#     name_col    = col_map.get("version name")
#     release_col = col_map.get("source release id")
#     project_col = col_map.get("project")
#     verid_col   = col_map.get("jira version id")

#     missing = [n for n, c in [
#         ("Version Name", name_col), ("Source Release ID", release_col),
#         ("Project", project_col), ("Jira Version ID", verid_col),
#     ] if c is None]
#     if missing:
#         raise ValueError(f"versions_mapping.xlsx missing expected column(s): {missing}")

#     by_key = {}
#     by_release_id = {}
#     for _, row in df.iterrows():
#         rid = norm_release_id(row.get(release_col))
#         if rid is None:
#             continue
#         project = str(row.get(project_col)).strip() if pd.notna(row.get(project_col)) else ""
#         entry = {
#             "version_id":   str(row.get(verid_col)).strip(),
#             "version_name": str(row.get(name_col)).strip() if pd.notna(row.get(name_col)) else "",
#             "project":      project,
#         }
#         if project:
#             by_key[(rid, project.upper())] = entry
#         by_release_id.setdefault(rid, []).append(entry)

#     return by_key, by_release_id


# def project_from_jira_key(jira_key):
#     """ "EX-202958" -> "EX"  (strip the trailing "-<number>"). """
#     if not jira_key:
#         return None
#     m = re.match(r"^(.*)-\d+$", jira_key.strip())
#     return m.group(1) if m else None


# def find_duplicate_id_warnings(by_release_id):
#     """
#     Flags any release id whose rows for different projects all carry the
#     SAME Jira Version ID — that's only legitimate if it's genuine
#     coincidence, but in practice usually means the Excel export reused one
#     id across projects instead of each project's real, distinct id.
#     Informational only — does not block the run.
#     """
#     warnings = []
#     for rid, rows in by_release_id.items():
#         ids = {r["version_id"] for r in rows}
#         projects = {r["project"] for r in rows}
#         if len(rows) > 1 and len(ids) == 1 and len(projects) > 1:
#             warnings.append({
#                 "source_release_id": rid,
#                 "projects": sorted(projects),
#                 "shared_jira_version_id": next(iter(ids)),
#             })
#     return warnings


# def resolve_version(by_key, release_id, jira_key):
#     """
#     Strict (release_id, project) lookup — project is derived from the
#     issue's own jira_key, never guessed. Returns (version_id, version_name)
#     or (None, None) if that exact project has no row for this release id.
#     """
#     project = project_from_jira_key(jira_key)
#     if not project:
#         return None, None
#     entry = by_key.get((release_id, project.upper()))
#     if not entry:
#         return None, None
#     return entry["version_id"], entry["version_name"]


# # =============================================================================
# # PER-ISSUE WORK
# # =============================================================================

# def process_issue(item, by_key, id_mapping_issues, dry_run):
#     source_issue_id = str(item.get("source_issue_id", "")).strip()
#     release_id = norm_release_id(item.get("source_release_id"))

#     if not release_id:
#         bump("skipped_no_version")
#         return

#     issue_info = id_mapping_issues.get(source_issue_id)
#     if not issue_info or not issue_info.get("jira_key"):
#         record_failure(source_issue_id, None, "no jira_key in id_mapping for this source_issue_id")
#         log(f"  ✖ {source_issue_id}: no jira_key found in id_mapping")
#         return

#     jira_key = issue_info["jira_key"]

#     # Strict (release_id, project) lookup — project comes from the issue's
#     # own jira_key (e.g. "EX-202958" -> "EX"), never guessed or defaulted
#     # to "whichever row came first".
#     version_id, version_name = resolve_version(by_key, release_id, jira_key)
#     if not version_id:
#         project = project_from_jira_key(jira_key)
#         record_failure(source_issue_id, jira_key,
#                         f"no versions_mapping.xlsx row for (release_id={release_id}, project={project})")
#         log(f"  ✖ {jira_key} (src {source_issue_id}): no row for "
#             f"release_id={release_id} + project={project}")
#         return

#     # ── "never touch it again" check — zero API calls if we've already
#     #    confirmed, in a prior run, that this issue's ONLY fixVersion is
#     #    exactly the correct one. ─────────────────────────────────────────
#     if known_correct.get(jira_key) == version_id:
#         bump("already_correct_cached")
#         log(f"  = {jira_key} (src {source_issue_id}): confirmed correct in a prior run — skipped, untouched")
#         return

#     # Fetch current fixVersions so we know exactly what to remove/keep.
#     data, status = jira_get(f"api/3/issue/{jira_key}?fields=fixVersions")
#     if status != 200 or data is None:
#         record_failure(source_issue_id, jira_key, f"GET issue failed (HTTP {status}): {data}")
#         log(f"  ✖ {jira_key}: could not fetch current fixVersions (HTTP {status})")
#         return

#     current_versions = data.get("fields", {}).get("fixVersions", []) or []
#     current_ids = [v.get("id") for v in current_versions if v.get("id")]
#     current_names = {v.get("id"): v.get("name") for v in current_versions}

#     if current_ids == [version_id]:
#         # Already exactly right — nothing to remove, nothing to add.
#         bump("already_correct_live")
#         mark_known_correct(jira_key, version_id)
#         log(f"  = {jira_key} (src {source_issue_id}): already correctly set to "
#             f"'{version_name}' — skipped, untouched")
#         return

#     # Only the WRONG version(s) get removed. If the correct one is already
#     # among current_ids, it is never included in the remove list and no
#     # "add" op is sent for it — it's simply left alone.
#     wrong_ids = [vid for vid in current_ids if vid != version_id]
#     correct_already_present = version_id in current_ids

#     update_ops = [{"remove": {"id": vid}} for vid in wrong_ids]
#     if not correct_already_present:
#         update_ops.append({"add": {"id": version_id}})

#     removed_desc = [f"{current_names.get(vid)} ({vid})" for vid in wrong_ids]

#     if dry_run:
#         if correct_already_present:
#             log(f"  ~ [DRY RUN] {jira_key} (src {source_issue_id}): would KEEP '{version_name}' "
#                 f"({version_id}) and remove wrong version(s): {removed_desc}")
#         else:
#             log(f"  ~ [DRY RUN] {jira_key} (src {source_issue_id}): would remove {removed_desc or 'none'} "
#                 f"→ add '{version_name}' ({version_id})")
#         bump("cleaned" if correct_already_present else "reassigned")
#         return

#     payload = {"update": {"fixVersions": update_ops}}
#     put_body, put_status = jira_put(f"api/3/issue/{jira_key}", payload)

#     if put_status == 204:
#         mark_known_correct(jira_key, version_id)
#         if correct_already_present:
#             bump("cleaned")
#             log(f"  ✔ {jira_key} (src {source_issue_id}): kept correct '{version_name}' "
#                 f"({version_id}), removed wrong version(s): {removed_desc}")
#         else:
#             bump("reassigned")
#             log(f"  ✔ {jira_key} (src {source_issue_id}): removed {removed_desc or 'none'} "
#                 f"→ added '{version_name}' ({version_id})")
#     else:
#         # Capture Jira's actual error body, not just the status code, so
#         # failed items are diagnosable without re-running debug_single_issue.py.
#         err_detail = None
#         if isinstance(put_body, dict):
#             err_detail = put_body.get("errors") or put_body.get("errorMessages") or put_body
#         record_failure(source_issue_id, jira_key, f"PUT failed (HTTP {put_status}): {err_detail}")
#         log(f"  ✖ {jira_key} (src {source_issue_id}): PUT failed (HTTP {put_status}) — {err_detail}")


# # =============================================================================
# # MAIN
# # =============================================================================

# def main():
#     parser = argparse.ArgumentParser(description="Reassign correct fixVersions on Jira issues.")
#     parser.add_argument("--dry-run", action="store_true", help="Don't call PUT, just show what would happen.")
#     parser.add_argument("--limit", type=int, default=None, help="Only process the first N matching issues.")
#     parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="Thread pool size (default 10).")
#     args = parser.parse_args()

#     if not JIRA_URL:
#         print("✖ JIRA_URL not set (check your .env). Aborting.")
#         return 1

#     print("=" * 70)
#     print("  FIX ISSUE VERSIONS")
#     print("=" * 70)
#     print(f"  issues_meta.json     : {ISSUES_META_PATH}")
#     print(f"  id_mapping.json       : {ID_MAPPING_PATH}")
#     print(f"  versions_mapping.xlsx : {VERSIONS_XLSX_PATH}")
#     print(f"  dry run              : {args.dry_run}")
#     print(f"  workers               : {args.workers}")
#     print()

#     issues = load_issues_meta(ISSUES_META_PATH)
#     id_mapping_issues = load_id_mapping(ID_MAPPING_PATH)
#     by_key, by_release_id = load_versions_excel(VERSIONS_XLSX_PATH)
#     load_known_correct()
#     print(f"  known_correct_versions.json : {len(known_correct)} issue(s) pre-confirmed from prior runs "
#           f"(will be skipped with zero API calls)\n")

#     dup_warnings = find_duplicate_id_warnings(by_release_id)
#     if dup_warnings:
#         warn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "duplicate_version_id_warnings.json")
#         with open(warn_path, "w", encoding="utf-8") as f:
#             json.dump(dup_warnings, f, indent=2)
#         print(f"  ⚠ {len(dup_warnings)} release id(s) have the SAME Jira Version ID across "
#               f"multiple projects in versions_mapping.xlsx (details → {warn_path}).")
#         print(f"    This run still assigns strictly per (release_id, project), so it's safe "
#               f"either way — but worth checking whether the Excel export is stale.\n")

#     # Only issues that actually have a version value.
#     work_items = [it for it in issues if norm_release_id(it.get("source_release_id"))]
#     if args.limit:
#         work_items = work_items[:args.limit]

#     print(f"  {len(issues)} total issues in issues_meta.json")
#     print(f"  {len(work_items)} issue(s) have a source_release_id (version) to process\n")

#     if not work_items:
#         print("Nothing to do.")
#         return 0

#     start = time.time()
#     workers = min(args.workers, max(len(work_items), 1))
#     with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
#         futures = [
#             executor.submit(process_issue, item, by_key, id_mapping_issues, args.dry_run)
#             for item in work_items
#         ]
#         for future in concurrent.futures.as_completed(futures):
#             try:
#                 future.result()
#             except Exception as e:
#                 log(f"   ✖ Worker exception: {e}")

#     elapsed = time.time() - start

#     # Force-flush the known-correct cache — the periodic save only writes
#     # every KNOWN_CORRECT_SAVE_EVERY confirmations, so anything since the
#     # last flush needs a final write here.
#     if not args.dry_run:
#         save_known_correct(force=True)

#     print("\n" + "=" * 70)
#     print("  SUMMARY")
#     print("=" * 70)
#     print(f"  Reassigned (no prior version)        : {counts['reassigned']}")
#     print(f"  Cleaned (kept correct, removed wrong) : {counts['cleaned']}")
#     print(f"  Already correct (checked live)        : {counts['already_correct_live']}")
#     print(f"  Already correct (cached, untouched)   : {counts['already_correct_cached']}")
#     print(f"  Skipped (no version value)            : {counts['skipped_no_version']}")
#     print(f"  Failed                                : {counts['failed']}")
#     print(f"  Time elapsed                          : {elapsed:.2f}s")
#     print("=" * 70)

#     report = {
#         "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
#         "dry_run": args.dry_run,
#         "counts": counts,
#         "elapsed_seconds": elapsed,
#     }
#     with open(REPORT_PATH, "w", encoding="utf-8") as f:
#         json.dump(report, f, indent=2)

#     if failed_items:
#         with open(FAILED_PATH, "w", encoding="utf-8") as f:
#             json.dump(failed_items, f, indent=2)
#         print(f"\n  ✔ {len(failed_items)} failed item(s) written → {FAILED_PATH}")

#     print(f"  ✔ Report written → {REPORT_PATH}")

#     return 0 if counts["failed"] == 0 else 1


# if __name__ == "__main__":
#     sys.exit(main())
"""
fix_issue_versions.py

Standalone script — goes through every issue in issues_meta.json that has a
non-empty `source_release_id` ("has a value for version"), resolves it to a
Jira issue key via id_mapping.json, resolves the correct Jira version id via
versions_mapping.xlsx (strict lookup by (release_id, project) — project is
derived from the issue's own jira_key, e.g. "EX-202958" -> "EX"), and
reassigns fixVersions on the live issue.

If an issue already has MORE THAN ONE fixVersion set, only the WRONG one(s)
are removed — the correct one, if already present, is left exactly as is
and never re-added. If the issue's fixVersions are already exactly
[correct_id] alone, it is skipped entirely (no PUT sent).

Once an issue is confirmed correct (either found already-correct, or fixed
by a PUT), it's recorded in known_correct_versions.json next to this
script. On every subsequent run, that file is checked FIRST — a confirmed
issue is skipped with ZERO API calls and is never touched again, unless the
expected version for it changes (e.g. the Excel mapping is updated).

Runs with a ThreadPoolExecutor (10 workers).

Usage:
    python fix_issue_versions.py                 # run for real
    python fix_issue_versions.py --dry-run        # show what would change, no writes
    python fix_issue_versions.py --limit 50        # only process first 50 matching issues

Inputs (edit the paths below if yours differ):
    ISSUES_META_PATH     .../FS_data/payload/issues_meta.json
    ID_MAPPING_PATH       .../FS_data/mapping/id_mapping.json
    VERSIONS_XLSX_PATH   .../FS_data/mapping/versions_mapping.xlsx

Output (all written next to this script):
    fix_versions_report.json           summary + timing
    fix_versions_failed.json           anything that failed, with Jira's real error
    known_correct_versions.json        persistent "don't touch again" memory
    duplicate_version_id_warnings.json only written if the Excel has release ids
                                        whose EX/PLAT/CEMIG rows share one id
"""

import os
import sys
import json
import time
import re
import argparse
import concurrent.futures
from threading import Lock

import requests
import pandas as pd
from dotenv import load_dotenv

# jira_credentials.py gives per-thread service-account pinning, same as the
# main migration script. Falls back to single JIRA_EMAIL/JIRA_API_TOKEN if
# that module isn't importable from wherever this script is run.
try:
    import jira_credentials
    _HAVE_CREDENTIALS_MODULE = True
except Exception:
    _HAVE_CREDENTIALS_MODULE = False

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

# =============================================================================
# CONFIG — edit these paths if your layout differs
# =============================================================================

BASE_FS_DATA = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data"

ISSUES_META_PATH   = os.path.join(BASE_FS_DATA, "payload", "issues_meta.json")
ID_MAPPING_PATH     = os.path.join(BASE_FS_DATA, "mapping", "id_mapping.json")
VERSIONS_XLSX_PATH = os.path.join(BASE_FS_DATA, "mapping", "versions_mapping.xlsx")

JIRA_URL       = os.getenv("JIRA_URL")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

JIRA_HEADERS = {
    "Accept":       "application/json",
    "Content-Type": "application/json",
}

MAX_WORKERS   = int(os.getenv("VERSION_FIX_MAX_WORKERS", "10"))
SINGLE_TIMEOUT = 60
RETRY_DELAY    = 5
MAX_RETRIES    = 3
RATE_LIMIT_DELAY = 0.3

REPORT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_versions_report.json")
FAILED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_versions_failed.json")

# Persistent "already confirmed correct — never touch again" memory.
# Keyed by jira_key -> the version_id we confirmed is correctly (and
# solely) set on it. On every run, before touching an issue at all, we
# check this file: if the expected version_id for that issue still
# matches what's recorded here, we skip it with ZERO API calls (no GET,
# no PUT). This is what makes re-runs over the same 39k issues cheap and
# guarantees a confirmed-correct issue is never re-touched.
KNOWN_CORRECT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_correct_versions.json")
KNOWN_CORRECT_SAVE_EVERY = 50

print_lock  = Lock()
counts_lock = Lock()
counts = {
    "already_correct_cached": 0,   # skipped via known_correct.json, zero API calls
    "already_correct_live":   0,   # GET showed it was already exactly right
    "reassigned":              0,  # correct version added (none of it was set before)
    "cleaned":                 0,  # correct version was already present alongside
                                    # wrong one(s) -- only the wrong one(s) removed
    "skipped_no_version":      0,  # source_release_id empty/nan
    "failed":                  0,
}
failed_items = []
failed_lock = Lock()

known_correct = {}
known_correct_lock = Lock()
_known_correct_dirty_count = 0


def load_known_correct():
    global known_correct
    if os.path.exists(KNOWN_CORRECT_PATH):
        try:
            with open(KNOWN_CORRECT_PATH, "r", encoding="utf-8") as f:
                known_correct = json.load(f)
        except (json.JSONDecodeError, OSError):
            known_correct = {}
    return known_correct


def save_known_correct(force=False):
    global _known_correct_dirty_count
    with known_correct_lock:
        _known_correct_dirty_count += 1
        if not force and _known_correct_dirty_count % KNOWN_CORRECT_SAVE_EVERY != 0:
            return
        with open(KNOWN_CORRECT_PATH, "w", encoding="utf-8") as f:
            json.dump(known_correct, f, indent=2)


def mark_known_correct(jira_key, version_id):
    with known_correct_lock:
        known_correct[jira_key] = version_id
    save_known_correct()


def log(msg):
    with print_lock:
        print(msg)


def bump(key):
    with counts_lock:
        counts[key] += 1


def record_failure(source_issue_id, jira_key, reason):
    with failed_lock:
        failed_items.append({
            "source_issue_id": source_issue_id,
            "jira_key": jira_key,
            "reason": reason,
        })
    bump("failed")


# =============================================================================
# AUTH / HTTP
# =============================================================================

def _get_auth():
    if _HAVE_CREDENTIALS_MODULE:
        try:
            return jira_credentials.get_auth_for_this_thread()
        except Exception:
            pass
    return (JIRA_EMAIL, JIRA_API_TOKEN)


def jira_request(method, endpoint, payload=None, retries=MAX_RETRIES):
    url = f"{JIRA_URL}/rest/{endpoint.lstrip('/')}"
    auth = _get_auth()

    for attempt in range(retries):
        try:
            if method == "GET":
                resp = requests.get(url, headers=JIRA_HEADERS, auth=auth, timeout=SINGLE_TIMEOUT)
            elif method == "PUT":
                resp = requests.put(url, json=payload, headers=JIRA_HEADERS, auth=auth, timeout=SINGLE_TIMEOUT)
            else:
                raise ValueError(f"Unsupported method: {method}")

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", RETRY_DELAY))
                log(f"  [RATE LIMIT] 429 on {method} {endpoint} — sleeping {retry_after}s")
                time.sleep(retry_after)
                continue

            if resp.status_code in (500, 502, 503) and attempt < retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue

            time.sleep(RATE_LIMIT_DELAY)
            return (resp.json() if resp.content else {}), resp.status_code

        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY)
                continue
            return None, 0
        except requests.exceptions.ConnectionError:
            time.sleep(RETRY_DELAY)
        except Exception as e:
            return {"_exception": str(e)}, 0

    return None, 0


def jira_get(endpoint):
    return jira_request("GET", endpoint)


def jira_put(endpoint, payload):
    return jira_request("PUT", endpoint, payload)


# =============================================================================
# LOAD INPUTS
# =============================================================================

def norm_release_id(value):
    """
    Normalize a release/version-source id so "42296.0" (issues_meta.json,
    often float-stringified) and 42296 (Excel int) compare equal.
    Returns None for blank/NaN-like values.
    """
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "nan", "None", "NaT"):
        return None
    try:
        return str(int(float(s)))
    except (ValueError, TypeError):
        return s


def load_issues_meta(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Support either a bare list, or a dict with an "issues"/"data" wrapper.
    if isinstance(data, dict):
        for key in ("issues", "data", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
        raise ValueError(f"Could not find a list of issues inside {path}")
    return data


def load_id_mapping(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Matches the migration script's id_mapping.json shape:
    # { "issues": { "<source_issue_id>": {"jira_key": ..., ...} }, ... }
    if "issues" in data:
        return data["issues"]
    return data  # already flat {source_id: {...}}


def load_versions_excel(path):
    """
    Returns a tuple:
      by_key:        { (norm_release_id, PROJECT_UPPER): {"version_id", "version_name"} }
                      — the exact lookup used for assignment.
      by_release_id: { norm_release_id: [row, ...] }
                      — kept only so we can flag suspicious duplicate ids
                      across projects for the same release (see
                      find_duplicate_id_warnings below); not used to pick
                      the version that actually gets assigned.
    """
    df = pd.read_excel(path, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    col_map = {c.lower(): c for c in df.columns}
    name_col    = col_map.get("version name")
    release_col = col_map.get("source release id")
    project_col = col_map.get("project")
    verid_col   = col_map.get("jira version id")

    missing = [n for n, c in [
        ("Version Name", name_col), ("Source Release ID", release_col),
        ("Project", project_col), ("Jira Version ID", verid_col),
    ] if c is None]
    if missing:
        raise ValueError(f"versions_mapping.xlsx missing expected column(s): {missing}")

    by_key = {}
    by_release_id = {}
    for _, row in df.iterrows():
        rid = norm_release_id(row.get(release_col))
        if rid is None:
            continue
        project = str(row.get(project_col)).strip() if pd.notna(row.get(project_col)) else ""
        entry = {
            "version_id":   str(row.get(verid_col)).strip(),
            "version_name": str(row.get(name_col)).strip() if pd.notna(row.get(name_col)) else "",
            "project":      project,
        }
        if project:
            by_key[(rid, project.upper())] = entry
        by_release_id.setdefault(rid, []).append(entry)

    return by_key, by_release_id


def project_from_jira_key(jira_key):
    """ "EX-202958" -> "EX"  (strip the trailing "-<number>"). """
    if not jira_key:
        return None
    m = re.match(r"^(.*)-\d+$", jira_key.strip())
    return m.group(1) if m else None


def find_duplicate_id_warnings(by_release_id):
    """
    Flags any release id whose rows for different projects all carry the
    SAME Jira Version ID — that's only legitimate if it's genuine
    coincidence, but in practice usually means the Excel export reused one
    id across projects instead of each project's real, distinct id.
    Informational only — does not block the run.
    """
    warnings = []
    for rid, rows in by_release_id.items():
        ids = {r["version_id"] for r in rows}
        projects = {r["project"] for r in rows}
        if len(rows) > 1 and len(ids) == 1 and len(projects) > 1:
            warnings.append({
                "source_release_id": rid,
                "projects": sorted(projects),
                "shared_jira_version_id": next(iter(ids)),
            })
    return warnings


def resolve_version(by_key, release_id, jira_key):
    """
    Strict (release_id, project) lookup — project is derived from the
    issue's own jira_key, never guessed. Returns (excel_version_id,
    version_name, project) or (None, None, None) if that exact project has
    no row for this release id.
    """
    project = project_from_jira_key(jira_key)
    if not project:
        return None, None, None
    entry = by_key.get((release_id, project.upper()))
    if not entry:
        return None, None, None
    return entry["version_id"], entry["version_name"], project


# =============================================================================
# LIVE VERSION VALIDATION / FALLBACK
# =============================================================================
# The Excel's "Jira Version ID" has repeatedly turned out to be stale for
# some (release_id, project) rows — same numeric id copy-pasted across
# EX/PLAT/CEMIG when in fact each project's version is a distinct Jira
# object. Sending a stale id straight to PUT just burns a 400
# ("Specify a valid value for fixVersions"). So: before ever using an
# Excel-resolved id, confirm it actually exists in that project's live
# version list. If it doesn't, try to recover by matching the version NAME
# instead (still scoped to that one project). Only fail if neither works.

_project_versions_cache = {}
_project_versions_lock = Lock()


def get_live_project_versions(project_key):
    """
    Returns (id_to_name, name_to_id) for a project, fetched once and cached.
    id_to_name: {"22942": "08_09_2026", ...}
    name_to_id: {"06_12_2022_esm_release": "19180", ...}  (lowercased keys)
    """
    with _project_versions_lock:
        cached = _project_versions_cache.get(project_key)
    if cached is not None:
        return cached

    data, status = jira_get(f"api/3/project/{project_key}/versions")
    id_to_name = {}
    name_to_id = {}
    if status == 200 and isinstance(data, list):
        for v in data:
            vid = v.get("id")
            vname_raw = v.get("name") or ""
            vname = vname_raw.strip().lower()
            if vid:
                id_to_name[vid] = vname_raw
            if vname:
                name_to_id[vname] = vid

    result = (id_to_name, name_to_id)
    with _project_versions_lock:
        _project_versions_cache[project_key] = result
    return result


def resolve_and_validate_version(by_key, release_id, jira_key):
    """
    Full resolution pipeline for one issue:
      1. Strict (release_id, project) lookup in the Excel.
      2. Validate that id EXISTS *and* its LIVE NAME matches the Excel's
         claimed version name. Existence alone is not enough — the Excel
         has been found to point a valid id at the WRONG version (id
         22942 exists in EX, but is actually named "08_09_2026" live, not
         "04_09_2026" as the Excel claims for that row). Trusting
         existence alone would silently reassign a correctly-set issue to
         the wrong version.
      3. If the id doesn't exist, or exists under a different name, fall
         back to a live NAME match in that same project instead.
      4. If still nothing, give up with a clear reason.

    Returns (version_id, version_name, error_reason). Exactly one of
    (version_id) or (error_reason) will be set.
    """
    excel_id, version_name, project = resolve_version(by_key, release_id, jira_key)
    if not excel_id:
        p = project_from_jira_key(jira_key)
        return None, None, f"no versions_mapping.xlsx row for (release_id={release_id}, project={p})"

    id_to_name, name_to_id = get_live_project_versions(project)
    expected_name_norm = version_name.strip().lower() if version_name else ""

    live_name_of_excel_id = id_to_name.get(excel_id)
    if live_name_of_excel_id is not None and live_name_of_excel_id.strip().lower() == expected_name_norm:
        # id exists AND its live name matches what the Excel claims — trust it.
        return excel_id, version_name, None

    # Excel id is either missing live, or exists but is actually a
    # DIFFERENT version than claimed — don't trust it either way. Recover
    # by looking up the expected NAME directly.
    live_id_by_name = name_to_id.get(expected_name_norm) if expected_name_norm else None
    if live_id_by_name:
        return live_id_by_name, version_name, None

    if live_name_of_excel_id is not None:
        return None, None, (
            f"Excel id {excel_id} exists live in project {project} but is actually named "
            f"'{live_name_of_excel_id}', not '{version_name}' as the Excel claims — and no "
            f"live version named '{version_name}' exists there either (Excel row is wrong)"
        )
    return None, None, (
        f"Excel id {excel_id} for version '{version_name}' does not exist live in "
        f"project {project}, and no live version named '{version_name}' was found "
        f"there either (Excel is stale for this row)"
    )


# =============================================================================
# PER-ISSUE WORK
# =============================================================================

def process_issue(item, by_key, id_mapping_issues, dry_run):
    source_issue_id = str(item.get("source_issue_id", "")).strip()
    release_id = norm_release_id(item.get("source_release_id"))

    if not release_id:
        bump("skipped_no_version")
        return

    issue_info = id_mapping_issues.get(source_issue_id)
    if not issue_info or not issue_info.get("jira_key"):
        record_failure(source_issue_id, None, "no jira_key in id_mapping for this source_issue_id")
        log(f"  ✖ {source_issue_id}: no jira_key found in id_mapping")
        return

    jira_key = issue_info["jira_key"]

    # Strict (release_id, project) lookup validated against Jira's LIVE
    # version list for that project — falls back to a name match if the
    # Excel's id is stale, and fails loudly (no PUT attempted) if neither
    # resolves, instead of burning a 400 on an invalid id.
    version_id, version_name, resolve_error = resolve_and_validate_version(by_key, release_id, jira_key)
    if not version_id:
        record_failure(source_issue_id, jira_key, resolve_error)
        log(f"  ✖ {jira_key} (src {source_issue_id}): {resolve_error}")
        return

    # ── "never touch it again" check — zero API calls if we've already
    #    confirmed, in a prior run, that this issue's ONLY fixVersion is
    #    exactly the correct one. ─────────────────────────────────────────
    if known_correct.get(jira_key) == version_id:
        bump("already_correct_cached")
        log(f"  = {jira_key} (src {source_issue_id}): confirmed correct in a prior run — skipped, untouched")
        return

    # Fetch current fixVersions so we know exactly what to remove/keep.
    data, status = jira_get(f"api/3/issue/{jira_key}?fields=fixVersions")
    if status != 200 or data is None:
        record_failure(source_issue_id, jira_key, f"GET issue failed (HTTP {status}): {data}")
        log(f"  ✖ {jira_key}: could not fetch current fixVersions (HTTP {status})")
        return

    current_versions = data.get("fields", {}).get("fixVersions", []) or []
    current_ids = [v.get("id") for v in current_versions if v.get("id")]
    current_names = {v.get("id"): v.get("name") for v in current_versions}

    if current_ids == [version_id]:
        # Already exactly right — nothing to remove, nothing to add.
        bump("already_correct_live")
        mark_known_correct(jira_key, version_id)
        log(f"  = {jira_key} (src {source_issue_id}): already correctly set to "
            f"'{version_name}' — skipped, untouched")
        return

    # Only the WRONG version(s) get removed. If the correct one is already
    # among current_ids, it is never included in the remove list and no
    # "add" op is sent for it — it's simply left alone.
    wrong_ids = [vid for vid in current_ids if vid != version_id]
    correct_already_present = version_id in current_ids

    update_ops = [{"remove": {"id": vid}} for vid in wrong_ids]
    if not correct_already_present:
        update_ops.append({"add": {"id": version_id}})

    removed_desc = [f"{current_names.get(vid)} ({vid})" for vid in wrong_ids]

    if dry_run:
        if correct_already_present:
            log(f"  ~ [DRY RUN] {jira_key} (src {source_issue_id}): would KEEP '{version_name}' "
                f"({version_id}) and remove wrong version(s): {removed_desc}")
        else:
            log(f"  ~ [DRY RUN] {jira_key} (src {source_issue_id}): would remove {removed_desc or 'none'} "
                f"→ add '{version_name}' ({version_id})")
        bump("cleaned" if correct_already_present else "reassigned")
        return

    payload = {"update": {"fixVersions": update_ops}}
    put_body, put_status = jira_put(f"api/3/issue/{jira_key}", payload)

    if put_status == 204:
        mark_known_correct(jira_key, version_id)
        if correct_already_present:
            bump("cleaned")
            log(f"  ✔ {jira_key} (src {source_issue_id}): kept correct '{version_name}' "
                f"({version_id}), removed wrong version(s): {removed_desc}")
        else:
            bump("reassigned")
            log(f"  ✔ {jira_key} (src {source_issue_id}): removed {removed_desc or 'none'} "
                f"→ added '{version_name}' ({version_id})")
    else:
        # Capture Jira's actual error body, not just the status code, so
        # failed items are diagnosable without re-running debug_single_issue.py.
        err_detail = None
        if isinstance(put_body, dict):
            err_detail = put_body.get("errors") or put_body.get("errorMessages") or put_body
        record_failure(source_issue_id, jira_key, f"PUT failed (HTTP {put_status}): {err_detail}")
        log(f"  ✖ {jira_key} (src {source_issue_id}): PUT failed (HTTP {put_status}) — {err_detail}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Reassign correct fixVersions on Jira issues.")
    parser.add_argument("--dry-run", action="store_true", help="Don't call PUT, just show what would happen.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N matching issues.")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="Thread pool size (default 10).")
    args = parser.parse_args()

    if not JIRA_URL:
        print("✖ JIRA_URL not set (check your .env). Aborting.")
        return 1

    print("=" * 70)
    print("  FIX ISSUE VERSIONS")
    print("=" * 70)
    print(f"  issues_meta.json     : {ISSUES_META_PATH}")
    print(f"  id_mapping.json       : {ID_MAPPING_PATH}")
    print(f"  versions_mapping.xlsx : {VERSIONS_XLSX_PATH}")
    print(f"  dry run              : {args.dry_run}")
    print(f"  workers               : {args.workers}")
    print()

    issues = load_issues_meta(ISSUES_META_PATH)
    id_mapping_issues = load_id_mapping(ID_MAPPING_PATH)
    by_key, by_release_id = load_versions_excel(VERSIONS_XLSX_PATH)
    load_known_correct()
    print(f"  known_correct_versions.json : {len(known_correct)} issue(s) pre-confirmed from prior runs "
          f"(will be skipped with zero API calls)\n")

    dup_warnings = find_duplicate_id_warnings(by_release_id)
    if dup_warnings:
        warn_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "duplicate_version_id_warnings.json")
        with open(warn_path, "w", encoding="utf-8") as f:
            json.dump(dup_warnings, f, indent=2)
        print(f"  ⚠ {len(dup_warnings)} release id(s) have the SAME Jira Version ID across "
              f"multiple projects in versions_mapping.xlsx (details → {warn_path}).")
        print(f"    This run still assigns strictly per (release_id, project), so it's safe "
              f"either way — but worth checking whether the Excel export is stale.\n")

    # Only issues that actually have a version value.
    work_items = [it for it in issues if norm_release_id(it.get("source_release_id"))]
    if args.limit:
        work_items = work_items[:args.limit]

    print(f"  {len(issues)} total issues in issues_meta.json")
    print(f"  {len(work_items)} issue(s) have a source_release_id (version) to process\n")

    if not work_items:
        print("Nothing to do.")
        return 0

    start = time.time()
    workers = min(args.workers, max(len(work_items), 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(process_issue, item, by_key, id_mapping_issues, args.dry_run)
            for item in work_items
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as e:
                log(f"   ✖ Worker exception: {e}")

    elapsed = time.time() - start

    # Force-flush the known-correct cache — the periodic save only writes
    # every KNOWN_CORRECT_SAVE_EVERY confirmations, so anything since the
    # last flush needs a final write here.
    if not args.dry_run:
        save_known_correct(force=True)

    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  Reassigned (no prior version)        : {counts['reassigned']}")
    print(f"  Cleaned (kept correct, removed wrong) : {counts['cleaned']}")
    print(f"  Already correct (checked live)        : {counts['already_correct_live']}")
    print(f"  Already correct (cached, untouched)   : {counts['already_correct_cached']}")
    print(f"  Skipped (no version value)            : {counts['skipped_no_version']}")
    print(f"  Failed                                : {counts['failed']}")
    print(f"  Time elapsed                          : {elapsed:.2f}s")
    print("=" * 70)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dry_run": args.dry_run,
        "counts": counts,
        "elapsed_seconds": elapsed,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    if failed_items:
        with open(FAILED_PATH, "w", encoding="utf-8") as f:
            json.dump(failed_items, f, indent=2)
        print(f"\n  ✔ {len(failed_items)} failed item(s) written → {FAILED_PATH}")

    print(f"  ✔ Report written → {REPORT_PATH}")

    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())