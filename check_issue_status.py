# # #full data check
# # """
# # check_issue_status.py

# # READ-ONLY audit. Does NOT change anything in Jira.

# # For every issue in id_mapping.json:
# #     1. Get its jira_key straight from id_mapping["issues"][source_id]["jira_key"].
# #     2. Use that same source_id to look up the FULL record in issues.json
# #        (payload/issues.json), matched on "source_issue_id".
# #     3. From that record pull:
# #          - "jira_status"   -> reported here as PAYLOAD STATUS
# #                               (the status that was recorded at export /
# #                               migration time, sitting in the payload file —
# #                               NOT live)
# #          - payload.fields.customfield_13038  -> reported here as FS KEY
# #                               (the original source-system issue key)
# #     4. Fetch the issue's CURRENT status AND CREATOR live from Jira via the API ->
# #        reported here as JIRA STATUS / CREATOR ID / CREATOR NAME.
# #     5. Only issues that pass BOTH filters are kept in the output:
# #          - LIVE Jira status is "Open"                      (--status-filter)
# #          - Jira creator accountId is one of the allowed IDs (--creator-ids)
# #        Everything else is skipped.

# # Default allowed creators:
# #     712020:d01481c0-4b37-4216-8059-c31531b8ec4e
# #     712020:3782473e-fdca-40d2-822c-306a0cd8054f

# # Paths are taken from the SAME config bulk_load_to_jira.py (`core`) already
# # resolves from your .env / project setup — nothing needs to be passed on
# # the command line for a normal run. Specifically:
# #     id_mapping.json  -> core.id_mapping["issues"]   (already loaded in memory
# #                          by `core` at import time — no re-read from disk)
# #     issues.json       -> resolved from core.MAPPING_DIR by walking up to the
# #                          project data dir and into payload/issues.json,
# #                          unless core exposes a more direct path/loader
# #                          (checked automatically, in priority order — see
# #                          resolve_issues_path()).

# # Every path can still be overridden explicitly with --issues-path /
# # --id-mapping-path if your layout differs or you want to point at a
# # specific file.

# # Output (written next to id_mapping.json, i.e. into core.MAPPING_DIR):
# #     {core.MAPPING_DIR}/issue_status_report.xlsx
# #     {core.MAPPING_DIR}/issue_status_report.json

# # Columns in the Excel:
# #     Source ID, Jira Key, FS Key, Jira Status, Payload Status, Status Match,
# #     Creator Account ID, Creator Name

# # Usage:
# #     python check_issue_status.py
# #     python check_issue_status.py --status-filter Open
# #     python check_issue_status.py --status-filter ""                 # no status filter
# #     python check_issue_status.py --creator-ids ""                   # no creator filter
# #     python check_issue_status.py --creator-ids "712020:aaa,712020:bbb"   # custom creators
# #     python check_issue_status.py --issues-path /custom/path/issues.json
# # """

# # import os
# # import json
# # import argparse
# # import concurrent.futures
# # from threading import Lock
# # from collections import Counter

# # import pandas as pd

# # import bulk_load_to_jira as core  # reuse .env, jira_credentials, jira_get, thread_safe_print, id_mapping, MAPPING_DIR, etc.

# # FS_KEY_FIELD = "customfield_13038"

# # # Only issues created by one of these Jira accounts are kept in the report.
# # DEFAULT_CREATOR_IDS = [
# #     "712020:d01481c0-4b37-4216-8059-c31531b8ec4e",
# #     "712020:3782473e-fdca-40d2-822c-306a0cd8054f",
# # ]


# # def load_json_file(path):
# #     if not os.path.exists(path):
# #         raise FileNotFoundError(f"File not found: {path}")
# #     with open(path, "r", encoding="utf-8") as f:
# #         return json.load(f)


# # def resolve_issues_path(explicit_path=None):
# #     """
# #     Figures out where issues.json lives, without requiring any CLI flag,
# #     by reusing whatever `core` already knows about the project layout.
# #     Tried in order:
# #         1. --issues-path, if given explicitly
# #         2. core.ISSUES_JSON_PATH / core.PAYLOAD_ISSUES_PATH, if `core` defines one
# #         3. core.PAYLOAD_DIR / "issues.json", if `core` defines a payload dir
# #         4. sibling "payload/issues.json" next to core.MAPPING_DIR
# #            (i.e. {project}_data/mapping -> {project}_data/payload/issues.json)
# #     """
# #     if explicit_path:
# #         return explicit_path

# #     for attr in ("ISSUES_JSON_PATH", "PAYLOAD_ISSUES_PATH"):
# #         candidate = getattr(core, attr, None)
# #         if candidate:
# #             return candidate

# #     payload_dir = getattr(core, "PAYLOAD_DIR", None)
# #     if payload_dir:
# #         return os.path.join(payload_dir, "issues.json")

# #     # Fallback: sibling "payload" dir next to core.MAPPING_DIR
# #     project_dir = os.path.dirname(os.path.normpath(core.MAPPING_DIR))
# #     return os.path.join(project_dir, "payload", "issues.json")


# # def resolve_id_mapping_lookup(explicit_path=None):
# #     """
# #     Returns {source_id: {jira_key, ...}}.
# #     If no explicit path is given, reuses core.id_mapping — which `core`
# #     has ALREADY loaded from disk at import time (you saw this in the
# #     "Loaded existing mapping: N entries" line) — so we don't re-read or
# #     re-parse id_mapping.json a second time.
# #     """
# #     if explicit_path:
# #         mapping = load_json_file(explicit_path)
# #         issues_map = mapping.get("issues", mapping)
# #         print(f"  ✔ Loaded {len(issues_map)} issue(s) from {explicit_path}")
# #         return issues_map

# #     issues_map = core.id_mapping.get("issues", {})
# #     print(f"  ✔ Reusing {len(issues_map)} issue(s) already loaded in core.id_mapping")
# #     return issues_map


# # def build_issues_lookup(issues_path):
# #     """source_issue_id (str) -> full record from issues.json"""
# #     issues = load_json_file(issues_path)
# #     lookup = {}
# #     for rec in issues:
# #         sid = str(rec.get("source_issue_id"))
# #         if sid:
# #             lookup[sid] = rec
# #     print(f"  ✔ Loaded {len(lookup)} record(s) from {issues_path}")
# #     return lookup


# # def extract_fs_key(rec):
# #     return rec.get("payload", {}).get("fields", {}).get(FS_KEY_FIELD, "") or ""


# # def extract_payload_status(rec):
# #     return rec.get("jira_status", "") or ""


# # def fetch_live_issue_info(jira_key):
# #     """
# #     One API call that returns both the live status and the creator.
# #     Returns {"status": str, "creator_id": str, "creator_name": str},
# #     or None if the GET failed.
# #     """
# #     data, status_code = core.jira_get(f"api/3/issue/{jira_key}?fields=status,creator")
# #     if status_code != 200 or not data:
# #         return None

# #     fields = data.get("fields", {}) or {}
# #     status_name = (fields.get("status") or {}).get("name", "") or ""
# #     creator = fields.get("creator") or {}

# #     return {
# #         "status": status_name,
# #         "creator_id": creator.get("accountId", "") or "",
# #         "creator_name": creator.get("displayName", "") or "",
# #     }


# # def build_check_list(id_mapping_issues, issues_lookup):
# #     """
# #     Returns a list of dicts:
# #         {source_id, jira_key, fs_key, payload_status, found_payload}
# #     """
# #     check_list = []
# #     no_payload_count = 0

# #     for source_id, issue_info in id_mapping_issues.items():
# #         jira_key = issue_info.get("jira_key")
# #         rec = issues_lookup.get(str(source_id))

# #         if not rec:
# #             no_payload_count += 1
# #             check_list.append({
# #                 "source_id": source_id, "jira_key": jira_key,
# #                 "fs_key": "", "payload_status": "", "found_payload": False,
# #             })
# #             continue

# #         check_list.append({
# #             "source_id": source_id, "jira_key": jira_key,
# #             "fs_key": extract_fs_key(rec), "payload_status": extract_payload_status(rec),
# #             "found_payload": True,
# #         })

# #     if no_payload_count:
# #         print(f"  ⚠ {no_payload_count} issue(s) in id_mapping have no matching record in issues.json")

# #     return check_list


# # def run_check(check_list, status_filter, creator_ids):
# #     print(f"\n  {len(check_list)} issue(s) to check against live Jira status / creator\n")

# #     results = []
# #     lock = Lock()

# #     def _check_one(item):
# #         info = fetch_live_issue_info(item["jira_key"]) if item["jira_key"] else None
# #         live_status = info["status"] if info else None

# #         out = dict(item)
# #         out["jira_status"] = live_status if live_status is not None else "GET_FAILED"
# #         out["creator_id"] = info["creator_id"] if info else ""
# #         out["creator_name"] = info["creator_name"] if info else ""
# #         out["status_match"] = (
# #             "Yes" if live_status is not None and live_status.strip().lower() == item["payload_status"].strip().lower()
# #             else "No"
# #         )
# #         with lock:
# #             results.append(out)
# #             done = len(results)
# #         if done % 500 == 0:
# #             core.thread_safe_print(f"  Checked {done}/{len(check_list)}...")
# #         return out

# #     if check_list:
# #         workers = min(getattr(core, "VERSION_ASSIGN_MAX_WORKERS", 20), len(check_list))
# #         print(f"  Workers: {workers}\n")
# #         with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
# #             futures = [executor.submit(_check_one, item) for item in check_list]
# #             for future in concurrent.futures.as_completed(futures):
# #                 try:
# #                     future.result()
# #                 except Exception as e:
# #                     core.thread_safe_print(f"   ✖ Check worker exception: {e}")

# #     total_before_filter = len(results)

# #     # --- Breakdowns (before any filtering) ---
# #     status_breakdown = Counter(r["jira_status"] for r in results)
# #     print("\n  Live status breakdown (before filter):")
# #     for status, count in status_breakdown.most_common():
# #         print(f"    {status:20s}: {count}")

# #     creator_breakdown = Counter(r["creator_id"] or "(unknown)" for r in results)
# #     print("\n  Creator breakdown (before filter):")
# #     for creator, count in creator_breakdown.most_common():
# #         print(f"    {creator:50s}: {count}")

# #     # --- Filter 1: live status ---
# #     filtered = results
# #     if status_filter:
# #         filtered = [r for r in filtered if r["jira_status"].strip().lower() == status_filter.strip().lower()]
# #         print(f"\n  {len(filtered)} issue(s) have live Jira status = '{status_filter}'")

# #     # --- Filter 2: creator ---
# #     if creator_ids:
# #         allowed = {c.strip() for c in creator_ids if c and c.strip()}
# #         filtered = [r for r in filtered if r["creator_id"] in allowed]
# #         print(f"  {len(filtered)} of those were created by one of the {len(allowed)} allowed creator(s)")

# #     print(f"\n  {total_before_filter} issue(s) checked total")
# #     print(f"  {len(filtered)} issue(s) kept")
# #     print(f"  {total_before_filter - len(filtered)} issue(s) skipped (failed status and/or creator filter)")

# #     return filtered


# # def save_report(results, out_dir):
# #     os.makedirs(out_dir, exist_ok=True)

# #     json_path = os.path.join(out_dir, "issue_status_report.json")
# #     with open(json_path, "w", encoding="utf-8") as f:
# #         json.dump(results, f, indent=2)
# #     print(f"\n   ✔ JSON report saved -> {json_path}")

# #     rows = []
# #     for r in results:
# #         rows.append({
# #             "Source ID": r["source_id"],
# #             "Jira Key": r["jira_key"],
# #             "FS Key": r["fs_key"],
# #             "Jira Status": r["jira_status"],
# #             "Payload Status": r["payload_status"],
# #             "Status Match": r["status_match"],
# #             "Creator Account ID": r["creator_id"],
# #             "Creator Name": r["creator_name"],
# #         })

# #     xlsx_path = os.path.join(out_dir, "issue_status_report.xlsx")
# #     if rows:
# #         df = pd.DataFrame(rows)
# #         df.to_excel(xlsx_path, index=False)
# #         print(f"   ✔ Excel report saved -> {xlsx_path}")
# #     else:
# #         print("   (no rows matched the filters — nothing written to Excel)")


# # def main():
# #     parser = argparse.ArgumentParser(
# #         description="Compare live Jira status vs recorded payload status, filtered to currently-Open issues "
# #                     "created by specific Jira accounts"
# #     )
# #     parser.add_argument("--issues-path", default=None,
# #                          help="Override path to issues.json (default: auto-resolved from core's project config)")
# #     parser.add_argument("--id-mapping-path", default=None,
# #                          help="Override path to id_mapping.json (default: reuse core.id_mapping already loaded in memory)")
# #     parser.add_argument("--status-filter", default="Open",
# #                          help="Only keep issues whose LIVE Jira status equals this (default: Open). Pass \"\" for no filter.")
# #     parser.add_argument("--creator-ids", default=",".join(DEFAULT_CREATOR_IDS),
# #                          help="Comma-separated Jira creator accountIds to keep "
# #                               "(default: the two built-in accounts). Pass \"\" for no creator filter.")
# #     args = parser.parse_args()

# #     creator_ids = [c.strip() for c in args.creator_ids.split(",") if c.strip()]

# #     issues_path = resolve_issues_path(args.issues_path)
# #     out_dir = core.MAPPING_DIR

# #     print("=" * 70)
# #     print("  CHECK ISSUE STATUS (read-only)")
# #     print("=" * 70)
# #     print(f"  issues.json:     {issues_path}")
# #     print(f"  id_mapping:      {'core.id_mapping (already loaded)' if not args.id_mapping_path else args.id_mapping_path}")
# #     print(f"  Output dir:      {out_dir}")
# #     print(f"  Status filter:   {args.status_filter or '(none — keep all)'}")
# #     if creator_ids:
# #         print(f"  Creator filter:  {len(creator_ids)} allowed creator(s)")
# #         for c in creator_ids:
# #             print(f"                     - {c}")
# #     else:
# #         print("  Creator filter:  (none — keep all)")
# #     print("=" * 70)

# #     issues_lookup = build_issues_lookup(issues_path)
# #     id_mapping_issues = resolve_id_mapping_lookup(args.id_mapping_path)
# #     check_list = build_check_list(id_mapping_issues, issues_lookup)

# #     core.start_rate_monitor()
# #     if not core.validate_all_accounts():
# #         core.stop_rate_monitor()
# #         return 1

# #     try:
# #         results = run_check(check_list, args.status_filter, creator_ids)
# #         save_report(results, out_dir)
# #     finally:
# #         core.stop_rate_monitor()

# #     print("\n" + "=" * 70)
# #     print("  DONE")
# #     print("=" * 70)
# #     return 0


# # if __name__ == "__main__":
# #     exit(main())
# #specific type
# """
# check_issue_status.py

# READ-ONLY audit. Does NOT change anything in Jira.

# Instead of checking every issue in id_mapping.json one-by-one (hundreds of
# thousands of API calls), this script lets JIRA do the filtering with ONE JQL
# search, and only the matching issues come back:

#     "fr project[short text]" ~ "freshservice"
#     AND type = "Epic"
#     AND status = "Backlog"
#     AND creator in ("712020:d01481c0-...", "712020:3782473e-...")
#     ORDER BY created ASC

# For each issue returned by that search:
#     1. jira_key / issue type / status (live) / creator  -> straight from the JQL result.
#     2. The jira_key is mapped back to its source_id using id_mapping["issues"]
#        (reverse lookup on "jira_key").
#     3. That source_id is used to look up the FULL record in issues.json
#        (payload/issues.json), matched on "source_issue_id", to get:
#          - "jira_status"   -> reported as PAYLOAD STATUS
#                               (status recorded at export / migration time, NOT live)
#          - payload.fields.customfield_13038  -> reported as FS KEY
#                               (falls back to the live value from Jira if the
#                                issue isn't in issues.json)

# Paths are taken from the SAME config bulk_load_to_jira.py (`core`) already
# resolves from your .env / project setup — nothing needs to be passed on the
# command line for a normal run (see resolve_issues_path()).

# Output (written to {project_data_dir}/{project_key}_issue_status/issue_type_status/):
#     .../{project_key}_issue_status/issue_type_status/issue_status_report.xlsx
#     .../{project_key}_issue_status/issue_type_status/issue_status_report.json
#     ({project_data_dir} is the parent of core.MAPPING_DIR; override with --output-dir)

# Columns in the Excel:
#     Source ID, Jira Key, FS Key, Issue Type, Jira Status, Payload Status,
#     Status Match, Creator Account ID, Creator Name

# Usage:
#     python check_issue_status.py
#     python check_issue_status.py --status-filter ""                 # don't filter on status
#     python check_issue_status.py --fr-project ""                    # don't filter on the FR project field
#     python check_issue_status.py --creator-ids "712020:aaa,712020:bbb"
#     python check_issue_status.py --order-by "key DESC"
#     python check_issue_status.py --jql 'project = ABC AND type = Epic'   # full manual JQL override
# """

# import os
# import json
# import argparse
# from collections import Counter
# from urllib.parse import quote

# import pandas as pd

# import bulk_load_to_jira as core  # reuse .env, jira_credentials, jira_get, id_mapping, MAPPING_DIR, etc.

# FS_KEY_FIELD = "customfield_13038"

# # JQL name of the custom short-text field that holds the FR project value.
# FR_PROJECT_JQL_FIELD = '"fr project[short text]"'
# DEFAULT_FR_PROJECT = "freshservice"

# # Only issues created by one of these Jira accounts are kept in the report.
# DEFAULT_CREATOR_IDS = [
#     "712020:d01481c0-4b37-4216-8059-c31531b8ec4e",
#     "712020:3782473e-fdca-40d2-822c-306a0cd8054f",
# ]

# SEARCH_PAGE_SIZE = 100


# def load_json_file(path):
#     if not os.path.exists(path):
#         raise FileNotFoundError(f"File not found: {path}")
#     with open(path, "r", encoding="utf-8") as f:
#         return json.load(f)


# def resolve_issues_path(explicit_path=None):
#     """
#     Figures out where issues.json lives, without requiring any CLI flag,
#     by reusing whatever `core` already knows about the project layout.
#     Tried in order:
#         1. --issues-path, if given explicitly
#         2. core.ISSUES_JSON_PATH / core.PAYLOAD_ISSUES_PATH, if `core` defines one
#         3. core.PAYLOAD_DIR / "issues.json", if `core` defines a payload dir
#         4. sibling "payload/issues.json" next to core.MAPPING_DIR
#            (i.e. {project}_data/mapping -> {project}_data/payload/issues.json)
#     """
#     if explicit_path:
#         return explicit_path

#     for attr in ("ISSUES_JSON_PATH", "PAYLOAD_ISSUES_PATH"):
#         candidate = getattr(core, attr, None)
#         if candidate:
#             return candidate

#     payload_dir = getattr(core, "PAYLOAD_DIR", None)
#     if payload_dir:
#         return os.path.join(payload_dir, "issues.json")

#     # Fallback: sibling "payload" dir next to core.MAPPING_DIR
#     project_dir = os.path.dirname(os.path.normpath(core.MAPPING_DIR))
#     return os.path.join(project_dir, "payload", "issues.json")


# def resolve_project_key():
#     """
#     Finds the project key, reusing whatever `core` already knows.
#     Tried in order:
#         1. core.PROJECT_KEY / core.JIRA_PROJECT_KEY / core.TARGET_PROJECT_KEY / core.project_key
#         2. derived from the "{project_key}_data" folder that contains core.MAPPING_DIR
#     """
#     for attr in ("PROJECT_KEY", "JIRA_PROJECT_KEY", "TARGET_PROJECT_KEY", "project_key"):
#         val = getattr(core, attr, None)
#         if val and isinstance(val, str):
#             return val

#     project_dir_name = os.path.basename(os.path.dirname(os.path.normpath(core.MAPPING_DIR)))
#     if project_dir_name.endswith("_data"):
#         return project_dir_name[: -len("_data")]
#     return project_dir_name


# def resolve_output_dir(explicit_path=None):
#     """
#     Output folder layout:
#         {project_data_dir}/{project_key}_issue_status/issue_type_status/
#     where {project_data_dir} is the parent of core.MAPPING_DIR.
#     --output-dir overrides the whole path.
#     """
#     if explicit_path:
#         return explicit_path

#     project_key = resolve_project_key()
#     project_dir = os.path.dirname(os.path.normpath(core.MAPPING_DIR))
#     return os.path.join(project_dir, f"{project_key}_issue_status", "issue_type_status")


# def resolve_id_mapping_lookup(explicit_path=None):
#     """
#     Returns {source_id: {jira_key, ...}}.
#     If no explicit path is given, reuses core.id_mapping — which `core`
#     has ALREADY loaded from disk at import time — so we don't re-read or
#     re-parse id_mapping.json a second time.
#     """
#     if explicit_path:
#         mapping = load_json_file(explicit_path)
#         issues_map = mapping.get("issues", mapping)
#         print(f"  ✔ Loaded {len(issues_map)} issue(s) from {explicit_path}")
#         return issues_map

#     issues_map = core.id_mapping.get("issues", {})
#     print(f"  ✔ Reusing {len(issues_map)} issue(s) already loaded in core.id_mapping")
#     return issues_map


# def build_issues_lookup(issues_path):
#     """source_issue_id (str) -> full record from issues.json"""
#     issues = load_json_file(issues_path)
#     lookup = {}
#     for rec in issues:
#         sid = str(rec.get("source_issue_id"))
#         if sid:
#             lookup[sid] = rec
#     print(f"  ✔ Loaded {len(lookup)} record(s) from {issues_path}")
#     return lookup


# def extract_fs_key(rec):
#     return rec.get("payload", {}).get("fields", {}).get(FS_KEY_FIELD, "") or ""


# def extract_payload_status(rec):
#     return rec.get("jira_status", "") or ""


# # --------------------------------------------------------------------------
# # JQL
# # --------------------------------------------------------------------------
# def _jql_quote(value):
#     """Wrap a value in double quotes for JQL, escaping backslashes and quotes."""
#     return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# def build_jql(fr_project, issue_type, status, creator_ids, order_by):
#     clauses = []
#     if fr_project:
#         clauses.append(f"{FR_PROJECT_JQL_FIELD} ~ {_jql_quote(fr_project)}")
#     if issue_type:
#         clauses.append(f"type = {_jql_quote(issue_type)}")
#     if status:
#         clauses.append(f"status = {_jql_quote(status)}")
#     if creator_ids:
#         clauses.append("creator in (" + ", ".join(_jql_quote(c) for c in creator_ids) + ")")

#     if not clauses:
#         raise ValueError(
#             "All filters are empty — Jira does not allow an unbounded JQL search. "
#             "Provide at least one filter, or pass --jql."
#         )

#     jql = " AND ".join(clauses)
#     if order_by:
#         jql += f" ORDER BY {order_by}"
#     return jql


# def search_issues(jql):
#     """
#     Runs the JQL against Jira (enhanced search, token-paginated) and returns
#     the raw list of issue dicts. Only the fields we need are requested.
#     """
#     fields = f"status,issuetype,creator,{FS_KEY_FIELD}"
#     issues = []
#     token = None
#     page = 0

#     while True:
#         path = (
#             f"api/3/search/jql?jql={quote(jql, safe='')}"
#             f"&fields={fields}&maxResults={SEARCH_PAGE_SIZE}"
#         )
#         if token:
#             path += f"&nextPageToken={quote(token, safe='')}"

#         data, status_code = core.jira_get(path)
#         if status_code != 200 or not data:
#             raise RuntimeError(f"JQL search failed (HTTP {status_code}): {data}")

#         batch = data.get("issues", []) or []
#         issues.extend(batch)
#         page += 1
#         print(f"  Page {page}: fetched {len(batch)} issue(s) (total {len(issues)})")

#         token = data.get("nextPageToken")
#         if not token or data.get("isLast") is True:
#             break

#     return issues


# # --------------------------------------------------------------------------
# # Build report rows
# # --------------------------------------------------------------------------
# def build_results(jira_issues, id_mapping_issues, issues_lookup):
#     # Reverse lookup: jira_key -> source_id
#     key_to_source = {}
#     for source_id, info in id_mapping_issues.items():
#         jira_key = (info or {}).get("jira_key")
#         if jira_key:
#             key_to_source[jira_key] = source_id

#     results = []
#     not_in_mapping = 0
#     no_payload = 0

#     for issue in jira_issues:
#         jira_key = issue.get("key", "")
#         fields = issue.get("fields") or {}

#         jira_status = (fields.get("status") or {}).get("name", "") or ""
#         issue_type = (fields.get("issuetype") or {}).get("name", "") or ""
#         creator = fields.get("creator") or {}

#         source_id = key_to_source.get(jira_key, "")
#         rec = issues_lookup.get(str(source_id)) if source_id != "" else None

#         if source_id == "":
#             not_in_mapping += 1
#         elif rec is None:
#             no_payload += 1

#         payload_status = extract_payload_status(rec) if rec else ""
#         fs_key = extract_fs_key(rec) if rec else (fields.get(FS_KEY_FIELD) or "")

#         if rec:
#             status_match = "Yes" if jira_status.strip().lower() == payload_status.strip().lower() else "No"
#         else:
#             status_match = "N/A"

#         results.append({
#             "source_id": source_id,
#             "jira_key": jira_key,
#             "fs_key": fs_key,
#             "issue_type": issue_type,
#             "jira_status": jira_status,
#             "payload_status": payload_status,
#             "status_match": status_match,
#             "creator_id": creator.get("accountId", "") or "",
#             "creator_name": creator.get("displayName", "") or "",
#         })

#     if not_in_mapping:
#         print(f"  ⚠ {not_in_mapping} issue(s) from Jira are not in id_mapping (Payload Status left blank)")
#     if no_payload:
#         print(f"  ⚠ {no_payload} issue(s) are in id_mapping but have no record in issues.json (Payload Status left blank)")

#     return results


# def print_breakdowns(results):
#     print(f"\n  {len(results)} issue(s) returned by the JQL search")

#     print("\n  Jira status breakdown:")
#     for status, count in Counter(r["jira_status"] for r in results).most_common():
#         print(f"    {status:20s}: {count}")

#     print("\n  Creator breakdown:")
#     for creator, count in Counter(r["creator_id"] or "(unknown)" for r in results).most_common():
#         print(f"    {creator:50s}: {count}")

#     print("\n  Status match (Jira vs payload):")
#     for match, count in Counter(r["status_match"] for r in results).most_common():
#         print(f"    {match:20s}: {count}")


# def save_report(results, out_dir):
#     os.makedirs(out_dir, exist_ok=True)

#     json_path = os.path.join(out_dir, "issue_status_report.json")
#     with open(json_path, "w", encoding="utf-8") as f:
#         json.dump(results, f, indent=2)
#     print(f"\n   ✔ JSON report saved -> {json_path}")

#     rows = []
#     for r in results:
#         rows.append({
#             "Source ID": r["source_id"],
#             "Jira Key": r["jira_key"],
#             "FS Key": r["fs_key"],
#             "Issue Type": r["issue_type"],
#             "Jira Status": r["jira_status"],
#             "Payload Status": r["payload_status"],
#             "Status Match": r["status_match"],
#             "Creator Account ID": r["creator_id"],
#             "Creator Name": r["creator_name"],
#         })

#     xlsx_path = os.path.join(out_dir, "issue_status_report.xlsx")
#     if rows:
#         df = pd.DataFrame(rows)
#         df.to_excel(xlsx_path, index=False)
#         print(f"   ✔ Excel report saved -> {xlsx_path}")
#     else:
#         print("   (JQL returned no issues — nothing written to Excel)")


# def main():
#     parser = argparse.ArgumentParser(
#         description="Fetch Backlog Epics for the FR project created by specific Jira accounts (single JQL search) "
#                     "and compare live Jira status vs recorded payload status"
#     )
#     parser.add_argument("--issues-path", default=None,
#                          help="Override path to issues.json (default: auto-resolved from core's project config)")
#     parser.add_argument("--id-mapping-path", default=None,
#                          help="Override path to id_mapping.json (default: reuse core.id_mapping already loaded in memory)")
#     parser.add_argument("--fr-project", default=DEFAULT_FR_PROJECT,
#                          help=f"Value for the {FR_PROJECT_JQL_FIELD} ~ filter (default: {DEFAULT_FR_PROJECT}). Pass \"\" for no filter.")
#     parser.add_argument("--issue-type-filter", default="Epic",
#                          help="Issue type to search for (default: Epic). Pass \"\" for no filter.")
#     parser.add_argument("--status-filter", default="Backlog",
#                          help="Jira status to search for (default: Backlog). Pass \"\" for no filter.")
#     parser.add_argument("--creator-ids", default=",".join(DEFAULT_CREATOR_IDS),
#                          help="Comma-separated Jira creator accountIds (default: the two built-in accounts). "
#                               "Pass \"\" for no creator filter.")
#     parser.add_argument("--order-by", default="created ASC",
#                          help="JQL ORDER BY clause, without the words ORDER BY (default: created ASC). Pass \"\" for none.")
#     parser.add_argument("--jql", default=None,
#                          help="Full JQL override — if given, all the filter options above are ignored.")
#     parser.add_argument("--output-dir", default=None,
#                          help="Override output folder (default: {project_data_dir}/{project_key}_issue_status/issue_type_status)")
#     args = parser.parse_args()

#     creator_ids = [c.strip() for c in args.creator_ids.split(",") if c.strip()]

#     jql = args.jql or build_jql(
#         fr_project=args.fr_project,
#         issue_type=args.issue_type_filter,
#         status=args.status_filter,
#         creator_ids=creator_ids,
#         order_by=args.order_by,
#     )

#     issues_path = resolve_issues_path(args.issues_path)
#     out_dir = resolve_output_dir(args.output_dir)

#     print("=" * 70)
#     print("  CHECK ISSUE STATUS (read-only, JQL search)")
#     print("=" * 70)
#     print(f"  issues.json:     {issues_path}")
#     print(f"  id_mapping:      {'core.id_mapping (already loaded)' if not args.id_mapping_path else args.id_mapping_path}")
#     print(f"  Project key:     {resolve_project_key()}")
#     print(f"  Output dir:      {out_dir}")
#     print(f"  JQL:             {jql}")
#     print("=" * 70)

#     issues_lookup = build_issues_lookup(issues_path)
#     id_mapping_issues = resolve_id_mapping_lookup(args.id_mapping_path)

#     core.start_rate_monitor()
#     if not core.validate_all_accounts():
#         core.stop_rate_monitor()
#         return 1

#     try:
#         print("\n  Searching Jira...\n")
#         jira_issues = search_issues(jql)
#         results = build_results(jira_issues, id_mapping_issues, issues_lookup)
#         print_breakdowns(results)
#         save_report(results, out_dir)
#     finally:
#         core.stop_rate_monitor()

#     print("\n" + "=" * 70)
#     print("  DONE")
#     print("=" * 70)
#     return 0


# if __name__ == "__main__":
#     exit(main())
"""
check_issue_status.py

READ-ONLY audit. Does NOT change anything in Jira.

Instead of checking every issue in id_mapping.json one-by-one (hundreds of
thousands of API calls), this script lets JIRA do the filtering with ONE JQL
search, and only the matching issues come back:

    "fr project[short text]" ~ "freshservice"
    AND type in ("Deployment Ticket", "Copy Review", "Dependency Request", "Tech Action Item",
                 "Initiative", "PR Intake Bot", "Iteration Bug", "Sub-task", "Ticket Intake Bot",
                 "VQA", "Testcase Review Sub-task", "Spike", "Bug", "Security Bug", "Task",
                 "Tech Story", "User Story")
    AND status = "Open"
    AND creator in ("712020:d01481c0-...", "712020:3782473e-...")
    ORDER BY created ASC

For each issue returned by that search:
    1. jira_key / issue type / status (live) / creator / created date -> straight from the JQL result.
    2. The issue's changelog is fetched to find when its status LAST changed
       (the most recent history entry where the "status" field was updated).
       If the status has never changed since creation, the status-changed
       date is treated as the same as the created date.
    3. LAST UPDATE (Yes/No): compares the CREATED date and the STATUS CHANGED
       date (day only, ignoring time-of-day). Same day -> Yes (status is
       still whatever the migration set it to, untouched). Different day ->
       No (someone changed the status after creation).
    4. The jira_key is mapped back to its source_id using id_mapping["issues"]
       (reverse lookup on "jira_key").
    5. That source_id is used to look up the FULL record in issues.json
       (payload/issues.json), matched on "source_issue_id", to get:
         - "jira_status"   -> reported as PAYLOAD STATUS
                              (status recorded at export / migration time, NOT live)
         - payload.fields.customfield_13038  -> reported as FS KEY
                              (falls back to the live value from Jira if the
                               issue isn't in issues.json)

Paths are taken from the SAME config bulk_load_to_jira.py (`core`) already
resolves from your .env / project setup — nothing needs to be passed on the
command line for a normal run (see resolve_issues_path()).

Output (written to {project_data_dir}/{project_key}_issue_status/issue_type_status/):
    .../{project_key}_issue_status/issue_type_status/issue_status_report.xlsx
    .../{project_key}_issue_status/issue_type_status/issue_status_report.json
    ({project_data_dir} is the parent of core.MAPPING_DIR; override with --output-dir)

Columns in the Excel:
    Source ID, Jira Key, FS Key, Issue Type, Jira Status, Payload Status,
    Status Match, Creator Account ID, Creator Name, Created Date,
    Status Changed Date, Last Update

Usage:
    python check_issue_status.py
    python check_issue_status.py --status-filter ""                 # don't filter on status
    python check_issue_status.py --fr-project ""                    # don't filter on the FR project field
    python check_issue_status.py --issue-type-filter "Bug,Task"     # narrow to specific type(s)
    python check_issue_status.py --creator-ids "712020:aaa,712020:bbb"
    python check_issue_status.py --order-by "key DESC"
    python check_issue_status.py --jql 'project = ABC AND type = Epic'   # full manual JQL override
"""

import os
import json
import argparse
import concurrent.futures
from threading import Lock
from collections import Counter
from urllib.parse import quote

import pandas as pd

import bulk_load_to_jira as core  # reuse .env, jira_credentials, jira_get, id_mapping, MAPPING_DIR, etc.

FS_KEY_FIELD = "customfield_13038"

# JQL name of the custom short-text field that holds the FR project value.
FR_PROJECT_JQL_FIELD = '"fr project[short text]"'
DEFAULT_FR_PROJECT = "freshservice"

# Only issues created by one of these Jira accounts are kept in the report.
DEFAULT_CREATOR_IDS = [
    "712020:d01481c0-4b37-4216-8059-c31531b8ec4e",
    "712020:3782473e-fdca-40d2-822c-306a0cd8054f",
]

SEARCH_PAGE_SIZE = 100


def load_json_file(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_issues_path(explicit_path=None):
    """
    Figures out where issues.json lives, without requiring any CLI flag,
    by reusing whatever `core` already knows about the project layout.
    Tried in order:
        1. --issues-path, if given explicitly
        2. core.ISSUES_JSON_PATH / core.PAYLOAD_ISSUES_PATH, if `core` defines one
        3. core.PAYLOAD_DIR / "issues.json", if `core` defines a payload dir
        4. sibling "payload/issues.json" next to core.MAPPING_DIR
           (i.e. {project}_data/mapping -> {project}_data/payload/issues.json)
    """
    if explicit_path:
        return explicit_path

    for attr in ("ISSUES_JSON_PATH", "PAYLOAD_ISSUES_PATH"):
        candidate = getattr(core, attr, None)
        if candidate:
            return candidate

    payload_dir = getattr(core, "PAYLOAD_DIR", None)
    if payload_dir:
        return os.path.join(payload_dir, "issues.json")

    # Fallback: sibling "payload" dir next to core.MAPPING_DIR
    project_dir = os.path.dirname(os.path.normpath(core.MAPPING_DIR))
    return os.path.join(project_dir, "payload", "issues.json")


def resolve_project_key():
    """
    Finds the project key, reusing whatever `core` already knows.
    Tried in order:
        1. core.PROJECT_KEY / core.JIRA_PROJECT_KEY / core.TARGET_PROJECT_KEY / core.project_key
        2. derived from the "{project_key}_data" folder that contains core.MAPPING_DIR
    """
    for attr in ("PROJECT_KEY", "JIRA_PROJECT_KEY", "TARGET_PROJECT_KEY", "project_key"):
        val = getattr(core, attr, None)
        if val and isinstance(val, str):
            return val

    project_dir_name = os.path.basename(os.path.dirname(os.path.normpath(core.MAPPING_DIR)))
    if project_dir_name.endswith("_data"):
        return project_dir_name[: -len("_data")]
    return project_dir_name


def resolve_output_dir(explicit_path=None):
    """
    Output folder layout:
        {project_data_dir}/{project_key}_issue_status/issue_type_status/
    where {project_data_dir} is the parent of core.MAPPING_DIR.
    --output-dir overrides the whole path.
    """
    if explicit_path:
        return explicit_path

    project_key = resolve_project_key()
    project_dir = os.path.dirname(os.path.normpath(core.MAPPING_DIR))
    return os.path.join(project_dir, f"{project_key}_issue_status", "issue_type_status")


def resolve_id_mapping_lookup(explicit_path=None):
    """
    Returns {source_id: {jira_key, ...}}.
    If no explicit path is given, reuses core.id_mapping — which `core`
    has ALREADY loaded from disk at import time — so we don't re-read or
    re-parse id_mapping.json a second time.
    """
    if explicit_path:
        mapping = load_json_file(explicit_path)
        issues_map = mapping.get("issues", mapping)
        print(f"  ✔ Loaded {len(issues_map)} issue(s) from {explicit_path}")
        return issues_map

    issues_map = core.id_mapping.get("issues", {})
    print(f"  ✔ Reusing {len(issues_map)} issue(s) already loaded in core.id_mapping")
    return issues_map


def build_issues_lookup(issues_path):
    """source_issue_id (str) -> full record from issues.json"""
    issues = load_json_file(issues_path)
    lookup = {}
    for rec in issues:
        sid = str(rec.get("source_issue_id"))
        if sid:
            lookup[sid] = rec
    print(f"  ✔ Loaded {len(lookup)} record(s) from {issues_path}")
    return lookup


def extract_fs_key(rec):
    return rec.get("payload", {}).get("fields", {}).get(FS_KEY_FIELD, "") or ""


def extract_payload_status(rec):
    return rec.get("jira_status", "") or ""


# --------------------------------------------------------------------------
# JQL
# --------------------------------------------------------------------------
def _jql_quote(value):
    """Wrap a value in double quotes for JQL, escaping backslashes and quotes."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_jql(fr_project, issue_type, status, creator_ids, order_by):
    clauses = []
    if fr_project:
        clauses.append(f"{FR_PROJECT_JQL_FIELD} ~ {_jql_quote(fr_project)}")
    if issue_type:
        # issue_type may be a single type ("Bug") or a comma-separated list
        # ("Bug,Task,User Story") — either way it becomes a single JQL clause.
        types = [t.strip() for t in issue_type.split(",") if t.strip()]
        if len(types) == 1:
            clauses.append(f"type = {_jql_quote(types[0])}")
        elif len(types) > 1:
            clauses.append("type in (" + ", ".join(_jql_quote(t) for t in types) + ")")
    if status:
        clauses.append(f"status = {_jql_quote(status)}")
    if creator_ids:
        clauses.append("creator in (" + ", ".join(_jql_quote(c) for c in creator_ids) + ")")

    if not clauses:
        raise ValueError(
            "All filters are empty — Jira does not allow an unbounded JQL search. "
            "Provide at least one filter, or pass --jql."
        )

    jql = " AND ".join(clauses)
    if order_by:
        jql += f" ORDER BY {order_by}"
    return jql


def search_issues(jql):
    """
    Runs the JQL against Jira (enhanced search, token-paginated) and returns
    the raw list of issue dicts. Only the fields we need are requested.
    """
    fields = f"status,issuetype,creator,created,{FS_KEY_FIELD}"
    issues = []
    token = None
    page = 0

    while True:
        path = (
            f"api/3/search/jql?jql={quote(jql, safe='')}"
            f"&fields={fields}&maxResults={SEARCH_PAGE_SIZE}"
        )
        if token:
            path += f"&nextPageToken={quote(token, safe='')}"

        data, status_code = core.jira_get(path)
        if status_code != 200 or not data:
            raise RuntimeError(f"JQL search failed (HTTP {status_code}): {data}")

        batch = data.get("issues", []) or []
        issues.extend(batch)
        page += 1
        print(f"  Page {page}: fetched {len(batch)} issue(s) (total {len(issues)})")

        token = data.get("nextPageToken")
        if not token or data.get("isLast") is True:
            break

    return issues


def get_last_status_change_date(jira_key):
    """
    Returns the timestamp (Jira's raw string, e.g. '2026-09-07T10:15:00.000+0000')
    of the most recent changelog entry where the "status" field changed, or None
    if the status has never changed (no such entry found).
    """
    latest = None
    start = 0
    while True:
        data, code = core.jira_get(f"api/3/issue/{jira_key}/changelog?startAt={start}&maxResults=100")
        if code != 200 or not data:
            raise RuntimeError(f"[{jira_key}] Failed to fetch changelog (HTTP {code}): {data}")

        values = data.get("values", []) or []
        for hist in values:
            if any(item.get("field") == "status" for item in hist.get("items", []) or []):
                created = hist.get("created")
                if created and (latest is None or created > latest):
                    latest = created

        if not values or data.get("isLast", True):
            break
        start += len(values)

    return latest


def _date_only(timestamp):
    """'2026-09-07T10:15:00.000+0000' -> '2026-09-07'. Empty/None stays as-is."""
    return (timestamp or "")[:10]


# --------------------------------------------------------------------------
# Build report rows
# --------------------------------------------------------------------------
def build_results(jira_issues, id_mapping_issues, issues_lookup):
    # Reverse lookup: jira_key -> source_id
    key_to_source = {}
    for source_id, info in id_mapping_issues.items():
        jira_key = (info or {}).get("jira_key")
        if jira_key:
            key_to_source[jira_key] = source_id

    # --- Fetch each issue's last-status-change date in parallel ---
    # This is the slow part (one extra API call per issue for the changelog),
    # so it's spread across worker threads. core.jira_get is expected to be
    # thread-safe and to round-robin across your configured service accounts,
    # the same way it already does in bulk_load_to_jira's own threaded calls.
    printer = getattr(core, "thread_safe_print", print)
    lock = Lock()
    change_dates = {}
    done_count = 0

    def _fetch_one(jira_key):
        return jira_key, get_last_status_change_date(jira_key)

    keys = [issue.get("key", "") for issue in jira_issues if issue.get("key")]
    if keys:
        workers = min(getattr(core, "VERSION_ASSIGN_MAX_WORKERS", 20), len(keys))
        print(f"  Fetching changelogs for {len(keys)} issue(s) with {workers} worker(s)...\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_fetch_one, k) for k in keys]
            for future in concurrent.futures.as_completed(futures):
                try:
                    jira_key, last_change_ts = future.result()
                    with lock:
                        change_dates[jira_key] = last_change_ts
                        done_count += 1
                        if done_count % 500 == 0:
                            printer(f"  Checked {done_count}/{len(keys)}...")
                except Exception as e:
                    printer(f"   ✖ Changelog worker exception: {e}")

    results = []
    not_in_mapping = 0
    no_payload = 0

    for issue in jira_issues:
        jira_key = issue.get("key", "")
        fields = issue.get("fields") or {}

        jira_status = (fields.get("status") or {}).get("name", "") or ""
        issue_type = (fields.get("issuetype") or {}).get("name", "") or ""
        creator = fields.get("creator") or {}
        created_ts = fields.get("created", "") or ""

        last_change_ts = change_dates.get(jira_key)
        status_changed_ts = last_change_ts or created_ts  # never changed -> same as created
        last_update = "Yes" if _date_only(created_ts) == _date_only(status_changed_ts) else "No"

        source_id = key_to_source.get(jira_key, "")
        rec = issues_lookup.get(str(source_id)) if source_id != "" else None

        if source_id == "":
            not_in_mapping += 1
        elif rec is None:
            no_payload += 1

        payload_status = extract_payload_status(rec) if rec else ""
        fs_key = extract_fs_key(rec) if rec else (fields.get(FS_KEY_FIELD) or "")

        if rec:
            status_match = "Yes" if jira_status.strip().lower() == payload_status.strip().lower() else "No"
        else:
            status_match = "N/A"

        results.append({
            "source_id": source_id,
            "jira_key": jira_key,
            "fs_key": fs_key,
            "issue_type": issue_type,
            "jira_status": jira_status,
            "payload_status": payload_status,
            "status_match": status_match,
            "creator_id": creator.get("accountId", "") or "",
            "creator_name": creator.get("displayName", "") or "",
            "created_date": created_ts,
            "status_changed_date": status_changed_ts,
            "last_update": last_update,
        })

    if not_in_mapping:
        print(f"  ⚠ {not_in_mapping} issue(s) from Jira are not in id_mapping (Payload Status left blank)")
    if no_payload:
        print(f"  ⚠ {no_payload} issue(s) are in id_mapping but have no record in issues.json (Payload Status left blank)")

    return results


def print_breakdowns(results):
    print(f"\n  {len(results)} issue(s) returned by the JQL search")

    print("\n  Jira status breakdown:")
    for status, count in Counter(r["jira_status"] for r in results).most_common():
        print(f"    {status:20s}: {count}")

    print("\n  Creator breakdown:")
    for creator, count in Counter(r["creator_id"] or "(unknown)" for r in results).most_common():
        print(f"    {creator:50s}: {count}")

    print("\n  Status match (Jira vs payload):")
    for match, count in Counter(r["status_match"] for r in results).most_common():
        print(f"    {match:20s}: {count}")

    print("\n  Last Update (created == status-changed date):")
    for val, count in Counter(r["last_update"] for r in results).most_common():
        print(f"    {val:20s}: {count}")


def save_report(results, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    json_path = os.path.join(out_dir, "issue_status_report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\n   ✔ JSON report saved -> {json_path}")

    rows = []
    for r in results:
        rows.append({
            "Source ID": r["source_id"],
            "Jira Key": r["jira_key"],
            "FS Key": r["fs_key"],
            "Issue Type": r["issue_type"],
            "Jira Status": r["jira_status"],
            "Payload Status": r["payload_status"],
            "Status Match": r["status_match"],
            "Creator Account ID": r["creator_id"],
            "Creator Name": r["creator_name"],
            "Created Date": _date_only(r["created_date"]),
            "Status Changed Date": _date_only(r["status_changed_date"]),
            "Last Update": r["last_update"],
        })

    xlsx_path = os.path.join(out_dir, "issue_status_report.xlsx")
    if rows:
        df = pd.DataFrame(rows)
        df.to_excel(xlsx_path, index=False)
        print(f"   ✔ Excel report saved -> {xlsx_path}")
    else:
        print("   (JQL returned no issues — nothing written to Excel)")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Backlog Epics for the FR project created by specific Jira accounts (single JQL search) "
                    "and compare live Jira status vs recorded payload status"
    )
    parser.add_argument("--issues-path", default=None,
                         help="Override path to issues.json (default: auto-resolved from core's project config)")
    parser.add_argument("--id-mapping-path", default=None,
                         help="Override path to id_mapping.json (default: reuse core.id_mapping already loaded in memory)")
    parser.add_argument("--fr-project", default=DEFAULT_FR_PROJECT,
                         help=f"Value for the {FR_PROJECT_JQL_FIELD} ~ filter (default: {DEFAULT_FR_PROJECT}). Pass \"\" for no filter.")
    parser.add_argument(
        "--issue-type-filter",
        default=(
            "Deployment Ticket,Copy Review,Dependency Request,Tech Action Item,Initiative,"
            "PR Intake Bot,Iteration Bug,Sub-task,Ticket Intake Bot,VQA,"
            "Testcase Review Sub-task,Spike,Bug,Security Bug,Task,Tech Story,User Story"
        ),
        help="Comma-separated Jira issue type(s) to search for (default: the 17 built-in types). "
             "Pass \"\" for no filter.",
    )
    parser.add_argument("--status-filter", default="Open",
                         help="Jira status to search for (default: Open). Pass \"\" for no filter.")
    parser.add_argument("--creator-ids", default=",".join(DEFAULT_CREATOR_IDS),
                         help="Comma-separated Jira creator accountIds (default: the two built-in accounts). "
                              "Pass \"\" for no creator filter.")
    parser.add_argument("--order-by", default="created ASC",
                         help="JQL ORDER BY clause, without the words ORDER BY (default: created ASC). Pass \"\" for none.")
    parser.add_argument("--jql", default=None,
                         help="Full JQL override — if given, all the filter options above are ignored.")
    parser.add_argument("--output-dir", default=None,
                         help="Override output folder (default: {project_data_dir}/{project_key}_issue_status/issue_type_status)")
    args = parser.parse_args()

    creator_ids = [c.strip() for c in args.creator_ids.split(",") if c.strip()]

    jql = args.jql or build_jql(
        fr_project=args.fr_project,
        issue_type=args.issue_type_filter,
        status=args.status_filter,
        creator_ids=creator_ids,
        order_by=args.order_by,
    )

    issues_path = resolve_issues_path(args.issues_path)
    out_dir = resolve_output_dir(args.output_dir)

    print("=" * 70)
    print("  CHECK ISSUE STATUS (read-only, JQL search)")
    print("=" * 70)
    print(f"  issues.json:     {issues_path}")
    print(f"  id_mapping:      {'core.id_mapping (already loaded)' if not args.id_mapping_path else args.id_mapping_path}")
    print(f"  Project key:     {resolve_project_key()}")
    print(f"  Output dir:      {out_dir}")
    print(f"  JQL:             {jql}")
    print("=" * 70)

    issues_lookup = build_issues_lookup(issues_path)
    id_mapping_issues = resolve_id_mapping_lookup(args.id_mapping_path)

    core.start_rate_monitor()
    if not core.validate_all_accounts():
        core.stop_rate_monitor()
        return 1

    try:
        print("\n  Searching Jira...\n")
        jira_issues = search_issues(jql)
        results = build_results(jira_issues, id_mapping_issues, issues_lookup)
        print_breakdowns(results)
        save_report(results, out_dir)
    finally:
        core.stop_rate_monitor()

    print("\n" + "=" * 70)
    print("  DONE")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    exit(main())