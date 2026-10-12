# # """
# # retry_failed_fixes.py

# # READS ./mapping/fix_duplicate_version.json (output of fix_duplicate_version.py),
# # CLASSIFIES which rows actually failed, and RETRIES them with a smarter,
# # validate-before-write strategy instead of blindly PUTting expected_id again.

# # WHY THE ORIGINAL FIX FAILED (EX-77092 case):
# #     expected_id (from versions_mapping.xlsx)   = 28770  "11_08_2026_FR"
# #     id_mapping_id (from id_mapping["versions"]) = 23015  "11_08_2026_FR"
# #     mapping_agreement                           = MISMATCH
# #     actual fixVersions on the issue             = 22940, 23002, 23015

# #     Same version NAME, two different Jira version IDs. 28770 does not
# #     exist / is not a valid version in that Jira project -> PUT with
# #     {"id": "28770"} returns 400. The correct, already-existing version
# #     (23015) was sitting right there in actual_ids the whole time.

# # STRATEGY (this is the "check first if value is present, else remove
# # duplicate" logic requested):
# #     For each row that FAILED (write_status != PUT_OK, or verify_status
# #     not in {VERIFIED}):

# #     1. Fetch the LIVE, valid version list for that Jira project
# #        (api/3/project/{key}/versions) once per project, cache it.

# #     2. Decide which ID to write, in this priority order:
# #          a) expected_id (Excel) IF it is a real/valid version in the
# #             project                                   -> source=EXCEL
# #          b) else id_mapping_id IF it is a real/valid version in the
# #             project                                   -> source=ID_MAPPING_FALLBACK
# #          c) else expected_id IF it's already attached to the issue
# #             (actual_ids) - just need to drop the extras, not add
# #             anything                                   -> source=ALREADY_PRESENT_EXCEL
# #          d) else id_mapping_id IF it's already attached to the issue
# #                                                          -> source=ALREADY_PRESENT_ID_MAPPING
# #          e) else -> cannot safely resolve. Do NOT write anything.
# #             Marked NEEDS_MANUAL_REVIEW and left for a human to fix
# #             versions_mapping.xlsx / id_mapping.json.

# #     3. If resolved (a-d): PUT fixVersions = EXACTLY [{"id": resolved_id}]
# #        (full replace -> this is the "remove duplicate" step, since any
# #        extra wrong versions are dropped automatically), then re-fetch
# #        live to verify it stuck.

# #     4. If NOT resolved (e): no write attempted, status recorded so you
# #        can see it in the report and fix the ground truth data instead.

# # Output:
# #     ./mapping/retry_failed_fixes.xlsx
# #     ./mapping/retry_failed_fixes.json

# # Usage:
# #     python retry_failed_fixes.py
# #     python retry_failed_fixes.py --projects EX,PLAT
# #     python retry_failed_fixes.py --input ./mapping/fix_duplicate_version.json
# #     python retry_failed_fixes.py --dry-run       # resolve + report only, no PUTs
# # """

# # import os
# # import json
# # import argparse
# # import concurrent.futures
# # from threading import Lock
# # from collections import Counter

# # import pandas as pd

# # import bulk_load_to_jira as core          # same .env / credentials / jira_get / jira_put / rate monitor
# # import check_duplicate_version as checker  # reuse fetch_actual_versions() for live verify


# # # =============================================================================
# # # STEP 1 — LOAD fix_duplicate_version.json AND CLASSIFY WHAT FAILED
# # # =============================================================================

# # def load_report(input_path):
# #     if not os.path.exists(input_path):
# #         raise FileNotFoundError(
# #             f"{input_path} not found. Run fix_duplicate_version.py first, "
# #             f"or pass --input to point at the report you want to retry from."
# #         )
# #     with open(input_path, "r", encoding="utf-8") as f:
# #         rows = json.load(f)
# #     print(f"  ✔ Loaded {len(rows)} row(s) from {input_path}")
# #     return rows


# # def classify_failures(rows, target_projects):
# #     """
# #     A row from fix_duplicate_version.json is FAILED if:
# #         write_status  != "PUT_OK"          (PUT itself failed / exception)
# #       OR verify_status not in ("VERIFIED",) (PUT said OK but live re-fetch
# #                                               didn't show the expected id,
# #                                               or the re-fetch itself failed)

# #     Returns (failed_rows, already_ok_rows) restricted to target_projects.
# #     """
# #     in_scope = [r for r in rows if r.get("project") in target_projects]

# #     failed, ok = [], []
# #     for r in in_scope:
# #         write_ok = r.get("write_status") == "PUT_OK"
# #         verify_ok = r.get("verify_status") == "VERIFIED"
# #         if write_ok and verify_ok:
# #             ok.append(r)
# #         else:
# #             failed.append(r)

# #     print(f"\n  In scope (target project(s)): {len(in_scope)}")
# #     print(f"    Already fixed (PUT_OK + VERIFIED): {len(ok)}")
# #     print(f"    Failed / unverified (to retry)   : {len(failed)}")

# #     breakdown = Counter((r.get("write_status"), r.get("verify_status")) for r in failed)
# #     for (ws, vs), count in breakdown.items():
# #         print(f"      write_status={ws!s:15s} verify_status={vs!s:15s} -> {count}")

# #     return failed, ok


# # # =============================================================================
# # # STEP 2 — LIVE, VALID VERSIONS PER PROJECT (cached)
# # # =============================================================================

# # _project_versions_cache = {}
# # _project_versions_lock = Lock()


# # def get_valid_version_ids(project):
# #     """
# #     Fetch the real list of versions that exist for this Jira project right
# #     now, so we never PUT an id that Jira will 400 on. Cached per project.
# #     """
# #     with _project_versions_lock:
# #         if project in _project_versions_cache:
# #             return _project_versions_cache[project]

# #     data, status = core.jira_get(f"api/3/project/{project}/versions")
# #     valid_ids = set()
# #     if status == 200 and data:
# #         for v in data:
# #             vid = v.get("id")
# #             if vid:
# #                 valid_ids.add(str(vid))
# #     else:
# #         core.thread_safe_print(f"   ⚠ Could not fetch versions for project {project} (HTTP {status})")

# #     with _project_versions_lock:
# #         _project_versions_cache[project] = valid_ids
# #     return valid_ids


# # # =============================================================================
# # # STEP 3 — RESOLVE WHICH ID TO WRITE (the "check present else dedupe" logic)
# # # =============================================================================

# # def resolve_fix_id(row, valid_version_ids):
# #     """
# #     Returns (resolved_id, resolution_source) or (None, "NEEDS_MANUAL_REVIEW").
# #     """
# #     expected_id = row.get("expected_id") or None
# #     id_mapping_id = row.get("id_mapping_id") or None

# #     actual_ids_raw = row.get("actual_ids") or row.get("pre_fix_actual_ids") or ""
# #     actual_ids = {v.strip() for v in actual_ids_raw.split(",") if v.strip()}

# #     # a) Excel's expected_id, if it's a real version in Jira right now
# #     if expected_id and expected_id in valid_version_ids:
# #         return expected_id, "EXCEL"

# #     # b) id_mapping's id, if it's a real version in Jira right now
# #     if id_mapping_id and id_mapping_id in valid_version_ids:
# #         return id_mapping_id, "ID_MAPPING_FALLBACK"

# #     # c) expected_id is already attached to the issue -> just need to
# #     #    drop extras, don't need it to independently "exist" via the
# #     #    versions endpoint (covers edge cases where that endpoint is
# #     #    filtered/paginated)
# #     if expected_id and expected_id in actual_ids:
# #         return expected_id, "ALREADY_PRESENT_EXCEL"

# #     # d) same, but for id_mapping's id
# #     if id_mapping_id and id_mapping_id in actual_ids:
# #         return id_mapping_id, "ALREADY_PRESENT_ID_MAPPING"

# #     # e) can't safely resolve — don't guess, don't write
# #     return None, "NEEDS_MANUAL_REVIEW"


# # # =============================================================================
# # # STEP 4 — APPLY + VERIFY ONE RETRY
# # # =============================================================================

# # def retry_one(row, dry_run=False):
# #     out = dict(row)
# #     project = row["project"]
# #     jira_key = row["jira_key"]

# #     valid_version_ids = get_valid_version_ids(project)
# #     resolved_id, source = resolve_fix_id(row, valid_version_ids)

# #     out["resolution_source"] = source
# #     out["resolved_id"] = resolved_id or ""

# #     if resolved_id is None:
# #         out["retry_write_status"] = "SKIPPED_NO_VALID_ID"
# #         out["retry_write_error"] = ""
# #         out["retry_verify_status"] = "SKIPPED"
# #         out["retry_verify_actual_ids"] = ""
# #         out["retry_verify_actual_names"] = ""
# #         return out

# #     if dry_run:
# #         out["retry_write_status"] = "DRY_RUN"
# #         out["retry_write_error"] = ""
# #         out["retry_verify_status"] = "SKIPPED"
# #         out["retry_verify_actual_ids"] = ""
# #         out["retry_verify_actual_names"] = ""
# #         return out

# #     payload = {"fields": {"fixVersions": [{"id": resolved_id}]}}
# #     try:
# #         data, status = core.jira_put(f"api/3/issue/{jira_key}", payload)
# #     except Exception as e:
# #         out["retry_write_status"] = "EXCEPTION"
# #         out["retry_write_error"] = str(e)
# #         out["retry_verify_status"] = "SKIPPED"
# #         out["retry_verify_actual_ids"] = ""
# #         out["retry_verify_actual_names"] = ""
# #         return out

# #     if status not in (200, 204):
# #         out["retry_write_status"] = "PUT_FAILED"
# #         out["retry_write_error"] = f"HTTP {status}: {data}"
# #         out["retry_verify_status"] = "SKIPPED"
# #         out["retry_verify_actual_ids"] = ""
# #         out["retry_verify_actual_names"] = ""
# #         return out

# #     out["retry_write_status"] = "PUT_OK"
# #     out["retry_write_error"] = ""

# #     actual = checker.fetch_actual_versions(jira_key)
# #     if actual is None:
# #         out["retry_verify_status"] = "VERIFY_FAILED"
# #         out["retry_verify_actual_ids"] = ""
# #         out["retry_verify_actual_names"] = ""
# #         return out

# #     actual_ids = [v["id"] for v in actual]
# #     out["retry_verify_actual_ids"] = ", ".join(actual_ids)
# #     out["retry_verify_actual_names"] = ", ".join(v["name"] for v in actual)
# #     out["retry_verify_status"] = "VERIFIED" if actual_ids == [resolved_id] else "STILL_WRONG"
# #     return out


# # # =============================================================================
# # # STEP 5 — MAIN RETRY RUN
# # # =============================================================================

# # def run_retry(failed_rows, dry_run=False):
# #     if not failed_rows:
# #         print("\n  Nothing to retry. Exiting.")
# #         return []

# #     print("\n" + "━" * 70)
# #     print(f"  Retrying {len(failed_rows)} failed issue(s){' (DRY RUN — no writes)' if dry_run else ''}")
# #     print("━" * 70)

# #     results = []
# #     lock = Lock()

# #     def _retry(row):
# #         fixed = retry_one(row, dry_run=dry_run)
# #         with lock:
# #             results.append(fixed)
# #             done_local = len(results)
# #         if done_local % 200 == 0:
# #             core.thread_safe_print(f"  Retried {done_local}/{len(failed_rows)}...")
# #         return fixed

# #     workers = min(getattr(core, "VERSION_ASSIGN_MAX_WORKERS", 1), len(failed_rows))
# #     print(f"  Workers: {workers}\n")
# #     with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
# #         futures = [executor.submit(_retry, row) for row in failed_rows]
# #         for future in concurrent.futures.as_completed(futures):
# #             try:
# #                 future.result()
# #             except Exception as e:
# #                 core.thread_safe_print(f"   ✖ Retry worker exception: {e}")

# #     resolution_counts = Counter(r["resolution_source"] for r in results)
# #     write_counts = Counter(r["retry_write_status"] for r in results)
# #     verify_counts = Counter(r["retry_verify_status"] for r in results)

# #     print("\n  Resolution source summary:")
# #     for source in ["EXCEL", "ID_MAPPING_FALLBACK", "ALREADY_PRESENT_EXCEL",
# #                     "ALREADY_PRESENT_ID_MAPPING", "NEEDS_MANUAL_REVIEW"]:
# #         if resolution_counts.get(source):
# #             print(f"    {source:28s}: {resolution_counts[source]}")

# #     print("\n  Retry write summary:")
# #     for status in ["PUT_OK", "PUT_FAILED", "EXCEPTION", "SKIPPED_NO_VALID_ID", "DRY_RUN"]:
# #         if write_counts.get(status):
# #             print(f"    {status:22s}: {write_counts[status]}")

# #     print("\n  Retry verify summary:")
# #     for status in ["VERIFIED", "STILL_WRONG", "VERIFY_FAILED", "SKIPPED"]:
# #         if verify_counts.get(status):
# #             print(f"    {status:22s}: {verify_counts[status]}")

# #     return results


# # def save_report(results):
# #     if not results:
# #         return

# #     json_path = os.path.join(core.MAPPING_DIR, "retry_failed_fixes.json")
# #     with open(json_path, "w", encoding="utf-8") as f:
# #         json.dump(results, f, indent=2)
# #     print(f"\n   ✔ JSON report saved -> {json_path}")

# #     rows = []
# #     for r in results:
# #         rows.append({
# #             "Source ID": r.get("source_id", ""),
# #             "Jira Key": r.get("jira_key", ""),
# #             "Jira ID": r.get("jira_id", ""),
# #             "Title": r.get("title", ""),
# #             "Project": r.get("project", ""),
# #             "Original Status": r.get("status") or r.get("pre_fix_status") or "",
# #             "Excel Expected ID": r.get("expected_id") or "",
# #             "Excel Expected Name": r.get("expected_name") or "",
# #             "id_mapping ID": r.get("id_mapping_id") or "",
# #             "Mapping Agreement": r.get("mapping_agreement") or "",
# #             "Resolution Source": r.get("resolution_source", ""),
# #             "Resolved ID Written": r.get("resolved_id", ""),
# #             "Retry Write Status": r.get("retry_write_status", ""),
# #             "Retry Write Error": r.get("retry_write_error", ""),
# #             "Retry Verify Status": r.get("retry_verify_status", ""),
# #             "Retry Post-Fix IDs": r.get("retry_verify_actual_ids", ""),
# #             "Retry Post-Fix Names": r.get("retry_verify_actual_names", ""),
# #         })

# #     xlsx_path = os.path.join(core.MAPPING_DIR, "retry_failed_fixes.xlsx")
# #     df = pd.DataFrame(rows)
# #     sort_order = {
# #         "NEEDS_MANUAL_REVIEW": 0, "STILL_WRONG": 1, "VERIFY_FAILED": 2,
# #         "PUT_FAILED": 3, "EXCEPTION": 4, "VERIFIED": 5,
# #     }
# #     df["_sort"] = df["Resolution Source"].map(lambda s: 0 if s == "NEEDS_MANUAL_REVIEW" else 9)
# #     df = df.sort_values("_sort").drop(columns="_sort")
# #     df.to_excel(xlsx_path, index=False)
# #     print(f"   ✔ Excel report saved -> {xlsx_path}")

# #     needs_review = [r for r in results if r["resolution_source"] == "NEEDS_MANUAL_REVIEW"]
# #     if needs_review:
# #         print(f"\n   ⚠ {len(needs_review)} issue(s) could NOT be safely resolved "
# #               f"(no valid expected_id or id_mapping_id, and neither is already "
# #               f"attached). These need versions_mapping.xlsx / id_mapping.json "
# #               f"corrected by hand:")
# #         for r in needs_review[:20]:
# #             print(f"       {r['jira_key']}: expected_id={r.get('expected_id')!r} "
# #                   f"id_mapping_id={r.get('id_mapping_id')!r} "
# #                   f"actual_ids={r.get('actual_ids')!r}")
# #         if len(needs_review) > 20:
# #             print(f"       ... and {len(needs_review) - 20} more (see the report)")


# # def main():
# #     parser = argparse.ArgumentParser(
# #         description="Classify failed rows from fix_duplicate_version.json and retry them "
# #                      "with a validate-before-write / dedupe-fallback strategy."
# #     )
# #     parser.add_argument("--projects", default="CEMIG,PLAT,EX",
# #                          help="Comma-separated Jira project keys to retry (default: CEMIG,PLAT,EX)")
# #     parser.add_argument("--input", default=None,
# #                          help="Path to the report to retry from "
# #                               "(default: ./mapping/fix_duplicate_version.json)")
# #     parser.add_argument("--dry-run", action="store_true",
# #                          help="Resolve + report only, do not PUT to Jira")
# #     args = parser.parse_args()

# #     target_projects = {p.strip().upper() for p in args.projects.split(",") if p.strip()}
# #     input_path = args.input or os.path.join(core.MAPPING_DIR, "fix_duplicate_version.json")

# #     print("=" * 70)
# #     print("  RETRY FAILED VERSION FIXES (validate-before-write + dedupe fallback)")
# #     print("=" * 70)
# #     print(f"  Target projects: {sorted(target_projects)}")
# #     print(f"  Input report:    {input_path}")
# #     print(f"  Dry run:         {args.dry_run}")
# #     print("=" * 70)

# #     core.start_rate_monitor()
# #     if not core.validate_all_accounts():
# #         core.stop_rate_monitor()
# #         return 1

# #     try:
# #         rows = load_report(input_path)
# #         failed_rows, _ok_rows = classify_failures(rows, target_projects)
# #         results = run_retry(failed_rows, dry_run=args.dry_run)
# #         save_report(results)
# #     finally:
# #         core.stop_rate_monitor()

# #     print("\n" + "=" * 70)
# #     print("  DONE")
# #     print("=" * 70)
# #     return 0


# # if __name__ == "__main__":
# #     exit(main())
# """
# retry_failed_fixes.py

# READS ./mapping/fix_duplicate_version.json (output of fix_duplicate_version.py),
# CLASSIFIES which rows actually failed, and RETRIES them with a smarter,
# validate-before-write strategy instead of blindly PUTting expected_id again.

# WHY THE ORIGINAL FIX FAILED (EX-77092 case):
#     expected_id (from versions_mapping.xlsx)   = 28770  "11_08_2026_FR"
#     id_mapping_id (from id_mapping["versions"]) = 23015  "11_08_2026_FR"
#     mapping_agreement                           = MISMATCH
#     actual fixVersions on the issue             = 22940, 23002, 23015

#     Same version NAME, two different Jira version IDs. 28770 does not
#     exist / is not a valid version in that Jira project -> PUT with
#     {"id": "28770"} returns 400. The correct, already-existing version
#     (23015) was sitting right there in actual_ids the whole time.

# STRATEGY (the "check first if value is present, else remove duplicate"
# logic requested):
#     For each row that FAILED (write_status != PUT_OK, or verify_status
#     not in {VERIFIED}):

#     1. Fetch the LIVE, valid version list for that Jira project
#        (api/3/project/{key}/versions) once per project, cache it.

#     2. Decide which ID to write, in this priority order:
#          a) expected_id (Excel) IF it is a real/valid version in the
#             project                                   -> source=EXCEL
#          b) else id_mapping_id IF it is a real/valid version in the
#             project                                   -> source=ID_MAPPING_FALLBACK
#          c) else expected_id IF it's already attached to the issue
#             (actual_ids) - just need to drop the extras, not add
#             anything                                   -> source=ALREADY_PRESENT_EXCEL
#          d) else id_mapping_id IF it's already attached to the issue
#                                                          -> source=ALREADY_PRESENT_ID_MAPPING
#          e) else -> cannot safely resolve. Do NOT write anything.
#             Marked NEEDS_MANUAL_REVIEW and left for a human to fix
#             versions_mapping.xlsx / id_mapping.json.

#     3. If resolved (a-d): PUT fixVersions = EXACTLY [{"id": resolved_id}]
#        (full replace -> this is the "remove duplicate" step, since any
#        extra wrong versions are dropped automatically), then re-fetch
#        live to verify it stuck.

#     4. If NOT resolved (e): no write attempted, status recorded so you
#        can see it in the report and fix the ground truth data instead.

# LOGGING:
#     Every run writes a timestamped, human-readable log of everything
#     that gets printed (all the same progress/summary lines you see on
#     screen) to a dedicated ./fix_log/ folder:

#         ./fix_log/retry_failed_fixes_<YYYYmmdd_HHMMSS>.log

#     This is a straight tee of stdout - nothing is summarized or
#     dropped, so you can grep it / diff it / attach it as evidence that
#     a run did what it says it did.

#     In addition to the full retry report (./mapping/retry_failed_fixes.json
#     / .xlsx, everything, fixed and unfixed), this script writes a
#     SEPARATE, focused report containing ONLY the issues that are still
#     NOT fixed after this run (no valid id resolved, PUT failed, or the
#     live re-fetch after PUT still didn't show the expected id):

#         ./fix_log/unresolved_issues_<YYYYmmdd_HHMMSS>.json
#         ./fix_log/unresolved_issues_<YYYYmmdd_HHMMSS>.xlsx

#     That's the list to hand to whoever owns versions_mapping.xlsx /
#     id_mapping.json, or to re-run once that ground truth is corrected.

# Output:
#     ./mapping/retry_failed_fixes.xlsx        <- everything (fixed + unfixed)
#     ./mapping/retry_failed_fixes.json
#     ./fix_log/retry_failed_fixes_<ts>.log    <- full run log
#     ./fix_log/unresolved_issues_<ts>.xlsx    <- ONLY what's still broken
#     ./fix_log/unresolved_issues_<ts>.json

# Usage:
#     python retry_failed_fixes.py
#     python retry_failed_fixes.py --projects EX,PLAT
#     python retry_failed_fixes.py --input ./mapping/fix_duplicate_version.json
#     python retry_failed_fixes.py --dry-run       # resolve + report only, no PUTs
# """

# import os
# import sys
# import json
# import argparse
# import concurrent.futures
# from threading import Lock
# from collections import Counter
# from datetime import datetime

# import pandas as pd

# import bulk_load_to_jira as core          # same .env / credentials / jira_get / jira_put / rate monitor
# import check_duplicate_version as checker  # reuse fetch_actual_versions() for live verify


# # =============================================================================
# # LOGGING — tee everything printed (from this script AND core/checker) to a
# # timestamped file under a dedicated fix_log/ folder, in addition to console
# # =============================================================================

# FIX_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(core.MAPPING_DIR)), "fix_log")


# class _Tee:
#     """Writes to multiple streams at once (console + log file)."""
#     def __init__(self, *streams):
#         self.streams = streams

#     def write(self, data):
#         for s in self.streams:
#             s.write(data)
#             s.flush()

#     def flush(self):
#         for s in self.streams:
#             s.flush()


# def start_logging():
#     """
#     Redirects stdout/stderr through a Tee so every print() - including the
#     ones already inside bulk_load_to_jira.py / check_duplicate_version.py
#     (e.g. core.thread_safe_print, rate monitor lines) - lands in the log
#     file too, not just on screen. Returns (log_path, log_file_handle,
#     original_stdout, original_stderr) so main() can restore + close it.
#     """
#     os.makedirs(FIX_LOG_DIR, exist_ok=True)
#     ts = datetime.now().strftime("%Y%m%d_%H%M%S")
#     log_path = os.path.join(FIX_LOG_DIR, f"retry_failed_fixes_{ts}.log")

#     log_file = open(log_path, "w", encoding="utf-8")
#     original_stdout, original_stderr = sys.stdout, sys.stderr
#     sys.stdout = _Tee(original_stdout, log_file)
#     sys.stderr = _Tee(original_stderr, log_file)

#     print(f"=== retry_failed_fixes.py run started {datetime.now().isoformat()} ===")
#     print(f"Log file: {log_path}")
#     return log_path, log_file, original_stdout, original_stderr, ts


# def stop_logging(log_file, original_stdout, original_stderr):
#     print(f"=== run finished {datetime.now().isoformat()} ===")
#     sys.stdout, sys.stderr = original_stdout, original_stderr
#     log_file.close()


# # =============================================================================
# # STEP 1 — LOAD fix_duplicate_version.json AND CLASSIFY WHAT FAILED
# # =============================================================================

# def load_report(input_path):
#     if not os.path.exists(input_path):
#         raise FileNotFoundError(
#             f"{input_path} not found. Run fix_duplicate_version.py first, "
#             f"or pass --input to point at the report you want to retry from."
#         )
#     with open(input_path, "r", encoding="utf-8") as f:
#         rows = json.load(f)
#     print(f"  [OK] Loaded {len(rows)} row(s) from {input_path}")
#     return rows


# def classify_failures(rows, target_projects):
#     """
#     A row from fix_duplicate_version.json is FAILED if:
#         write_status  != "PUT_OK"          (PUT itself failed / exception)
#       OR verify_status not in ("VERIFIED",) (PUT said OK but live re-fetch
#                                               didn't show the expected id,
#                                               or the re-fetch itself failed)

#     Returns (failed_rows, already_ok_rows) restricted to target_projects.
#     """
#     in_scope = [r for r in rows if r.get("project") in target_projects]

#     failed, ok = [], []
#     for r in in_scope:
#         write_ok = r.get("write_status") == "PUT_OK"
#         verify_ok = r.get("verify_status") == "VERIFIED"
#         if write_ok and verify_ok:
#             ok.append(r)
#         else:
#             failed.append(r)

#     print(f"\n  In scope (target project(s)): {len(in_scope)}")
#     print(f"    Already fixed (PUT_OK + VERIFIED): {len(ok)}")
#     print(f"    Failed / unverified (to retry)   : {len(failed)}")

#     breakdown = Counter((r.get("write_status"), r.get("verify_status")) for r in failed)
#     for (ws, vs), count in breakdown.items():
#         print(f"      write_status={ws!s:15s} verify_status={vs!s:15s} -> {count}")

#     return failed, ok


# # =============================================================================
# # STEP 2 — LIVE, VALID VERSIONS PER PROJECT (cached)
# # =============================================================================

# _project_versions_cache = {}
# _project_versions_lock = Lock()


# def get_valid_version_ids(project):
#     """
#     Fetch the real list of versions that exist for this Jira project right
#     now, so we never PUT an id that Jira will 400 on. Cached per project.
#     """
#     with _project_versions_lock:
#         if project in _project_versions_cache:
#             return _project_versions_cache[project]

#     data, status = core.jira_get(f"api/3/project/{project}/versions")
#     valid_ids = set()
#     if status == 200 and data:
#         for v in data:
#             vid = v.get("id")
#             if vid:
#                 valid_ids.add(str(vid))
#     else:
#         core.thread_safe_print(f"   [WARN] Could not fetch versions for project {project} (HTTP {status})")

#     with _project_versions_lock:
#         _project_versions_cache[project] = valid_ids
#     return valid_ids


# # =============================================================================
# # STEP 3 — RESOLVE WHICH ID TO WRITE (the "check present else dedupe" logic)
# # =============================================================================

# def resolve_fix_id(row, valid_version_ids):
#     """
#     Returns (resolved_id, resolution_source) or (None, "NEEDS_MANUAL_REVIEW").
#     """
#     expected_id = row.get("expected_id") or None
#     id_mapping_id = row.get("id_mapping_id") or None

#     actual_ids_raw = row.get("actual_ids") or row.get("pre_fix_actual_ids") or ""
#     actual_ids = {v.strip() for v in actual_ids_raw.split(",") if v.strip()}

#     # a) Excel's expected_id, if it's a real version in Jira right now
#     if expected_id and expected_id in valid_version_ids:
#         return expected_id, "EXCEL"

#     # b) id_mapping's id, if it's a real version in Jira right now
#     if id_mapping_id and id_mapping_id in valid_version_ids:
#         return id_mapping_id, "ID_MAPPING_FALLBACK"

#     # c) expected_id is already attached to the issue -> just need to
#     #    drop extras, don't need it to independently "exist" via the
#     #    versions endpoint (covers edge cases where that endpoint is
#     #    filtered/paginated)
#     if expected_id and expected_id in actual_ids:
#         return expected_id, "ALREADY_PRESENT_EXCEL"

#     # d) same, but for id_mapping's id
#     if id_mapping_id and id_mapping_id in actual_ids:
#         return id_mapping_id, "ALREADY_PRESENT_ID_MAPPING"

#     # e) can't safely resolve — don't guess, don't write
#     return None, "NEEDS_MANUAL_REVIEW"


# # =============================================================================
# # STEP 4 — APPLY + VERIFY ONE RETRY
# # =============================================================================

# def retry_one(row, dry_run=False):
#     out = dict(row)
#     project = row["project"]
#     jira_key = row["jira_key"]

#     valid_version_ids = get_valid_version_ids(project)
#     resolved_id, source = resolve_fix_id(row, valid_version_ids)

#     out["resolution_source"] = source
#     out["resolved_id"] = resolved_id or ""

#     if resolved_id is None:
#         out["retry_write_status"] = "SKIPPED_NO_VALID_ID"
#         out["retry_write_error"] = ""
#         out["retry_verify_status"] = "SKIPPED"
#         out["retry_verify_actual_ids"] = ""
#         out["retry_verify_actual_names"] = ""
#         return out

#     if dry_run:
#         out["retry_write_status"] = "DRY_RUN"
#         out["retry_write_error"] = ""
#         out["retry_verify_status"] = "SKIPPED"
#         out["retry_verify_actual_ids"] = ""
#         out["retry_verify_actual_names"] = ""
#         return out

#     payload = {"fields": {"fixVersions": [{"id": resolved_id}]}}
#     try:
#         data, status = core.jira_put(f"api/3/issue/{jira_key}", payload)
#     except Exception as e:
#         out["retry_write_status"] = "EXCEPTION"
#         out["retry_write_error"] = str(e)
#         out["retry_verify_status"] = "SKIPPED"
#         out["retry_verify_actual_ids"] = ""
#         out["retry_verify_actual_names"] = ""
#         return out

#     if status not in (200, 204):
#         out["retry_write_status"] = "PUT_FAILED"
#         out["retry_write_error"] = f"HTTP {status}: {data}"
#         out["retry_verify_status"] = "SKIPPED"
#         out["retry_verify_actual_ids"] = ""
#         out["retry_verify_actual_names"] = ""
#         return out

#     out["retry_write_status"] = "PUT_OK"
#     out["retry_write_error"] = ""

#     actual = checker.fetch_actual_versions(jira_key)
#     if actual is None:
#         out["retry_verify_status"] = "VERIFY_FAILED"
#         out["retry_verify_actual_ids"] = ""
#         out["retry_verify_actual_names"] = ""
#         return out

#     actual_ids = [v["id"] for v in actual]
#     out["retry_verify_actual_ids"] = ", ".join(actual_ids)
#     out["retry_verify_actual_names"] = ", ".join(v["name"] for v in actual)
#     out["retry_verify_status"] = "VERIFIED" if actual_ids == [resolved_id] else "STILL_WRONG"
#     return out


# # =============================================================================
# # STEP 5 — MAIN RETRY RUN
# # =============================================================================

# def run_retry(failed_rows, dry_run=False):
#     if not failed_rows:
#         print("\n  Nothing to retry. Exiting.")
#         return []

#     print("\n" + "-" * 70)
#     print(f"  Retrying {len(failed_rows)} failed issue(s){' (DRY RUN - no writes)' if dry_run else ''}")
#     print("-" * 70)

#     results = []
#     lock = Lock()

#     def _retry(row):
#         fixed = retry_one(row, dry_run=dry_run)
#         with lock:
#             results.append(fixed)
#             done_local = len(results)
#         if done_local % 200 == 0:
#             core.thread_safe_print(f"  Retried {done_local}/{len(failed_rows)}...")
#         return fixed

#     workers = min(getattr(core, "VERSION_ASSIGN_MAX_WORKERS", 5), len(failed_rows))
#     print(f"  Workers: {workers}\n")
#     with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
#         futures = [executor.submit(_retry, row) for row in failed_rows]
#         for future in concurrent.futures.as_completed(futures):
#             try:
#                 future.result()
#             except Exception as e:
#                 core.thread_safe_print(f"   [ERROR] Retry worker exception: {e}")

#     resolution_counts = Counter(r["resolution_source"] for r in results)
#     write_counts = Counter(r["retry_write_status"] for r in results)
#     verify_counts = Counter(r["retry_verify_status"] for r in results)

#     print("\n  Resolution source summary:")
#     for source in ["EXCEL", "ID_MAPPING_FALLBACK", "ALREADY_PRESENT_EXCEL",
#                     "ALREADY_PRESENT_ID_MAPPING", "NEEDS_MANUAL_REVIEW"]:
#         if resolution_counts.get(source):
#             print(f"    {source:28s}: {resolution_counts[source]}")

#     print("\n  Retry write summary:")
#     for status in ["PUT_OK", "PUT_FAILED", "EXCEPTION", "SKIPPED_NO_VALID_ID", "DRY_RUN"]:
#         if write_counts.get(status):
#             print(f"    {status:22s}: {write_counts[status]}")

#     print("\n  Retry verify summary:")
#     for status in ["VERIFIED", "STILL_WRONG", "VERIFY_FAILED", "SKIPPED"]:
#         if verify_counts.get(status):
#             print(f"    {status:22s}: {verify_counts[status]}")

#     return results


# def save_full_report(results):
#     """Everything from this retry run — fixed AND still-unfixed — goes here."""
#     if not results:
#         return

#     json_path = os.path.join(core.MAPPING_DIR, "retry_failed_fixes.json")
#     with open(json_path, "w", encoding="utf-8") as f:
#         json.dump(results, f, indent=2)
#     print(f"\n   [OK] Full retry JSON saved -> {json_path}")

#     rows = [_row_to_report_dict(r) for r in results]
#     xlsx_path = os.path.join(core.MAPPING_DIR, "retry_failed_fixes.xlsx")
#     df = pd.DataFrame(rows)
#     df["_sort"] = df["Resolution Source"].map(lambda s: 0 if s == "NEEDS_MANUAL_REVIEW" else 9)
#     df = df.sort_values("_sort").drop(columns="_sort")
#     df.to_excel(xlsx_path, index=False)
#     print(f"   [OK] Full retry Excel saved -> {xlsx_path}")


# def _row_to_report_dict(r):
#     return {
#         "Source ID": r.get("source_id", ""),
#         "Jira Key": r.get("jira_key", ""),
#         "Jira ID": r.get("jira_id", ""),
#         "Title": r.get("title", ""),
#         "Project": r.get("project", ""),
#         "Original Status": r.get("status") or r.get("pre_fix_status") or "",
#         "Excel Expected ID": r.get("expected_id") or "",
#         "Excel Expected Name": r.get("expected_name") or "",
#         "id_mapping ID": r.get("id_mapping_id") or "",
#         "Mapping Agreement": r.get("mapping_agreement") or "",
#         "Pre-Fix Actual IDs": r.get("actual_ids", ""),
#         "Pre-Fix Actual Names": r.get("actual_names", ""),
#         "Resolution Source": r.get("resolution_source", ""),
#         "Resolved ID Written": r.get("resolved_id", ""),
#         "Retry Write Status": r.get("retry_write_status", ""),
#         "Retry Write Error": r.get("retry_write_error", ""),
#         "Retry Verify Status": r.get("retry_verify_status", ""),
#         "Retry Post-Fix IDs": r.get("retry_verify_actual_ids", ""),
#         "Retry Post-Fix Names": r.get("retry_verify_actual_names", ""),
#     }


# def is_still_unresolved(r):
#     """True if, after this retry attempt, the issue is STILL not correctly fixed."""
#     return not (r.get("retry_write_status") == "PUT_OK" and r.get("retry_verify_status") == "VERIFIED")


# def save_unresolved_report(results, ts):
#     """
#     ONLY the issues that are still broken after this run - the list to hand
#     to whoever owns versions_mapping.xlsx / id_mapping.json. Written to the
#     dedicated fix_log/ folder, timestamped to match the run's log file.
#     """
#     unresolved = [r for r in results if is_still_unresolved(r)]

#     print(f"\n  Still unresolved after this run: {len(unresolved)} of {len(results)}")

#     if not unresolved:
#         print("   Nothing unresolved - skipping unresolved_issues report.")
#         return unresolved

#     os.makedirs(FIX_LOG_DIR, exist_ok=True)

#     json_path = os.path.join(FIX_LOG_DIR, f"unresolved_issues_{ts}.json")
#     with open(json_path, "w", encoding="utf-8") as f:
#         json.dump(unresolved, f, indent=2)
#     print(f"   [OK] Unresolved JSON saved -> {json_path}")

#     rows = [_row_to_report_dict(r) for r in unresolved]
#     xlsx_path = os.path.join(FIX_LOG_DIR, f"unresolved_issues_{ts}.xlsx")
#     df = pd.DataFrame(rows)
#     resolution_order = {"NEEDS_MANUAL_REVIEW": 0}
#     df["_sort"] = df["Resolution Source"].map(lambda s: resolution_order.get(s, 9))
#     df = df.sort_values("_sort").drop(columns="_sort")
#     df.to_excel(xlsx_path, index=False)
#     print(f"   [OK] Unresolved Excel saved -> {xlsx_path}")

#     print(f"\n   [WARN] {len(unresolved)} issue(s) still need attention:")
#     for r in unresolved[:20]:
#         print(f"       {r.get('jira_key')}: resolution={r.get('resolution_source')} "
#               f"retry_write={r.get('retry_write_status')} retry_verify={r.get('retry_verify_status')} "
#               f"expected_id={r.get('expected_id')!r} id_mapping_id={r.get('id_mapping_id')!r} "
#               f"actual_ids={r.get('actual_ids')!r}")
#     if len(unresolved) > 20:
#         print(f"       ... and {len(unresolved) - 20} more (see the report)")

#     return unresolved


# def main():
#     parser = argparse.ArgumentParser(
#         description="Classify failed rows from fix_duplicate_version.json and retry them "
#                      "with a validate-before-write / dedupe-fallback strategy."
#     )
#     parser.add_argument("--projects", default="CEMIG,PLAT,EX",
#                          help="Comma-separated Jira project keys to retry (default: CEMIG,PLAT,EX)")
#     parser.add_argument("--input", default=None,
#                          help="Path to the report to retry from "
#                               "(default: ./mapping/fix_duplicate_version.json)")
#     parser.add_argument("--dry-run", action="store_true",
#                          help="Resolve + report only, do not PUT to Jira")
#     args = parser.parse_args()

#     log_path, log_file, original_stdout, original_stderr, ts = start_logging()

#     try:
#         target_projects = {p.strip().upper() for p in args.projects.split(",") if p.strip()}
#         input_path = args.input or os.path.join(core.MAPPING_DIR, "fix_duplicate_version.json")

#         print("=" * 70)
#         print("  RETRY FAILED VERSION FIXES (validate-before-write + dedupe fallback)")
#         print("=" * 70)
#         print(f"  Target projects: {sorted(target_projects)}")
#         print(f"  Input report:    {input_path}")
#         print(f"  Dry run:         {args.dry_run}")
#         print("=" * 70)

#         core.start_rate_monitor()
#         if not core.validate_all_accounts():
#             core.stop_rate_monitor()
#             return 1

#         try:
#             rows = load_report(input_path)
#             failed_rows, _ok_rows = classify_failures(rows, target_projects)
#             results = run_retry(failed_rows, dry_run=args.dry_run)
#             save_full_report(results)
#             save_unresolved_report(results, ts)
#         finally:
#             core.stop_rate_monitor()

#         print("\n" + "=" * 70)
#         print("  DONE")
#         print(f"  Log file: {log_path}")
#         print("=" * 70)
#         return 0
#     finally:
#         stop_logging(log_file, original_stdout, original_stderr)


# if __name__ == "__main__":
#     exit(main())
# """
# fix_from_check_duplicate.py

# READS ./mapping/check_duplicate_version.json (the raw output of
# check_duplicate_version.py's run_check(), BEFORE it's flattened to Excel),
# resolves the correct fixVersion for every row that isn't already right,
# and writes the fix straight to Jira. No fix_duplicate_version.py /
# retry_failed_fixes.py hop needed.

# RESOLUTION LOGIC (priority order), IDs normalized so "28770", "28770.0",
# 28770, 28770.0 all compare equal:

#     a) expected_id (versions_mapping.xlsx) IF it's a real, currently valid
#        version in the project              -> source=EXCEL
#     b) id_mapping_id IF it's a real, currently valid version in the
#        project                             -> source=ID_MAPPING_FALLBACK
#     c) expected_id already attached to the issue (actual_ids)
#                                             -> source=ALREADY_PRESENT_EXCEL
#     d) id_mapping_id already attached to the issue
#                                             -> source=ALREADY_PRESENT_ID_MAPPING
#     e) exactly ONE of the currently-attached IDs is a real, valid version
#        in the project (even if it matches neither Excel nor id_mapping)
#        -> keep that one, drop the rest      -> source=VALID_FROM_ACTUAL_DEDUPE
#     f) none of the above -> cannot safely resolve, no write attempted
#                                             -> source=NEEDS_MANUAL_REVIEW

# SKIP (no write, no PUT call at all):
#     - Row already has exactly one actual fixVersion AND it equals the
#       resolved id -> nothing to do, marked ALREADY_CORRECT.

# WRITE:
#     Full replace: fixVersions = EXACTLY [{"id": resolved_id}]. This is
#     the "remove duplicate/invalid extras" step — any wrong or extra
#     versions currently on the issue are dropped automatically because
#     it's a full replace, not an add.

# VERIFY:
#     Immediately re-fetch live fixVersions after the PUT and confirm it's
#     exactly [resolved_id]. Anything else is flagged STILL_WRONG.

# Usage:
#     python fix_from_check_duplicate.py
#     python fix_from_check_duplicate.py --projects EX,PLAT
#     python fix_from_check_duplicate.py --input ./mapping/check_duplicate_version.json
#     python fix_from_check_duplicate.py --dry-run       # resolve + report only, no PUTs
# """

# import os
# import sys
# import json
# import argparse
# import concurrent.futures
# from threading import Lock
# from collections import Counter
# from datetime import datetime

# import pandas as pd

# import bulk_load_to_jira as core          # same .env / credentials / jira_get / jira_put / rate monitor
# import check_duplicate_version as checker  # reuse fetch_actual_versions() for live verify


# # =============================================================================
# # LOGGING
# # =============================================================================

# FIX_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(core.MAPPING_DIR)), "fix_log")


# class _Tee:
#     def __init__(self, *streams):
#         self.streams = streams

#     def write(self, data):
#         for s in self.streams:
#             s.write(data)
#             s.flush()

#     def flush(self):
#         for s in self.streams:
#             s.flush()


# def start_logging():
#     os.makedirs(FIX_LOG_DIR, exist_ok=True)
#     ts = datetime.now().strftime("%Y%m%d_%H%M%S")
#     log_path = os.path.join(FIX_LOG_DIR, f"fix_from_check_duplicate_{ts}.log")

#     log_file = open(log_path, "w", encoding="utf-8")
#     original_stdout, original_stderr = sys.stdout, sys.stderr
#     sys.stdout = _Tee(original_stdout, log_file)
#     sys.stderr = _Tee(original_stderr, log_file)

#     print(f"=== fix_from_check_duplicate.py run started {datetime.now().isoformat()} ===")
#     print(f"Log file: {log_path}")
#     return log_path, log_file, original_stdout, original_stderr, ts


# def stop_logging(log_file, original_stdout, original_stderr):
#     print(f"=== run finished {datetime.now().isoformat()} ===")
#     sys.stdout, sys.stderr = original_stdout, original_stderr
#     log_file.close()


# # =============================================================================
# # ID NORMALIZATION — the root cause of "everything -> NEEDS_MANUAL_REVIEW"
# # is almost always float-vs-string formatting ("28770" vs "28770.0").
# # Normalize EVERYTHING through this before comparing.
# # =============================================================================

# def _norm_id(val):
#     if val is None:
#         return None
#     s = str(val).strip()
#     if s == "" or s.lower() in ("nan", "none"):
#         return None
#     try:
#         f = float(s)
#         if f.is_integer():
#             return str(int(f))
#         return s
#     except ValueError:
#         return s


# def _norm_id_list(val):
#     """
#     Accepts a comma-separated string, a list, or a single value and
#     returns a set of normalized, non-empty IDs.
#     """
#     if val is None:
#         return set()
#     if isinstance(val, (list, tuple, set)):
#         items = val
#     else:
#         items = str(val).split(",")
#     out = {_norm_id(v) for v in items}
#     out.discard(None)
#     return out


# # =============================================================================
# # STEP 1 — LOAD check_duplicate_version.json AND FILTER TO WHAT NEEDS FIXING
# # =============================================================================

# def load_rows(input_path):
#     if not os.path.exists(input_path):
#         raise FileNotFoundError(
#             f"{input_path} not found. Run check_duplicate_version.py first, "
#             f"or pass --input to point at the JSON you want to fix from."
#         )
#     with open(input_path, "r", encoding="utf-8") as f:
#         rows = json.load(f)
#     print(f"  [OK] Loaded {len(rows)} row(s) from: {input_path}")
#     if rows:
#         sample = rows[0]
#         print(f"  [DEBUG] Sample row keys: {list(sample.keys())}")
#     return rows


# # Statuses from check_duplicate_version.py that mean "something's off"
# NEEDS_FIX_STATUSES = {
#     "MULTIPLE_WRONG", "CORRECT_PLUS_EXTRA", "WRONG_ONLY", "MISSING",
# }
# # Statuses we never touch (nothing to write, or nothing we can safely act on)
# SKIP_STATUSES = {"NO_META", "NO_RELEASE_ID", "UNRESOLVED", "GET_FAILED"}


# def select_rows_to_fix(rows, target_projects):
#     in_scope = [r for r in rows if r.get("project") in target_projects]

#     to_fix, already_ok, skipped_status = [], [], []
#     for r in in_scope:
#         status = r.get("status", "")
#         if status == "OK":
#             already_ok.append(r)
#         elif status in SKIP_STATUSES:
#             skipped_status.append(r)
#         else:
#             to_fix.append(r)

#     print(f"\n  In scope (target project(s)): {len(in_scope)}")
#     print(f"    Already OK (status=OK)              : {len(already_ok)}")
#     print(f"    Skipped (no safe action, e.g. missing meta/release) : {len(skipped_status)}")
#     print(f"    To attempt fixing                    : {len(to_fix)}")

#     if to_fix:
#         breakdown = Counter(r.get("status") for r in to_fix)
#         print("\n    Status breakdown of rows to fix:")
#         for status_val, count in breakdown.most_common():
#             print(f"      Status={status_val:25s} -> {count}")

#     return to_fix


# # =============================================================================
# # STEP 2 — LIVE, VALID VERSIONS PER PROJECT (cached)
# # =============================================================================

# _project_versions_cache = {}
# _project_version_names_cache = {}
# _project_versions_lock = Lock()


# def get_valid_version_ids(project):
#     """Returns (set_of_valid_ids, {id: name}) for the project, cached."""
#     with _project_versions_lock:
#         if project in _project_versions_cache:
#             return _project_versions_cache[project], _project_version_names_cache[project]

#     data, status = core.jira_get(f"api/3/project/{project}/versions")
#     valid_ids = set()
#     id_to_name = {}
#     if status == 200 and data:
#         for v in data:
#             vid = _norm_id(v.get("id"))
#             if vid:
#                 valid_ids.add(vid)
#                 id_to_name[vid] = v.get("name", "")
#     else:
#         core.thread_safe_print(f"   [WARN] Could not fetch versions for project {project} (HTTP {status})")

#     with _project_versions_lock:
#         _project_versions_cache[project] = valid_ids
#         _project_version_names_cache[project] = id_to_name
#     return valid_ids, id_to_name


# # =============================================================================
# # STEP 3 — RESOLVE WHICH ID TO WRITE
# # =============================================================================

# def resolve_fix_id(row, valid_version_ids):
#     """
#     Returns (resolved_id, resolution_source) or (None, "NEEDS_MANUAL_REVIEW").
#     All comparisons go through _norm_id / _norm_id_list.
#     """
#     expected_id = _norm_id(row.get("expected_id"))
#     id_mapping_id = _norm_id(row.get("id_mapping_id"))
#     actual_ids = _norm_id_list(row.get("actual_ids"))

#     # a) Excel's expected_id, if it's a real version in Jira right now
#     if expected_id and expected_id in valid_version_ids:
#         return expected_id, "EXCEL"

#     # b) id_mapping's id, if it's a real version in Jira right now
#     if id_mapping_id and id_mapping_id in valid_version_ids:
#         return id_mapping_id, "ID_MAPPING_FALLBACK"

#     # c) expected_id is already attached to the issue -> just drop extras
#     if expected_id and expected_id in actual_ids:
#         return expected_id, "ALREADY_PRESENT_EXCEL"

#     # d) same, but for id_mapping's id
#     if id_mapping_id and id_mapping_id in actual_ids:
#         return id_mapping_id, "ALREADY_PRESENT_ID_MAPPING"

#     # e) exactly one currently-attached id is a real, valid version
#     #    (matches neither Excel nor id_mapping, but it's real -> keep it,
#     #    drop the rest). If MORE than one attached id is valid, that's
#     #    ambiguous -> fall through to manual review, don't guess.
#     valid_actual = actual_ids & valid_version_ids
#     if len(valid_actual) == 1:
#         return next(iter(valid_actual)), "VALID_FROM_ACTUAL_DEDUPE"

#     # f) can't safely resolve
#     return None, "NEEDS_MANUAL_REVIEW"


# # =============================================================================
# # STEP 4 — APPLY + VERIFY ONE FIX
# # =============================================================================

# def fix_one(row, dry_run=False):
#     out = dict(row)
#     project = row.get("project")
#     jira_key = row.get("jira_key")

#     valid_version_ids = get_valid_version_ids(project)
#     resolved_id, source = resolve_fix_id(row, valid_version_ids)

#     out["resolution_source"] = source
#     out["resolved_id"] = resolved_id or ""

#     if resolved_id is None:
#         out["fix_write_status"] = "SKIPPED_NO_VALID_ID"
#         out["fix_write_error"] = ""
#         out["fix_verify_status"] = "SKIPPED"
#         out["fix_verify_actual_ids"] = ""
#         out["fix_verify_actual_names"] = ""
#         return out

#     # SKIP if it's already exactly right — no PUT needed at all
#     actual_ids = _norm_id_list(row.get("actual_ids"))
#     if actual_ids == {resolved_id}:
#         out["resolution_source"] = "ALREADY_CORRECT"
#         out["fix_write_status"] = "SKIPPED_ALREADY_CORRECT"
#         out["fix_write_error"] = ""
#         out["fix_verify_status"] = "SKIPPED"
#         out["fix_verify_actual_ids"] = row.get("actual_ids", "")
#         out["fix_verify_actual_names"] = row.get("actual_names", "")
#         return out

#     if dry_run:
#         out["fix_write_status"] = "DRY_RUN"
#         out["fix_write_error"] = ""
#         out["fix_verify_status"] = "SKIPPED"
#         out["fix_verify_actual_ids"] = ""
#         out["fix_verify_actual_names"] = ""
#         return out

#     payload = {"fields": {"fixVersions": [{"id": resolved_id}]}}
#     try:
#         data, status = core.jira_put(f"api/3/issue/{jira_key}", payload)
#     except Exception as e:
#         out["fix_write_status"] = "EXCEPTION"
#         out["fix_write_error"] = str(e)
#         out["fix_verify_status"] = "SKIPPED"
#         out["fix_verify_actual_ids"] = ""
#         out["fix_verify_actual_names"] = ""
#         return out

#     if status not in (200, 204):
#         out["fix_write_status"] = "PUT_FAILED"
#         out["fix_write_error"] = f"HTTP {status}: {data}"
#         out["fix_verify_status"] = "SKIPPED"
#         out["fix_verify_actual_ids"] = ""
#         out["fix_verify_actual_names"] = ""
#         return out

#     out["fix_write_status"] = "PUT_OK"
#     out["fix_write_error"] = ""

#     actual = checker.fetch_actual_versions(jira_key)
#     if actual is None:
#         out["fix_verify_status"] = "VERIFY_FAILED"
#         out["fix_verify_actual_ids"] = ""
#         out["fix_verify_actual_names"] = ""
#         return out

#     verify_ids = {_norm_id(v["id"]) for v in actual}
#     out["fix_verify_actual_ids"] = ", ".join(v["id"] for v in actual)
#     out["fix_verify_actual_names"] = ", ".join(v["name"] for v in actual)
#     out["fix_verify_status"] = "VERIFIED" if verify_ids == {resolved_id} else "STILL_WRONG"
#     return out


# # =============================================================================
# # STEP 5 — MAIN RUN
# # =============================================================================

# def run_fix(rows_to_fix, dry_run=False):
#     if not rows_to_fix:
#         print("\n  Nothing to fix. Exiting.")
#         return []

#     print("\n" + "-" * 70)
#     print(f"  Fixing {len(rows_to_fix)} issue(s){' (DRY RUN - no writes)' if dry_run else ''}")
#     print("-" * 70)

#     results = []
#     lock = Lock()

#     def _fix(row):
#         fixed = fix_one(row, dry_run=dry_run)
#         with lock:
#             results.append(fixed)
#             done_local = len(results)
#         if done_local % 200 == 0:
#             core.thread_safe_print(f"  Processed {done_local}/{len(rows_to_fix)}...")
#         return fixed

#     workers = min(getattr(core, "VERSION_ASSIGN_MAX_WORKERS", 1), len(rows_to_fix))
#     print(f"  Workers: {workers}\n")
#     with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
#         futures = [executor.submit(_fix, row) for row in rows_to_fix]
#         for future in concurrent.futures.as_completed(futures):
#             try:
#                 future.result()
#             except Exception as e:
#                 core.thread_safe_print(f"   [ERROR] Fix worker exception: {e}")

#     resolution_counts = Counter(r["resolution_source"] for r in results)
#     write_counts = Counter(r["fix_write_status"] for r in results)
#     verify_counts = Counter(r["fix_verify_status"] for r in results)

#     print("\n  Resolution source summary:")
#     for source in ["ALREADY_CORRECT", "EXCEL", "ID_MAPPING_FALLBACK",
#                     "ALREADY_PRESENT_EXCEL", "ALREADY_PRESENT_ID_MAPPING",
#                     "VALID_FROM_ACTUAL_DEDUPE", "NEEDS_MANUAL_REVIEW"]:
#         if resolution_counts.get(source):
#             print(f"    {source:28s}: {resolution_counts[source]}")

#     print("\n  Write summary:")
#     for status in ["PUT_OK", "PUT_FAILED", "EXCEPTION", "SKIPPED_NO_VALID_ID",
#                     "SKIPPED_ALREADY_CORRECT", "DRY_RUN"]:
#         if write_counts.get(status):
#             print(f"    {status:26s}: {write_counts[status]}")

#     print("\n  Verify summary:")
#     for status in ["VERIFIED", "STILL_WRONG", "VERIFY_FAILED", "SKIPPED"]:
#         if verify_counts.get(status):
#             print(f"    {status:22s}: {verify_counts[status]}")

#     return results


# def _row_to_report_dict(r):
#     return {
#         "Source ID": r.get("source_id", ""),
#         "Jira Key": r.get("jira_key", ""),
#         "Jira ID": r.get("jira_id", ""),
#         "Title": r.get("title", ""),
#         "Project": r.get("project", ""),
#         "Original Status": r.get("status", ""),
#         "Excel Expected ID": r.get("expected_id") or "",
#         "Excel Expected Name": r.get("expected_name") or "",
#         "id_mapping ID": r.get("id_mapping_id") or "",
#         "Mapping Agreement": r.get("mapping_agreement") or "",
#         "Pre-Fix Actual IDs": r.get("actual_ids", ""),
#         "Pre-Fix Actual Names": r.get("actual_names", ""),
#         "Resolution Source": r.get("resolution_source", ""),
#         "Resolved ID Written": r.get("resolved_id", ""),
#         "Write Status": r.get("fix_write_status", ""),
#         "Write Error": r.get("fix_write_error", ""),
#         "Verify Status": r.get("fix_verify_status", ""),
#         "Post-Fix IDs": r.get("fix_verify_actual_ids", ""),
#         "Post-Fix Names": r.get("fix_verify_actual_names", ""),
#     }


# def save_full_report(results):
#     if not results:
#         return
#     json_path = os.path.join(core.MAPPING_DIR, "fix_from_check_duplicate.json")
#     with open(json_path, "w", encoding="utf-8") as f:
#         json.dump(results, f, indent=2)
#     print(f"\n   [OK] Full JSON saved -> {json_path}")

#     rows = [_row_to_report_dict(r) for r in results]
#     xlsx_path = os.path.join(core.MAPPING_DIR, "fix_from_check_duplicate.xlsx")
#     df = pd.DataFrame(rows)
#     df["_sort"] = df["Resolution Source"].map(lambda s: 0 if s == "NEEDS_MANUAL_REVIEW" else 9)
#     df = df.sort_values("_sort").drop(columns="_sort")
#     df.to_excel(xlsx_path, index=False)
#     print(f"   [OK] Full Excel saved -> {xlsx_path}")


# def is_still_unresolved(r):
#     return r.get("resolution_source") == "NEEDS_MANUAL_REVIEW" or not (
#         r.get("fix_write_status") in ("PUT_OK", "SKIPPED_ALREADY_CORRECT")
#         and r.get("fix_verify_status") in ("VERIFIED", "SKIPPED")
#     )


# def save_unresolved_report(results, ts):
#     unresolved = [r for r in results if is_still_unresolved(r)]
#     print(f"\n  Still unresolved after this run: {len(unresolved)} of {len(results)}")

#     if not unresolved:
#         print("   Nothing unresolved - skipping unresolved_issues report.")
#         return unresolved

#     os.makedirs(FIX_LOG_DIR, exist_ok=True)

#     json_path = os.path.join(FIX_LOG_DIR, f"unresolved_issues_{ts}.json")
#     with open(json_path, "w", encoding="utf-8") as f:
#         json.dump(unresolved, f, indent=2)
#     print(f"   [OK] Unresolved JSON saved -> {json_path}")

#     rows = [_row_to_report_dict(r) for r in unresolved]
#     xlsx_path = os.path.join(FIX_LOG_DIR, f"unresolved_issues_{ts}.xlsx")
#     df = pd.DataFrame(rows)
#     resolution_order = {"NEEDS_MANUAL_REVIEW": 0}
#     df["_sort"] = df["Resolution Source"].map(lambda s: resolution_order.get(s, 9))
#     df = df.sort_values("_sort").drop(columns="_sort")
#     df.to_excel(xlsx_path, index=False)
#     print(f"   [OK] Unresolved Excel saved -> {xlsx_path}")

#     print(f"\n   [WARN] {len(unresolved)} issue(s) still need attention:")
#     for r in unresolved[:20]:
#         print(f"       {r.get('jira_key')}: resolution={r.get('resolution_source')} "
#               f"write={r.get('fix_write_status')} verify={r.get('fix_verify_status')} "
#               f"expected_id={r.get('expected_id')!r} id_mapping_id={r.get('id_mapping_id')!r} "
#               f"actual_ids={r.get('actual_ids')!r}")
#     if len(unresolved) > 20:
#         print(f"       ... and {len(unresolved) - 20} more (see the report)")

#     return unresolved


# def main():
#     parser = argparse.ArgumentParser(
#         description="Fix wrong/duplicate Jira fixVersions directly from "
#                      "check_duplicate_version.json (skips fix_duplicate_version.py)."
#     )
#     parser.add_argument("--projects", default="CEMIG,PLAT,EX",
#                          help="Comma-separated Jira project keys to fix (default: CEMIG,PLAT,EX)")
#     parser.add_argument("--input", default=None,
#                          help="Path to check_duplicate_version.json "
#                               "(default: ./mapping/check_duplicate_version.json)")
#     parser.add_argument("--dry-run", action="store_true",
#                          help="Resolve + report only, do not PUT to Jira")
#     args = parser.parse_args()

#     log_path, log_file, original_stdout, original_stderr, ts = start_logging()

#     try:
#         target_projects = {p.strip().upper() for p in args.projects.split(",") if p.strip()}
#         input_path = args.input or os.path.join(core.MAPPING_DIR, "check_duplicate_version.json")

#         print("=" * 70)
#         print("  FIX WRONG/DUPLICATE VERSIONS FROM check_duplicate_version.json")
#         print("=" * 70)
#         print(f"  Target projects: {sorted(target_projects)}")
#         print(f"  Input:           {input_path}")
#         print(f"  Dry run:         {args.dry_run}")
#         print("=" * 70)

#         core.start_rate_monitor()
#         if not core.validate_all_accounts():
#             core.stop_rate_monitor()
#             return 1

#         try:
#             rows = load_rows(input_path)
#             rows_to_fix = select_rows_to_fix(rows, target_projects)
#             results = run_fix(rows_to_fix, dry_run=args.dry_run)
#             save_full_report(results)
#             save_unresolved_report(results, ts)
#         finally:
#             core.stop_rate_monitor()

#         print("\n" + "=" * 70)
#         print("  DONE")
#         print(f"  Log file: {log_path}")
#         print("=" * 70)
#         return 0
#     finally:
#         stop_logging(log_file, original_stdout, original_stderr)


# if __name__ == "__main__":
#     exit(main())
"""
fix_from_check_duplicate.py

READS ./mapping/check_duplicate_version.json (the raw output of
check_duplicate_version.py's run_check(), BEFORE it's flattened to Excel),
resolves the correct fixVersion for every row that isn't already right,
and writes the fix straight to Jira. No fix_duplicate_version.py /
retry_failed_fixes.py hop needed.

RESOLUTION LOGIC (priority order), IDs normalized so "28770", "28770.0",
28770, 28770.0 all compare equal:

    a) expected_id (versions_mapping.xlsx) IF it's a real, currently valid
       version in the project              -> source=EXCEL
    b) id_mapping_id IF it's a real, currently valid version in the
       project                             -> source=ID_MAPPING_FALLBACK
    c) expected_id already attached to the issue (actual_ids)
                                            -> source=ALREADY_PRESENT_EXCEL
    d) id_mapping_id already attached to the issue
                                            -> source=ALREADY_PRESENT_ID_MAPPING
    e) exactly ONE of the currently-attached IDs is a real, valid version
       in the project (even if it matches neither Excel nor id_mapping)
       -> keep that one, drop the rest      -> source=VALID_FROM_ACTUAL_DEDUPE
    f) none of the above -> cannot safely resolve, no write attempted
                                            -> source=NEEDS_MANUAL_REVIEW

SKIP (no write, no PUT call at all):
    - Row already has exactly one actual fixVersion AND it equals the
      resolved id -> nothing to do, marked ALREADY_CORRECT.

WRITE:
    Full replace: fixVersions = EXACTLY [{"id": resolved_id}]. This is
    the "remove duplicate/invalid extras" step — any wrong or extra
    versions currently on the issue are dropped automatically because
    it's a full replace, not an add.

VERIFY:
    Immediately re-fetch live fixVersions after the PUT and confirm it's
    exactly [resolved_id]. Anything else is flagged STILL_WRONG.

Usage:
    python fix_from_check_duplicate.py
    python fix_from_check_duplicate.py --projects EX,PLAT
    python fix_from_check_duplicate.py --input ./mapping/check_duplicate_version.json
    python fix_from_check_duplicate.py --dry-run       # resolve + report only, no PUTs
"""

import os
import sys
import json
import argparse
import concurrent.futures
from threading import Lock
from collections import Counter
from datetime import datetime

import pandas as pd

import bulk_load_to_jira as core          # same .env / credentials / jira_get / jira_put / rate monitor
import check_duplicate_version as checker  # reuse fetch_actual_versions() for live verify


# =============================================================================
# LOGGING
# =============================================================================

FIX_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(core.MAPPING_DIR)), "fix_log")


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


def start_logging():
    os.makedirs(FIX_LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(FIX_LOG_DIR, f"fix_from_check_duplicate_{ts}.log")

    log_file = open(log_path, "w", encoding="utf-8")
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = _Tee(original_stdout, log_file)
    sys.stderr = _Tee(original_stderr, log_file)

    print(f"=== fix_from_check_duplicate.py run started {datetime.now().isoformat()} ===")
    print(f"Log file: {log_path}")
    return log_path, log_file, original_stdout, original_stderr, ts


def stop_logging(log_file, original_stdout, original_stderr):
    print(f"=== run finished {datetime.now().isoformat()} ===")
    sys.stdout, sys.stderr = original_stdout, original_stderr
    log_file.close()


# =============================================================================
# ID NORMALIZATION — the root cause of "everything -> NEEDS_MANUAL_REVIEW"
# is almost always float-vs-string formatting ("28770" vs "28770.0").
# Normalize EVERYTHING through this before comparing.
# =============================================================================

def _norm_id(val):
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "none"):
        return None
    try:
        f = float(s)
        if f.is_integer():
            return str(int(f))
        return s
    except ValueError:
        return s


def _norm_id_list(val):
    """
    Accepts a comma-separated string, a list, or a single value and
    returns a set of normalized, non-empty IDs.
    """
    if val is None:
        return set()
    if isinstance(val, (list, tuple, set)):
        items = val
    else:
        items = str(val).split(",")
    out = {_norm_id(v) for v in items}
    out.discard(None)
    return out


# =============================================================================
# STEP 1 — LOAD check_duplicate_version.json AND FILTER TO WHAT NEEDS FIXING
# =============================================================================

def load_rows(input_path):
    if not os.path.exists(input_path):
        raise FileNotFoundError(
            f"{input_path} not found. Run check_duplicate_version.py first, "
            f"or pass --input to point at the JSON you want to fix from."
        )
    with open(input_path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    print(f"  [OK] Loaded {len(rows)} row(s) from: {input_path}")
    if rows:
        sample = rows[0]
        print(f"  [DEBUG] Sample row keys: {list(sample.keys())}")
    return rows


# Statuses from check_duplicate_version.py that mean "something's off"
NEEDS_FIX_STATUSES = {
    "MULTIPLE_WRONG", "CORRECT_PLUS_EXTRA", "WRONG_ONLY", "MISSING",
}
# Statuses we never touch (nothing to write, or nothing we can safely act on)
SKIP_STATUSES = {"NO_META", "NO_RELEASE_ID", "UNRESOLVED", "GET_FAILED"}


def select_rows_to_fix(rows, target_projects):
    in_scope = [r for r in rows if r.get("project") in target_projects]

    to_fix, already_ok, skipped_status = [], [], []
    for r in in_scope:
        status = r.get("status", "")
        if status == "OK":
            already_ok.append(r)
        elif status in SKIP_STATUSES:
            skipped_status.append(r)
        else:
            to_fix.append(r)

    print(f"\n  In scope (target project(s)): {len(in_scope)}")
    print(f"    Already OK (status=OK)              : {len(already_ok)}")
    print(f"    Skipped (no safe action, e.g. missing meta/release) : {len(skipped_status)}")
    print(f"    To attempt fixing                    : {len(to_fix)}")

    if to_fix:
        breakdown = Counter(r.get("status") for r in to_fix)
        print("\n    Status breakdown of rows to fix:")
        for status_val, count in breakdown.most_common():
            print(f"      Status={status_val:25s} -> {count}")

    return to_fix


# =============================================================================
# STEP 2 — LIVE, VALID VERSIONS PER PROJECT (cached)
# =============================================================================

_project_versions_cache = {}
_project_version_names_cache = {}
_project_versions_lock = Lock()


def get_valid_version_ids(project):
    """Returns (set_of_valid_ids, {id: name}) for the project, cached."""
    with _project_versions_lock:
        if project in _project_versions_cache:
            return _project_versions_cache[project], _project_version_names_cache[project]

    data, status = core.jira_get(f"api/3/project/{project}/versions")
    valid_ids = set()
    id_to_name = {}
    if status == 200 and data:
        for v in data:
            vid = _norm_id(v.get("id"))
            if vid:
                valid_ids.add(vid)
                id_to_name[vid] = v.get("name", "")
    else:
        core.thread_safe_print(f"   [WARN] Could not fetch versions for project {project} (HTTP {status})")

    with _project_versions_lock:
        _project_versions_cache[project] = valid_ids
        _project_version_names_cache[project] = id_to_name
    return valid_ids, id_to_name


# =============================================================================
# STEP 3 — RESOLVE WHICH ID TO WRITE
# =============================================================================

def resolve_fix_id(row, valid_version_ids):
    """
    Returns (resolved_id, resolution_source) or (None, "NEEDS_MANUAL_REVIEW").
    All comparisons go through _norm_id / _norm_id_list.
    """
    expected_id = _norm_id(row.get("expected_id"))
    id_mapping_id = _norm_id(row.get("id_mapping_id"))
    actual_ids = _norm_id_list(row.get("actual_ids"))

    # a) Excel's expected_id, if it's a real version in Jira right now
    if expected_id and expected_id in valid_version_ids:
        return expected_id, "EXCEL"

    # b) id_mapping's id, if it's a real version in Jira right now
    if id_mapping_id and id_mapping_id in valid_version_ids:
        return id_mapping_id, "ID_MAPPING_FALLBACK"

    # c) expected_id is already attached to the issue -> just drop extras
    if expected_id and expected_id in actual_ids:
        return expected_id, "ALREADY_PRESENT_EXCEL"

    # d) same, but for id_mapping's id
    if id_mapping_id and id_mapping_id in actual_ids:
        return id_mapping_id, "ALREADY_PRESENT_ID_MAPPING"

    # e) exactly one currently-attached id is a real, valid version
    #    (matches neither Excel nor id_mapping, but it's real -> keep it,
    #    drop the rest). If MORE than one attached id is valid, that's
    #    ambiguous -> fall through to manual review, don't guess.
    valid_actual = actual_ids & valid_version_ids
    if len(valid_actual) == 1:
        return next(iter(valid_actual)), "VALID_FROM_ACTUAL_DEDUPE"

    # f) can't safely resolve
    return None, "NEEDS_MANUAL_REVIEW"


# =============================================================================
# STEP 4 — APPLY + VERIFY ONE FIX
# =============================================================================

def fix_one(row, dry_run=False):
    out = dict(row)
    project = row.get("project")
    jira_key = row.get("jira_key")

    try:
        # FIX: Unpack the tuple returned by get_valid_version_ids
        valid_version_ids, id_to_name = get_valid_version_ids(project)
        resolved_id, source = resolve_fix_id(row, valid_version_ids)

        out["resolution_source"] = source
        out["resolved_id"] = resolved_id or ""

        if resolved_id is None:
            out["fix_write_status"] = "SKIPPED_NO_VALID_ID"
            out["fix_write_error"] = ""
            out["fix_verify_status"] = "SKIPPED"
            out["fix_verify_actual_ids"] = ""
            out["fix_verify_actual_names"] = ""
            return out

        # SKIP if it's already exactly right — no PUT needed at all
        actual_ids = _norm_id_list(row.get("actual_ids"))
        if actual_ids == {resolved_id}:
            out["resolution_source"] = "ALREADY_CORRECT"
            out["fix_write_status"] = "SKIPPED_ALREADY_CORRECT"
            out["fix_write_error"] = ""
            out["fix_verify_status"] = "SKIPPED"
            out["fix_verify_actual_ids"] = row.get("actual_ids", "")
            out["fix_verify_actual_names"] = row.get("actual_names", "")
            return out

        if dry_run:
            out["fix_write_status"] = "DRY_RUN"
            out["fix_write_error"] = ""
            out["fix_verify_status"] = "SKIPPED"
            out["fix_verify_actual_ids"] = ""
            out["fix_verify_actual_names"] = ""
            return out

        payload = {"fields": {"fixVersions": [{"id": resolved_id}]}}
        try:
            data, status = core.jira_put(f"api/3/issue/{jira_key}", payload)
        except Exception as e:
            out["fix_write_status"] = "EXCEPTION"
            out["fix_write_error"] = str(e)
            out["fix_verify_status"] = "SKIPPED"
            out["fix_verify_actual_ids"] = ""
            out["fix_verify_actual_names"] = ""
            core.thread_safe_print(f"   [ERROR] {jira_key}: PUT exception - {str(e)}")
            return out

        if status not in (200, 204):
            out["fix_write_status"] = "PUT_FAILED"
            out["fix_write_error"] = f"HTTP {status}: {data}"
            out["fix_verify_status"] = "SKIPPED"
            out["fix_verify_actual_ids"] = ""
            out["fix_verify_actual_names"] = ""
            core.thread_safe_print(f"   [ERROR] {jira_key}: PUT failed HTTP {status} - {data}")
            return out

        out["fix_write_status"] = "PUT_OK"
        out["fix_write_error"] = ""

        actual = checker.fetch_actual_versions(jira_key)
        if actual is None:
            out["fix_verify_status"] = "VERIFY_FAILED"
            out["fix_verify_actual_ids"] = ""
            out["fix_verify_actual_names"] = ""
            core.thread_safe_print(f"   [ERROR] {jira_key}: Verification fetch failed after PUT")
            return out

        verify_ids = {_norm_id(v["id"]) for v in actual}
        out["fix_verify_actual_ids"] = ", ".join(v["id"] for v in actual)
        out["fix_verify_actual_names"] = ", ".join(v["name"] for v in actual)
        
        if verify_ids == {resolved_id}:
            out["fix_verify_status"] = "VERIFIED"
        else:
            out["fix_verify_status"] = "STILL_WRONG"
            core.thread_safe_print(
                f"   [ERROR] {jira_key}: Verification failed - expected {resolved_id}, "
                f"got {verify_ids}"
            )
        
        return out
        
    except Exception as e:
        # Catch any unexpected errors in the entire fix_one process
        out["resolution_source"] = "PROCESSING_ERROR"
        out["resolved_id"] = ""
        out["fix_write_status"] = "PROCESSING_ERROR"
        out["fix_write_error"] = str(e)
        out["fix_verify_status"] = "SKIPPED"
        out["fix_verify_actual_ids"] = ""
        out["fix_verify_actual_names"] = ""
        core.thread_safe_print(f"   [ERROR] {jira_key}: Unexpected error in fix_one - {str(e)}")
        return out


# =============================================================================
# STEP 5 — MAIN RUN
# =============================================================================

def run_fix(rows_to_fix, dry_run=False):
    if not rows_to_fix:
        print("\n  Nothing to fix. Exiting.")
        return []

    print("\n" + "-" * 70)
    print(f"  Fixing {len(rows_to_fix)} issue(s){' (DRY RUN - no writes)' if dry_run else ''}")
    print("-" * 70)

    results = []
    lock = Lock()
    error_count = 0
    error_lock = Lock()

    def _fix(row):
        nonlocal error_count
        fixed = fix_one(row, dry_run=dry_run)
        with lock:
            results.append(fixed)
            done_local = len(results)
        
        # Track errors
        is_error = (
            fixed.get("fix_write_status") in ("EXCEPTION", "PUT_FAILED", "PROCESSING_ERROR")
            or fixed.get("fix_verify_status") == "STILL_WRONG"
            or fixed.get("resolution_source") in ("NEEDS_MANUAL_REVIEW", "PROCESSING_ERROR")
        )
        
        if is_error:
            with error_lock:
                error_count += 1
        
        if done_local % 200 == 0:
            core.thread_safe_print(f"  Processed {done_local}/{len(rows_to_fix)}... (errors: {error_count})")
        return fixed

    workers = min(getattr(core, "VERSION_ASSIGN_MAX_WORKERS", 1), len(rows_to_fix))
    print(f"  Workers: {workers}\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_fix, row) for row in rows_to_fix]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as e:
                core.thread_safe_print(f"   [ERROR] Fix worker exception: {e}")
                with error_lock:
                    error_count += 1

    resolution_counts = Counter(r["resolution_source"] for r in results)
    write_counts = Counter(r["fix_write_status"] for r in results)
    verify_counts = Counter(r["fix_verify_status"] for r in results)

    print("\n  Resolution source summary:")
    for source in ["ALREADY_CORRECT", "EXCEL", "ID_MAPPING_FALLBACK",
                    "ALREADY_PRESENT_EXCEL", "ALREADY_PRESENT_ID_MAPPING",
                    "VALID_FROM_ACTUAL_DEDUPE", "NEEDS_MANUAL_REVIEW", "PROCESSING_ERROR"]:
        if resolution_counts.get(source):
            print(f"    {source:28s}: {resolution_counts[source]}")

    print("\n  Write summary:")
    for status in ["PUT_OK", "PUT_FAILED", "EXCEPTION", "SKIPPED_NO_VALID_ID",
                    "SKIPPED_ALREADY_CORRECT", "DRY_RUN", "PROCESSING_ERROR"]:
        if write_counts.get(status):
            print(f"    {status:26s}: {write_counts[status]}")

    print("\n  Verify summary:")
    for status in ["VERIFIED", "STILL_WRONG", "VERIFY_FAILED", "SKIPPED"]:
        if verify_counts.get(status):
            print(f"    {status:22s}: {verify_counts[status]}")

    # Print error summary
    if error_count > 0:
        print(f"\n  ⚠️  TOTAL ERRORS DETECTED: {error_count}")
        print("  See detailed error messages above and in the error reports below.")

    return results


def _row_to_report_dict(r):
    return {
        "Source ID": r.get("source_id", ""),
        "Jira Key": r.get("jira_key", ""),
        "Jira ID": r.get("jira_id", ""),
        "Title": r.get("title", ""),
        "Project": r.get("project", ""),
        "Original Status": r.get("status", ""),
        "Excel Expected ID": r.get("expected_id") or "",
        "Excel Expected Name": r.get("expected_name") or "",
        "id_mapping ID": r.get("id_mapping_id") or "",
        "Mapping Agreement": r.get("mapping_agreement") or "",
        "Pre-Fix Actual IDs": r.get("actual_ids", ""),
        "Pre-Fix Actual Names": r.get("actual_names", ""),
        "Resolution Source": r.get("resolution_source", ""),
        "Resolved ID Written": r.get("resolved_id", ""),
        "Write Status": r.get("fix_write_status", ""),
        "Write Error": r.get("fix_write_error", ""),
        "Verify Status": r.get("fix_verify_status", ""),
        "Post-Fix IDs": r.get("fix_verify_actual_ids", ""),
        "Post-Fix Names": r.get("fix_verify_actual_names", ""),
    }


def save_full_report(results):
    if not results:
        return
    json_path = os.path.join(core.MAPPING_DIR, "fix_from_check_duplicate.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\n   [OK] Full JSON saved -> {json_path}")

    rows = [_row_to_report_dict(r) for r in results]
    xlsx_path = os.path.join(core.MAPPING_DIR, "fix_from_check_duplicate.xlsx")
    df = pd.DataFrame(rows)
    df["_sort"] = df["Resolution Source"].map(lambda s: 0 if s == "NEEDS_MANUAL_REVIEW" else 9)
    df = df.sort_values("_sort").drop(columns="_sort")
    df.to_excel(xlsx_path, index=False)
    print(f"   [OK] Full Excel saved -> {xlsx_path}")


def is_still_unresolved(r):
    return r.get("resolution_source") == "NEEDS_MANUAL_REVIEW" or not (
        r.get("fix_write_status") in ("PUT_OK", "SKIPPED_ALREADY_CORRECT")
        and r.get("fix_verify_status") in ("VERIFIED", "SKIPPED")
    )


def save_error_report(results, ts):
    """Save a detailed report of all errors encountered."""
    errors = [r for r in results if (
        r.get("fix_write_status") in ("EXCEPTION", "PUT_FAILED", "PROCESSING_ERROR")
        or r.get("fix_verify_status") in ("STILL_WRONG", "VERIFY_FAILED")
        or r.get("resolution_source") in ("NEEDS_MANUAL_REVIEW", "PROCESSING_ERROR")
    )]
    
    if not errors:
        print("   [OK] No errors encountered - all issues processed successfully!")
        return errors

    print(f"\n  ⚠️  ERRORS DETECTED: {len(errors)} of {len(results)} issues had problems")

    os.makedirs(FIX_LOG_DIR, exist_ok=True)

    # Categorize errors
    error_categories = {
        "NEEDS_MANUAL_REVIEW": [],
        "PUT_FAILED": [],
        "EXCEPTION": [],
        "VERIFY_FAILED": [],
        "STILL_WRONG": [],
        "PROCESSING_ERROR": []
    }
    
    for r in errors:
        if r.get("resolution_source") == "NEEDS_MANUAL_REVIEW":
            error_categories["NEEDS_MANUAL_REVIEW"].append(r)
        elif r.get("fix_write_status") == "PUT_FAILED":
            error_categories["PUT_FAILED"].append(r)
        elif r.get("fix_write_status") in ("EXCEPTION", "PROCESSING_ERROR"):
            error_categories["EXCEPTION"].append(r)
        elif r.get("fix_verify_status") == "VERIFY_FAILED":
            error_categories["VERIFY_FAILED"].append(r)
        elif r.get("fix_verify_status") == "STILL_WRONG":
            error_categories["STILL_WRONG"].append(r)

    # Print error breakdown
    print("\n  Error breakdown:")
    for category, items in error_categories.items():
        if items:
            print(f"    {category:25s}: {len(items)}")

    # Save JSON
    json_path = os.path.join(FIX_LOG_DIR, f"errors_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(errors, f, indent=2)
    print(f"\n   [OK] Error JSON saved -> {json_path}")

    # Save Excel
    rows = [_row_to_report_dict(r) for r in errors]
    xlsx_path = os.path.join(FIX_LOG_DIR, f"errors_{ts}.xlsx")
    df = pd.DataFrame(rows)
    
    # Sort by error type for easier review
    error_priority = {
        "PROCESSING_ERROR": 0,
        "EXCEPTION": 1,
        "PUT_FAILED": 2,
        "STILL_WRONG": 3,
        "VERIFY_FAILED": 4,
        "NEEDS_MANUAL_REVIEW": 5
    }
    df["_sort"] = df.apply(
        lambda row: min(
            error_priority.get(row["Resolution Source"], 99),
            error_priority.get(row["Write Status"], 99),
            error_priority.get(row["Verify Status"], 99)
        ),
        axis=1
    )
    df = df.sort_values("_sort").drop(columns="_sort")
    df.to_excel(xlsx_path, index=False)
    print(f"   [OK] Error Excel saved -> {xlsx_path}")

    # Print detailed error samples
    print(f"\n  Detailed error samples (showing first 10 of each type):")
    for category, items in error_categories.items():
        if items:
            print(f"\n  {category} ({len(items)} total):")
            for r in items[:10]:
                jira_key = r.get('jira_key', 'UNKNOWN')
                error_msg = r.get('fix_write_error', '')
                expected = r.get('expected_id', '')
                actual = r.get('actual_ids', '')
                post_fix = r.get('fix_verify_actual_ids', '')
                
                if category == "NEEDS_MANUAL_REVIEW":
                    print(f"    {jira_key}: expected={expected!r}, actual={actual!r}, "
                          f"id_mapping={r.get('id_mapping_id', '')!r}")
                elif category in ("PUT_FAILED", "EXCEPTION"):
                    print(f"    {jira_key}: {error_msg}")
                elif category == "STILL_WRONG":
                    print(f"    {jira_key}: tried to set {r.get('resolved_id', '')!r}, "
                          f"but got {post_fix!r}")
                elif category == "VERIFY_FAILED":
                    print(f"    {jira_key}: PUT succeeded but could not verify")
            
            if len(items) > 10:
                print(f"    ... and {len(items) - 10} more (see full report)")

    return errors


def save_unresolved_report(results, ts):
    unresolved = [r for r in results if is_still_unresolved(r)]
    print(f"\n  Still unresolved after this run: {len(unresolved)} of {len(results)}")

    if not unresolved:
        print("   Nothing unresolved - skipping unresolved_issues report.")
        return unresolved

    os.makedirs(FIX_LOG_DIR, exist_ok=True)

    json_path = os.path.join(FIX_LOG_DIR, f"unresolved_issues_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(unresolved, f, indent=2)
    print(f"   [OK] Unresolved JSON saved -> {json_path}")

    rows = [_row_to_report_dict(r) for r in unresolved]
    xlsx_path = os.path.join(FIX_LOG_DIR, f"unresolved_issues_{ts}.xlsx")
    df = pd.DataFrame(rows)
    resolution_order = {"NEEDS_MANUAL_REVIEW": 0, "PROCESSING_ERROR": 1}
    df["_sort"] = df["Resolution Source"].map(lambda s: resolution_order.get(s, 9))
    df = df.sort_values("_sort").drop(columns="_sort")
    df.to_excel(xlsx_path, index=False)
    print(f"   [OK] Unresolved Excel saved -> {xlsx_path}")

    return unresolved


def main():
    parser = argparse.ArgumentParser(
        description="Fix wrong/duplicate Jira fixVersions directly from "
                     "check_duplicate_version.json (skips fix_duplicate_version.py)."
    )
    parser.add_argument("--projects", default="CEMIG,PLAT,EX",
                         help="Comma-separated Jira project keys to fix (default: CEMIG,PLAT,EX)")
    parser.add_argument("--input", default=None,
                         help="Path to check_duplicate_version.json "
                              "(default: ./mapping/check_duplicate_version.json)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Resolve + report only, do not PUT to Jira")
    args = parser.parse_args()

    log_path, log_file, original_stdout, original_stderr, ts = start_logging()

    try:
        target_projects = {p.strip().upper() for p in args.projects.split(",") if p.strip()}
        input_path = args.input or os.path.join(core.MAPPING_DIR, "check_duplicate_version.json")

        print("=" * 70)
        print("  FIX WRONG/DUPLICATE VERSIONS FROM check_duplicate_version.json")
        print("=" * 70)
        print(f"  Target projects: {sorted(target_projects)}")
        print(f"  Input:           {input_path}")
        print(f"  Dry run:         {args.dry_run}")
        print("=" * 70)

        core.start_rate_monitor()
        if not core.validate_all_accounts():
            core.stop_rate_monitor()
            return 1

        try:
            rows = load_rows(input_path)
            rows_to_fix = select_rows_to_fix(rows, target_projects)
            results = run_fix(rows_to_fix, dry_run=args.dry_run)
            save_full_report(results)
            
            # Save both error report and unresolved report
            errors = save_error_report(results, ts)
            unresolved = save_unresolved_report(results, ts)
            
            # Final status summary
            print("\n" + "=" * 70)
            success_count = len([r for r in results if 
                r.get("fix_write_status") in ("PUT_OK", "SKIPPED_ALREADY_CORRECT")
                and r.get("fix_verify_status") in ("VERIFIED", "SKIPPED")
            ])
            
            print(f"  FINAL SUMMARY:")
            print(f"    Total processed:        {len(results)}")
            print(f"    Successfully fixed:     {success_count}")
            print(f"    Errors encountered:     {len(errors) if errors else 0}")
            print(f"    Still unresolved:       {len(unresolved) if unresolved else 0}")
            
            if errors:
                print(f"\n  ⚠️  Please review error reports in {FIX_LOG_DIR}")
            
        finally:
            core.stop_rate_monitor()

        print("\n" + "=" * 70)
        print("  DONE")
        print(f"  Log file: {log_path}")
        print("=" * 70)
        
        # Return non-zero exit code if there were errors
        return 1 if errors else 0
        
    finally:
        stop_logging(log_file, original_stdout, original_stderr)


if __name__ == "__main__":
    exit(main())