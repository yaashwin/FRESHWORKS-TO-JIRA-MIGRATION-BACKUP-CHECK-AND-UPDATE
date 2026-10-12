# # # # #!/usr/bin/env python3
# # # # """
# # # # Reads a TSV mapping file (FR Project / FR Subproject / target Squad, etc.)
# # # # and for each row, queries Jira for all issues matching:

# # # #     "FR Project[Short text]" ~ "<FR Project>" AND "FR Sub-Project[Short text]" ~ "<FR Subproject>"

# # # # This script ONLY fetches/prints issue keys per row for now.
# # # # No values are updated yet — that's a separate step once you confirm the
# # # # fetched issues look right.

# # # # ENV VARS REQUIRED:
# # # #     JIRA_URL          e.g. https://api.atlassian.com/ex/jira/<cloudId>
# # # #     JIRA_EMAIL
# # # #     JIRA_API_TOKEN
# # # #     (JIRA_ACCOUNT_ID not needed for search, kept for later update step)

# # # # USAGE:
# # # #     python3 fetch_jira_issues.py input.tsv
# # # #     python3 fetch_jira_issues.py input.tsv --auth basic   # force basic auth
# # # #     python3 fetch_jira_issues.py input.tsv --auth bearer  # force bearer auth
# # # # """

# # # # import os
# # # # import sys
# # # # import csv
# # # # import json
# # # # import time
# # # # import base64
# # # # import argparse
# # # # import requests

# # # # try:
# # # #     from dotenv import load_dotenv
# # # #     # Loads a .env file sitting in the current working directory (if present).
# # # #     # Does NOT override variables that are already set in the real shell env.
# # # #     load_dotenv()
# # # # except ImportError:
# # # #     pass  # dotenv not installed -- fall back to whatever's already exported

# # # # # ----------------------------------------------------------------------------
# # # # # Config from environment
# # # # # ----------------------------------------------------------------------------
# # # # JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
# # # # JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
# # # # JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

# # # # # Custom field NAMES as they appear in your example JQL.
# # # # # If Jira doesn't resolve these by name, replace with the customfield_XXXXX id.
# # # # FR_PROJECT_FIELD = '"FR Project[Short text]"'
# # # # FR_SUBPROJECT_FIELD = '"FR Sub-Project[Short text]"'

# # # # # NOTE: /rest/api/3/search is deprecated/removed by Atlassian (410 Gone).
# # # # # The replacement is /rest/api/3/search/jql, which uses nextPageToken-based
# # # # # pagination instead of startAt/total.
# # # # SEARCH_ENDPOINT = f"{JIRA_URL}/rest/api/3/search/jql"

# # # # # How many issues to pull per JQL (pagination handled below)
# # # # PAGE_SIZE = 100


# # # # def get_session(auth_mode: str) -> requests.Session:
# # # #     """
# # # #     api.atlassian.com/ex/jira/<cloudId> is the OAuth 2.0 (3LO) gateway.
# # # #     That gateway expects:  Authorization: Bearer <oauth_access_token>
# # # #     A classic Jira API token (email + token, Basic auth) does NOT work
# # # #     against api.atlassian.com — it only works against
# # # #     https://yoursite.atlassian.net directly.

# # # #     So: if JIRA_URL contains "api.atlassian.com", default to bearer.
# # # #     Otherwise default to basic (email + token).
# # # #     You can override with --auth basic|bearer.
# # # #     """
# # # #     session = requests.Session()

# # # #     if auth_mode == "bearer":
# # # #         session.headers.update({
# # # #             "Authorization": f"Bearer {JIRA_API_TOKEN}",
# # # #         })
# # # #     else:  # basic
# # # #         session.auth = (JIRA_EMAIL, JIRA_API_TOKEN)

# # # #     session.headers.update({
# # # #         "Accept": "application/json",
# # # #         "Content-Type": "application/json",
# # # #     })
# # # #     return session


# # # # def build_jql(fr_project: str, fr_subproject: str) -> str:
# # # #     fr_project = fr_project.replace('"', '\\"').strip()
# # # #     parts = [f'{FR_PROJECT_FIELD} ~ "{fr_project}"']
# # # #     fr_subproject = fr_subproject.strip()
# # # #     if fr_subproject:
# # # #         fr_subproject_escaped = fr_subproject.replace('"', '\\"')
# # # #         parts.append(f'{FR_SUBPROJECT_FIELD} ~ "{fr_subproject_escaped}"')
# # # #     return " AND ".join(parts)


# # # # def search_issues(session: requests.Session, jql: str):
# # # #     """
# # # #     Yield all issues (dicts) matching the JQL, paging through results using
# # # #     the new /rest/api/3/search/jql endpoint (token-based pagination —
# # # #     nextPageToken, NOT startAt).
# # # #     """
# # # #     next_page_token = None
# # # #     while True:
# # # #         payload = {
# # # #             "jql": jql,
# # # #             "maxResults": PAGE_SIZE,
# # # #             "fields": ["key", "summary"],
# # # #         }
# # # #         if next_page_token:
# # # #             payload["nextPageToken"] = next_page_token

# # # #         resp = session.post(SEARCH_ENDPOINT, data=json.dumps(payload))
# # # #         if resp.status_code == 401:
# # # #             raise RuntimeError(
# # # #                 "401 Unauthorized — wrong auth mode for this JIRA_URL. "
# # # #                 "Try re-running with --auth basic or --auth bearer."
# # # #             )
# # # #         if resp.status_code == 410:
# # # #             raise RuntimeError(
# # # #                 "410 Gone — this Jira site has fully removed the legacy "
# # # #                 "search endpoint. Double-check SEARCH_ENDPOINT is "
# # # #                 "/rest/api/3/search/jql (should already be the case)."
# # # #             )
# # # #         resp.raise_for_status()
# # # #         data = resp.json()

# # # #         issues = data.get("issues", [])
# # # #         for issue in issues:
# # # #             yield issue

# # # #         next_page_token = data.get("nextPageToken")
# # # #         # New endpoint signals "no more pages" by omitting nextPageToken,
# # # #         # or by returning isLast True (whichever the tenant implements).
# # # #         if not next_page_token or data.get("isLast") or not issues:
# # # #             break
# # # #         time.sleep(0.2)  # be gentle with the API


# # # # def main():
# # # #     parser = argparse.ArgumentParser()
# # # #     parser.add_argument("tsv_path", help="Path to the TSV mapping file")
# # # #     parser.add_argument(
# # # #         "--auth",
# # # #         choices=["basic", "bearer"],
# # # #         default="bearer" if "api.atlassian.com" in JIRA_URL else "basic",
# # # #         help="Auth mode against Jira (default inferred from JIRA_URL)",
# # # #     )
# # # #     parser.add_argument(
# # # #         "--limit-rows",
# # # #         type=int,
# # # #         default=None,
# # # #         help="Only process the first N rows (useful for testing)",
# # # #     )
# # # #     parser.add_argument(
# # # #         "--out",
# # # #         default="jira_issue_results.json",
# # # #         help="Where to write the collected results (JSON)",
# # # #     )
# # # #     args = parser.parse_args()

# # # #     if not JIRA_URL or not JIRA_API_TOKEN:
# # # #         sys.exit("ERROR: JIRA_URL / JIRA_API_TOKEN not set in environment.")

# # # #     session = get_session(args.auth)

# # # #     results = []
# # # #     with open(args.tsv_path, newline="", encoding="utf-8") as f:
# # # #         reader = csv.DictReader(f, delimiter="\t")
# # # #         for i, row in enumerate(reader):
# # # #             if args.limit_rows is not None and i >= args.limit_rows:
# # # #                 break

# # # #             fr_project = row.get("FR Project", "")
# # # #             fr_subproject = row.get("FR Subproject", "")

# # # #             if not fr_project:
# # # #                 continue

# # # #             jql = build_jql(fr_project, fr_subproject)
# # # #             print(f"\n[Row {i}] {fr_project} / {fr_subproject or '(none)'}")
# # # #             print(f"  JQL: {jql}")

# # # #             try:
# # # #                 issue_keys = [issue["key"] for issue in search_issues(session, jql)]
# # # #             except Exception as e:
# # # #                 print(f"  ERROR: {e}")
# # # #                 issue_keys = []
# # # #                 error = str(e)
# # # #             else:
# # # #                 error = None

# # # #             print(f"  -> {len(issue_keys)} issue(s): {issue_keys}")

# # # #             results.append({
# # # #                 "row_index": i,
# # # #                 "fr_project": fr_project,
# # # #                 "fr_subproject": fr_subproject,
# # # #                 "target_squad": row.get("to be updated Jira Squad", ""),
# # # #                 "current_squad": row.get("current squad name", ""),
# # # #                 "jql": jql,
# # # #                 "issue_keys": issue_keys,
# # # #                 "error": error,
# # # #             })

# # # #     with open(args.out, "w", encoding="utf-8") as out_f:
# # # #         json.dump(results, out_f, indent=2)

# # # #     print(f"\nDone. Wrote {len(results)} row results to {args.out}")


# # # # if __name__ == "__main__":
# # # #     main()
# # # #!/usr/bin/env python3
# # # """
# # # Reads a TSV mapping file (FR Project / FR Subproject / target Squad, etc.)
# # # and for each row, queries Jira for all issues matching:

# # #     "FR Project[Short text]" ~ "<FR Project>" AND "FR Sub-Project[Short text]" ~ "<FR Subproject>"

# # # This script fetches the APPROXIMATE ISSUE COUNT per row for now (not the
# # # full list of issue keys). No values are updated yet — that's a separate
# # # step once you confirm the counts look right.

# # # ENV VARS REQUIRED:
# # #     JIRA_URL          e.g. https://api.atlassian.com/ex/jira/<cloudId>
# # #     JIRA_EMAIL
# # #     JIRA_API_TOKEN
# # #     (JIRA_ACCOUNT_ID not needed for search, kept for later update step)

# # # USAGE:
# # #     python3 fetch_jira_issues.py input.tsv
# # #     python3 fetch_jira_issues.py input.tsv --auth basic   # force basic auth
# # #     python3 fetch_jira_issues.py input.tsv --auth bearer  # force bearer auth
# # # """

# # # import os
# # # import sys
# # # import csv
# # # import json
# # # import time
# # # import base64
# # # import argparse
# # # import requests

# # # try:
# # #     from dotenv import load_dotenv
# # #     # Loads a .env file sitting in the current working directory (if present).
# # #     # Does NOT override variables that are already set in the real shell env.
# # #     load_dotenv()
# # # except ImportError:
# # #     pass  # dotenv not installed -- fall back to whatever's already exported

# # # # ----------------------------------------------------------------------------
# # # # Config from environment
# # # # ----------------------------------------------------------------------------
# # # JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
# # # JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
# # # JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

# # # # Custom field NAMES as they appear in your example JQL.
# # # # If Jira doesn't resolve these by name, replace with the customfield_XXXXX id.
# # # FR_PROJECT_FIELD = '"FR Project[Short text]"'
# # # FR_SUBPROJECT_FIELD = '"FR Sub-Project[Short text]"'
# # # SQUAD_FIELD = '"Squad[Select List (cascading)]"'

# # # # NOTE: /rest/api/3/search is deprecated/removed by Atlassian (410 Gone).
# # # # The new /rest/api/3/search/jql endpoint no longer returns a "total" count
# # # # at all -- for just a count, Atlassian's dedicated endpoint is
# # # # /rest/api/3/search/approximate-count.
# # # COUNT_ENDPOINT = f"{JIRA_URL}/rest/api/3/search/approximate-count"


# # # def get_session(auth_mode: str) -> requests.Session:
# # #     """
# # #     api.atlassian.com/ex/jira/<cloudId> is the OAuth 2.0 (3LO) gateway.
# # #     That gateway expects:  Authorization: Bearer <oauth_access_token>
# # #     A classic Jira API token (email + token, Basic auth) does NOT work
# # #     against api.atlassian.com — it only works against
# # #     https://yoursite.atlassian.net directly.

# # #     So: if JIRA_URL contains "api.atlassian.com", default to bearer.
# # #     Otherwise default to basic (email + token).
# # #     You can override with --auth basic|bearer.
# # #     """
# # #     session = requests.Session()

# # #     if auth_mode == "bearer":
# # #         session.headers.update({
# # #             "Authorization": f"Bearer {JIRA_API_TOKEN}",
# # #         })
# # #     else:  # basic
# # #         session.auth = (JIRA_EMAIL, JIRA_API_TOKEN)

# # #     session.headers.update({
# # #         "Accept": "application/json",
# # #         "Content-Type": "application/json",
# # #     })
# # #     return session


# # # def build_jql(fr_project: str, fr_subproject: str, squad: str = "") -> str:
# # #     fr_project = fr_project.replace('"', '\\"').strip()
# # #     parts = [f'{FR_PROJECT_FIELD} ~ "{fr_project}"']

# # #     fr_subproject = fr_subproject.strip()
# # #     if fr_subproject:
# # #         fr_subproject_escaped = fr_subproject.replace('"', '\\"')
# # #         parts.append(f'{FR_SUBPROJECT_FIELD} ~ "{fr_subproject_escaped}"')

# # #     squad = squad.strip()
# # #     if squad:
# # #         squad_escaped = squad.replace('"', '\\"')
# # #         parts.append(f'{SQUAD_FIELD} = "{squad_escaped}"')

# # #     return " AND ".join(parts)


# # # def get_issue_count(session: requests.Session, jql: str) -> int:
# # #     """
# # #     Return the approximate count of issues matching the JQL, using
# # #     /rest/api/3/search/approximate-count. Single call, no pagination.
# # #     """
# # #     payload = {"jql": jql}
# # #     resp = session.post(COUNT_ENDPOINT, data=json.dumps(payload))
# # #     if resp.status_code == 401:
# # #         raise RuntimeError(
# # #             "401 Unauthorized — wrong auth mode for this JIRA_URL. "
# # #             "Try re-running with --auth basic or --auth bearer."
# # #         )
# # #     if resp.status_code == 410:
# # #         raise RuntimeError(
# # #             "410 Gone — this Jira site has fully removed the legacy "
# # #             "search endpoint. Double-check COUNT_ENDPOINT is "
# # #             "/rest/api/3/search/approximate-count."
# # #         )
# # #     resp.raise_for_status()
# # #     data = resp.json()
# # #     return data.get("count", 0)


# # # def main():
# # #     parser = argparse.ArgumentParser()
# # #     parser.add_argument("tsv_path", help="Path to the TSV mapping file")
# # #     parser.add_argument(
# # #         "--auth",
# # #         choices=["basic", "bearer"],
# # #         default="bearer" if "api.atlassian.com" in JIRA_URL else "basic",
# # #         help="Auth mode against Jira (default inferred from JIRA_URL)",
# # #     )
# # #     parser.add_argument(
# # #         "--limit-rows",
# # #         type=int,
# # #         default=None,
# # #         help="Only process the first N rows (useful for testing)",
# # #     )
# # #     parser.add_argument(
# # #         "--out",
# # #         default="jira_issue_results.json",
# # #         help="Where to write the collected results (JSON)",
# # #     )
# # #     args = parser.parse_args()

# # #     if not JIRA_URL or not JIRA_API_TOKEN:
# # #         sys.exit("ERROR: JIRA_URL / JIRA_API_TOKEN not set in environment.")

# # #     session = get_session(args.auth)

# # #     results = []
# # #     with open(args.tsv_path, newline="", encoding="utf-8") as f:
# # #         reader = csv.DictReader(f, delimiter="\t")
# # #         for i, row in enumerate(reader):
# # #             if args.limit_rows is not None and i >= args.limit_rows:
# # #                 break

# # #             fr_project = row.get("FR Project", "")
# # #             fr_subproject = row.get("FR Subproject", "")
# # #             squad = row.get("current squad name", "")

# # #             if not fr_project:
# # #                 continue

# # #             jql = build_jql(fr_project, fr_subproject, squad)
# # #             print(f"\n[Row {i}] {fr_project} / {fr_subproject or '(none)'} / {squad or '(none)'}")
# # #             print(f"  JQL: {jql}")

# # #             try:
# # #                 count = get_issue_count(session, jql)
# # #                 error = None
# # #             except Exception as e:
# # #                 print(f"  ERROR: {e}")
# # #                 count = None
# # #                 error = str(e)

# # #             print(f"  -> {count} issue(s)")

# # #             results.append({
# # #                 "row_index": i,
# # #                 "fr_project": fr_project,
# # #                 "fr_subproject": fr_subproject,
# # #                 "target_squad": row.get("to be updated Jira Squad", ""),
# # #                 "current_squad": row.get("current squad name", ""),
# # #                 "jql": jql,
# # #                 "issue_count": count,
# # #                 "error": error,
# # #             })

# # #     with open(args.out, "w", encoding="utf-8") as out_f:
# # #         json.dump(results, out_f, indent=2)

# # #     print(f"\nDone. Wrote {len(results)} row results to {args.out}")


# # # if __name__ == "__main__":
# # #     main()
# # #!/usr/bin/env python3
# # """
# # Reads a TSV mapping file (FR Project / FR Subproject / current squad /
# # target squad, etc.) and for each row:

# #   1. Builds a JQL filter:
# #        "FR Project[Short text]" ~ "<FR Project>"
# #        AND "FR Sub-Project[Short text]" ~ "<FR Subproject>"
# #        AND "Squad[Select List (cascading)]" = "<current squad name>"
# #   2. Finds all matching issues.
# #   3. For each issue, updates the Squad cascading-select field
# #      (customfield_10973) so that:
# #        - the PARENT value is left exactly as it currently is on the issue
# #          (never touched/overwritten)
# #        - the CHILD value is set to the row's "to be updated Jira Squad"

# # DEFAULT MODE IS DRY-RUN: it will print every planned change but will NOT
# # call PUT on any issue unless you pass --apply.

# # ENV VARS REQUIRED:
# #     JIRA_URL          e.g. https://api.atlassian.com/ex/jira/<cloudId>
# #     JIRA_EMAIL
# #     JIRA_API_TOKEN
# #     (JIRA_ACCOUNT_ID not needed here)

# # USAGE:
# #     # Safe: prints planned changes, no writes
# #     python3 fetch_jira_issues.py input.tsv --limit-rows 1

# #     # Actually performs the updates
# #     python3 fetch_jira_issues.py input.tsv --limit-rows 1 --apply

# #     python3 fetch_jira_issues.py input.tsv --auth basic   # force basic auth
# #     python3 fetch_jira_issues.py input.tsv --auth bearer  # force bearer auth
# # """

# # import os
# # import sys
# # import csv
# # import json
# # import time
# # import base64
# # import argparse
# # import requests

# # try:
# #     from dotenv import load_dotenv
# #     # Loads a .env file sitting in the current working directory (if present).
# #     # Does NOT override variables that are already set in the real shell env.
# #     load_dotenv()
# # except ImportError:
# #     pass  # dotenv not installed -- fall back to whatever's already exported

# # # ----------------------------------------------------------------------------
# # # Config from environment
# # # ----------------------------------------------------------------------------
# # JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
# # JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
# # JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

# # # Custom field NAMES as they appear in your example JQL.
# # # If Jira doesn't resolve these by name, replace with the customfield_XXXXX id.
# # FR_PROJECT_FIELD = '"FR Project[Short text]"'
# # FR_SUBPROJECT_FIELD = '"FR Sub-Project[Short text]"'
# # SQUAD_FIELD = '"Squad[Select List (cascading)]"'

# # # The actual field ID used for reading/writing via the REST API (JQL can use
# # # the display name above, but issue field updates need the customfield_ id).
# # SQUAD_FIELD_ID = "customfield_10973"

# # # NOTE: /rest/api/3/search is deprecated/removed by Atlassian (410 Gone).
# # # The new /rest/api/3/search/jql endpoint no longer returns a "total" count
# # # at all -- for just a count, Atlassian's dedicated endpoint is
# # # /rest/api/3/search/approximate-count.
# # COUNT_ENDPOINT = f"{JIRA_URL}/rest/api/3/search/approximate-count"
# # SEARCH_ENDPOINT = f"{JIRA_URL}/rest/api/3/search/jql"


# # def get_session(auth_mode: str) -> requests.Session:
# #     """
# #     api.atlassian.com/ex/jira/<cloudId> is the OAuth 2.0 (3LO) gateway.
# #     That gateway expects:  Authorization: Bearer <oauth_access_token>
# #     A classic Jira API token (email + token, Basic auth) does NOT work
# #     against api.atlassian.com — it only works against
# #     https://yoursite.atlassian.net directly.

# #     So: if JIRA_URL contains "api.atlassian.com", default to bearer.
# #     Otherwise default to basic (email + token).
# #     You can override with --auth basic|bearer.
# #     """
# #     session = requests.Session()

# #     if auth_mode == "bearer":
# #         session.headers.update({
# #             "Authorization": f"Bearer {JIRA_API_TOKEN}",
# #         })
# #     else:  # basic
# #         session.auth = (JIRA_EMAIL, JIRA_API_TOKEN)

# #     session.headers.update({
# #         "Accept": "application/json",
# #         "Content-Type": "application/json",
# #     })
# #     return session


# # def build_jql(fr_project: str, fr_subproject: str, squad: str = "") -> str:
# #     fr_project = fr_project.replace('"', '\\"').strip()
# #     parts = [f'{FR_PROJECT_FIELD} ~ "{fr_project}"']

# #     fr_subproject = fr_subproject.strip()
# #     if fr_subproject:
# #         fr_subproject_escaped = fr_subproject.replace('"', '\\"')
# #         parts.append(f'{FR_SUBPROJECT_FIELD} ~ "{fr_subproject_escaped}"')

# #     squad = squad.strip()
# #     if squad:
# #         squad_escaped = squad.replace('"', '\\"')
# #         parts.append(f'{SQUAD_FIELD} = "{squad_escaped}"')

# #     return " AND ".join(parts)


# # def get_issue_count(session: requests.Session, jql: str) -> int:
# #     """
# #     Return the approximate count of issues matching the JQL, using
# #     /rest/api/3/search/approximate-count. Single call, no pagination.
# #     """
# #     payload = {"jql": jql}
# #     resp = session.post(COUNT_ENDPOINT, data=json.dumps(payload))
# #     if resp.status_code == 401:
# #         raise RuntimeError(
# #             "401 Unauthorized — wrong auth mode for this JIRA_URL. "
# #             "Try re-running with --auth basic or --auth bearer."
# #         )
# #     if resp.status_code == 410:
# #         raise RuntimeError(
# #             "410 Gone — this Jira site has fully removed the legacy "
# #             "search endpoint. Double-check COUNT_ENDPOINT is "
# #             "/rest/api/3/search/approximate-count."
# #         )
# #     resp.raise_for_status()
# #     data = resp.json()
# #     return data.get("count", 0)


# # def search_issues_with_field(session: requests.Session, jql: str, field_id: str):
# #     """
# #     Yield {"key": ..., "field_value": <raw cascading field dict or None>}
# #     for every issue matching the JQL, using /rest/api/3/search/jql
# #     (token-based pagination via nextPageToken).
# #     """
# #     next_page_token = None
# #     while True:
# #         payload = {
# #             "jql": jql,
# #             "maxResults": 100,
# #             "fields": ["key", field_id],
# #         }
# #         if next_page_token:
# #             payload["nextPageToken"] = next_page_token

# #         resp = session.post(SEARCH_ENDPOINT, data=json.dumps(payload))
# #         if resp.status_code == 401:
# #             raise RuntimeError(
# #                 "401 Unauthorized — wrong auth mode for this JIRA_URL. "
# #                 "Try re-running with --auth basic or --auth bearer."
# #             )
# #         if resp.status_code == 410:
# #             raise RuntimeError(
# #                 "410 Gone — legacy search endpoint removed. "
# #                 "SEARCH_ENDPOINT should be /rest/api/3/search/jql."
# #             )
# #         resp.raise_for_status()
# #         data = resp.json()

# #         issues = data.get("issues", [])
# #         for issue in issues:
# #             yield {
# #                 "key": issue.get("key"),
# #                 "field_value": (issue.get("fields") or {}).get(field_id),
# #             }

# #         next_page_token = data.get("nextPageToken")
# #         if not next_page_token or data.get("isLast") or not issues:
# #             break
# #         time.sleep(0.2)  # be gentle with the API


# # def update_squad_child(
# #     session: requests.Session,
# #     issue_key: str,
# #     field_id: str,
# #     parent_value: str,
# #     parent_id: str,
# #     new_child_value: str,
# #     dry_run: bool = True,
# # ):
# #     """
# #     PUT the cascading-select field back onto the issue, keeping the
# #     PARENT exactly as it currently is (by id if we have it, else by value)
# #     and setting only the CHILD to new_child_value.
# #     """
# #     field_payload = {"value": parent_value}
# #     if parent_id:
# #         # Prefer id when available -- most exact way to avoid touching parent.
# #         field_payload = {"id": parent_id}
# #     field_payload["child"] = {"value": new_child_value}

# #     body = {"fields": {field_id: field_payload}}

# #     if dry_run:
# #         print(f"    [DRY RUN] {issue_key}: keep parent={parent_value!r}, "
# #               f"set child={new_child_value!r}")
# #         return True, None

# #     resp = session.put(
# #         f"{JIRA_URL}/rest/api/3/issue/{issue_key}",
# #         data=json.dumps(body),
# #     )
# #     if resp.status_code in (204,):
# #         print(f"    OK {issue_key}: child -> {new_child_value!r} "
# #               f"(parent unchanged: {parent_value!r})")
# #         return True, None

# #     error = f"{resp.status_code} {resp.text[:300]}"
# #     print(f"    FAILED {issue_key}: {error}")
# #     return False, error


# # def main():
# #     parser = argparse.ArgumentParser()
# #     parser.add_argument("tsv_path", help="Path to the TSV mapping file")
# #     parser.add_argument(
# #         "--auth",
# #         choices=["basic", "bearer"],
# #         default="bearer" if "api.atlassian.com" in JIRA_URL else "basic",
# #         help="Auth mode against Jira (default inferred from JIRA_URL)",
# #     )
# #     parser.add_argument(
# #         "--limit-rows",
# #         type=int,
# #         default=None,
# #         help="Only process the first N rows of the TSV (useful for testing)",
# #     )
# #     parser.add_argument(
# #         "--limit-issues-per-row",
# #         type=int,
# #         default=None,
# #         help="Only update the first N matching issues per row (safety cap for testing)",
# #     )
# #     parser.add_argument(
# #         "--apply",
# #         action="store_true",
# #         help="Actually PUT the updates. Without this flag, it's a dry run "
# #              "that only prints what WOULD change.",
# #     )
# #     parser.add_argument(
# #         "--out",
# #         default="jira_update_results.json",
# #         help="Where to write the collected results (JSON)",
# #     )
# #     args = parser.parse_args()

# #     if not JIRA_URL or not JIRA_API_TOKEN:
# #         sys.exit("ERROR: JIRA_URL / JIRA_API_TOKEN not set in environment.")

# #     session = get_session(args.auth)

# #     if not args.apply:
# #         print("=== DRY RUN (no changes will be written). Pass --apply to write. ===\n")

# #     results = []
# #     with open(args.tsv_path, newline="", encoding="utf-8") as f:
# #         reader = csv.DictReader(f, delimiter="\t")
# #         for i, row in enumerate(reader):
# #             if args.limit_rows is not None and i >= args.limit_rows:
# #                 break

# #             fr_project = row.get("FR Project", "")
# #             fr_subproject = row.get("FR Subproject", "")
# #             current_squad = row.get("current squad name", "")
# #             target_child = row.get("to be updated Jira Squad", "").strip()

# #             if not fr_project:
# #                 continue
# #             if not target_child:
# #                 print(f"[Row {i}] skipping — no 'to be updated Jira Squad' value")
# #                 continue

# #             jql = build_jql(fr_project, fr_subproject, current_squad)
# #             print(f"\n[Row {i}] {fr_project} / {fr_subproject or '(none)'} / "
# #                   f"{current_squad or '(none)'} -> child: {target_child}")
# #             print(f"  JQL: {jql}")

# #             row_result = {
# #                 "row_index": i,
# #                 "fr_project": fr_project,
# #                 "fr_subproject": fr_subproject,
# #                 "current_squad": current_squad,
# #                 "target_child": target_child,
# #                 "jql": jql,
# #                 "issues": [],
# #                 "error": None,
# #             }

# #             try:
# #                 issue_iter = search_issues_with_field(session, jql, SQUAD_FIELD_ID)
# #                 processed = 0
# #                 for issue in issue_iter:
# #                     if (args.limit_issues_per_row is not None
# #                             and processed >= args.limit_issues_per_row):
# #                         break
# #                     processed += 1

# #                     key = issue["key"]
# #                     field_value = issue["field_value"] or {}
# #                     parent_value = field_value.get("value")
# #                     parent_id = field_value.get("id")
# #                     existing_child = (field_value.get("child") or {}).get("value")

# #                     if parent_value is None:
# #                         print(f"    SKIP {key}: Squad field is empty on this "
# #                               f"issue -- no parent to preserve, skipping to "
# #                               f"avoid guessing.")
# #                         row_result["issues"].append({
# #                             "key": key, "status": "skipped_empty_parent"
# #                         })
# #                         continue

# #                     if existing_child == target_child:
# #                         print(f"    SKIP {key}: child already = {target_child!r}")
# #                         row_result["issues"].append({
# #                             "key": key, "status": "already_correct"
# #                         })
# #                         continue

# #                     ok, err = update_squad_child(
# #                         session,
# #                         issue_key=key,
# #                         field_id=SQUAD_FIELD_ID,
# #                         parent_value=parent_value,
# #                         parent_id=parent_id,
# #                         new_child_value=target_child,
# #                         dry_run=not args.apply,
# #                     )
# #                     row_result["issues"].append({
# #                         "key": key,
# #                         "status": "ok" if ok else "failed",
# #                         "previous_child": existing_child,
# #                         "new_child": target_child,
# #                         "parent_kept": parent_value,
# #                         "error": err,
# #                     })
# #                     if args.apply:
# #                         time.sleep(0.2)  # be gentle with the API

# #             except Exception as e:
# #                 print(f"  ERROR: {e}")
# #                 row_result["error"] = str(e)

# #             results.append(row_result)

# #     with open(args.out, "w", encoding="utf-8") as out_f:
# #         json.dump(results, out_f, indent=2)

# #     print(f"\nDone. Wrote results for {len(results)} row(s) to {args.out}")
# #     if not args.apply:
# #         print("This was a DRY RUN — nothing was written. Re-run with --apply to write.")


# # if __name__ == "__main__":
# #     main()
# #!/usr/bin/env python3
# """
# Reads a TSV mapping file (FR Project / FR Subproject / current squad /
# target squad, etc.) and for each row:

#   1. Builds a JQL filter:
#        "FR Project[Short text]" ~ "<FR Project>"
#        AND "FR Sub-Project[Short text]" ~ "<FR Subproject>"
#        AND "Squad[Select List (cascading)]" = "<current squad name>"
#   2. Finds all matching issues.
#   3. For each issue, updates the Squad cascading-select field
#      (customfield_10973) so that:
#        - the PARENT value is left exactly as it currently is on the issue
#          (never touched/overwritten)
#        - the CHILD value is set to the row's "to be updated Jira Squad"
#   4. Writes an Excel file per "to be updated Jira Squad" value into a
#      project_wise/ folder, listing every issue matched for that squad
#      (across all TSV rows sharing that same target squad name).

# DEFAULT MODE IS DRY-RUN: it will print every planned change but will NOT
# call PUT on any issue unless you pass --apply.

# ENV VARS REQUIRED:
#     JIRA_URL          e.g. https://api.atlassian.com/ex/jira/<cloudId>
#     JIRA_EMAIL
#     JIRA_API_TOKEN
#     (JIRA_ACCOUNT_ID not needed here)

# USAGE:
#     # Safe: prints planned changes, no writes
#     python3 fetch_jira_issues.py input.tsv --limit-rows 1

#     # Actually performs the updates
#     python3 fetch_jira_issues.py input.tsv --limit-rows 1 --apply

#     python3 fetch_jira_issues.py input.tsv --auth basic   # force basic auth
#     python3 fetch_jira_issues.py input.tsv --auth bearer  # force bearer auth
# """

# import os
# import re
# import sys
# import csv
# import json
# import time
# import base64
# import argparse
# import requests
# import pandas as pd

# try:
#     from dotenv import load_dotenv
#     # Loads a .env file sitting in the current working directory (if present).
#     # Does NOT override variables that are already set in the real shell env.
#     load_dotenv()
# except ImportError:
#     pass  # dotenv not installed -- fall back to whatever's already exported

# # ----------------------------------------------------------------------------
# # Config from environment
# # ----------------------------------------------------------------------------
# JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
# JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
# JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

# # Custom field NAMES as they appear in your example JQL.
# # If Jira doesn't resolve these by name, replace with the customfield_XXXXX id.
# FR_PROJECT_FIELD = '"FR Project[Short text]"'
# FR_SUBPROJECT_FIELD = '"FR Sub-Project[Short text]"'
# SQUAD_FIELD = '"Squad[Select List (cascading)]"'

# # The actual field ID used for reading/writing via the REST API (JQL can use
# # the display name above, but issue field updates need the customfield_ id).
# SQUAD_FIELD_ID = "customfield_10973"

# # NOTE: /rest/api/3/search is deprecated/removed by Atlassian (410 Gone).
# # The new /rest/api/3/search/jql endpoint no longer returns a "total" count
# # at all -- for just a count, Atlassian's dedicated endpoint is
# # /rest/api/3/search/approximate-count.
# COUNT_ENDPOINT = f"{JIRA_URL}/rest/api/3/search/approximate-count"
# SEARCH_ENDPOINT = f"{JIRA_URL}/rest/api/3/search/jql"


# def get_session(auth_mode: str) -> requests.Session:
#     """
#     api.atlassian.com/ex/jira/<cloudId> is the OAuth 2.0 (3LO) gateway.
#     That gateway expects:  Authorization: Bearer <oauth_access_token>
#     A classic Jira API token (email + token, Basic auth) does NOT work
#     against api.atlassian.com — it only works against
#     https://yoursite.atlassian.net directly.

#     So: if JIRA_URL contains "api.atlassian.com", default to bearer.
#     Otherwise default to basic (email + token).
#     You can override with --auth basic|bearer.
#     """
#     session = requests.Session()

#     if auth_mode == "bearer":
#         session.headers.update({
#             "Authorization": f"Bearer {JIRA_API_TOKEN}",
#         })
#     else:  # basic
#         session.auth = (JIRA_EMAIL, JIRA_API_TOKEN)

#     session.headers.update({
#         "Accept": "application/json",
#         "Content-Type": "application/json",
#     })
#     return session


# def build_jql(fr_project: str, fr_subproject: str, squad: str = "") -> str:
#     fr_project = fr_project.replace('"', '\\"').strip()
#     parts = [f'{FR_PROJECT_FIELD} ~ "{fr_project}"']

#     fr_subproject = fr_subproject.strip()
#     if fr_subproject:
#         fr_subproject_escaped = fr_subproject.replace('"', '\\"')
#         parts.append(f'{FR_SUBPROJECT_FIELD} ~ "{fr_subproject_escaped}"')

#     squad = squad.strip()
#     if squad:
#         squad_escaped = squad.replace('"', '\\"')
#         parts.append(f'{SQUAD_FIELD} = "{squad_escaped}"')

#     return " AND ".join(parts)


# def get_issue_count(session: requests.Session, jql: str) -> int:
#     """
#     Return the approximate count of issues matching the JQL, using
#     /rest/api/3/search/approximate-count. Single call, no pagination.
#     """
#     payload = {"jql": jql}
#     resp = session.post(COUNT_ENDPOINT, data=json.dumps(payload))
#     if resp.status_code == 401:
#         raise RuntimeError(
#             "401 Unauthorized — wrong auth mode for this JIRA_URL. "
#             "Try re-running with --auth basic or --auth bearer."
#         )
#     if resp.status_code == 410:
#         raise RuntimeError(
#             "410 Gone — this Jira site has fully removed the legacy "
#             "search endpoint. Double-check COUNT_ENDPOINT is "
#             "/rest/api/3/search/approximate-count."
#         )
#     resp.raise_for_status()
#     data = resp.json()
#     return data.get("count", 0)


# def search_issues_with_field(session: requests.Session, jql: str, field_id: str):
#     """
#     Yield {"key": ..., "field_value": <raw cascading field dict or None>}
#     for every issue matching the JQL, using /rest/api/3/search/jql
#     (token-based pagination via nextPageToken).
#     """
#     next_page_token = None
#     while True:
#         payload = {
#             "jql": jql,
#             "maxResults": 100,
#             "fields": ["key", field_id],
#         }
#         if next_page_token:
#             payload["nextPageToken"] = next_page_token

#         resp = session.post(SEARCH_ENDPOINT, data=json.dumps(payload))
#         if resp.status_code == 401:
#             raise RuntimeError(
#                 "401 Unauthorized — wrong auth mode for this JIRA_URL. "
#                 "Try re-running with --auth basic or --auth bearer."
#             )
#         if resp.status_code == 410:
#             raise RuntimeError(
#                 "410 Gone — legacy search endpoint removed. "
#                 "SEARCH_ENDPOINT should be /rest/api/3/search/jql."
#             )
#         resp.raise_for_status()
#         data = resp.json()

#         issues = data.get("issues", [])
#         for issue in issues:
#             yield {
#                 "key": issue.get("key"),
#                 "field_value": (issue.get("fields") or {}).get(field_id),
#             }

#         next_page_token = data.get("nextPageToken")
#         if not next_page_token or data.get("isLast") or not issues:
#             break
#         time.sleep(0.2)  # be gentle with the API


# def sanitize_filename(name: str) -> str:
#     """Make a string safe to use as a filename (no path separators etc.)."""
#     name = name.strip()
#     name = re.sub(r'[\\/*?:"<>|]', "-", name)
#     name = re.sub(r"\s+", " ", name)
#     return name or "unnamed"


# def write_project_wise_excels(rows_by_squad: dict, export_dir: str):
#     """
#     rows_by_squad: { "<to be updated Jira Squad>": [record_dict, ...] }
#     Writes one .xlsx per squad name into export_dir/<squad>.xlsx.
#     If a squad name repeats across TSV rows, all its issues are combined
#     into that same file (appended as more rows, not overwritten).
#     """
#     os.makedirs(export_dir, exist_ok=True)
#     written = []
#     for squad_name, records in rows_by_squad.items():
#         if not records:
#             continue
#         df = pd.DataFrame(records)
#         filename = sanitize_filename(squad_name) + ".xlsx"
#         path = os.path.join(export_dir, filename)
#         df.to_excel(path, index=False, sheet_name="Issues")
#         written.append(path)
#     return written


# def update_squad_child(
#     session: requests.Session,
#     issue_key: str,
#     field_id: str,
#     parent_value: str,
#     parent_id: str,
#     new_child_value: str,
#     dry_run: bool = True,
# ):
#     """
#     PUT the cascading-select field back onto the issue, keeping the
#     PARENT exactly as it currently is (by id if we have it, else by value)
#     and setting only the CHILD to new_child_value.
#     """
#     field_payload = {"value": parent_value}
#     if parent_id:
#         # Prefer id when available -- most exact way to avoid touching parent.
#         field_payload = {"id": parent_id}
#     field_payload["child"] = {"value": new_child_value}

#     body = {"fields": {field_id: field_payload}}

#     if dry_run:
#         print(f"    [DRY RUN] {issue_key}: keep parent={parent_value!r}, "
#               f"set child={new_child_value!r}")
#         return True, None

#     resp = session.put(
#         f"{JIRA_URL}/rest/api/3/issue/{issue_key}",
#         data=json.dumps(body),
#     )
#     if resp.status_code in (204,):
#         print(f"    OK {issue_key}: child -> {new_child_value!r} "
#               f"(parent unchanged: {parent_value!r})")
#         return True, None

#     error = f"{resp.status_code} {resp.text[:300]}"
#     print(f"    FAILED {issue_key}: {error}")
#     return False, error


# def main():
#     parser = argparse.ArgumentParser()
#     parser.add_argument("tsv_path", help="Path to the TSV mapping file")
#     parser.add_argument(
#         "--auth",
#         choices=["basic", "bearer"],
#         default="bearer" if "api.atlassian.com" in JIRA_URL else "basic",
#         help="Auth mode against Jira (default inferred from JIRA_URL)",
#     )
#     parser.add_argument(
#         "--limit-rows",
#         type=int,
#         default=None,
#         help="Only process the first N rows of the TSV (useful for testing)",
#     )
#     parser.add_argument(
#         "--limit-issues-per-row",
#         type=int,
#         default=None,
#         help="Only update the first N matching issues per row (safety cap for testing)",
#     )
#     parser.add_argument(
#         "--apply",
#         action="store_true",
#         help="Actually PUT the updates. Without this flag, it's a dry run "
#              "that only prints what WOULD change.",
#     )
#     parser.add_argument(
#         "--out",
#         default="jira_update_results.json",
#         help="Where to write the collected results (JSON)",
#     )
#     parser.add_argument(
#         "--export-dir",
#         default="project_wise",
#         help="Folder to write one Excel file per 'to be updated Jira Squad' "
#              "value, each listing all matching issues for that squad.",
#     )
#     args = parser.parse_args()

#     if not JIRA_URL or not JIRA_API_TOKEN:
#         sys.exit("ERROR: JIRA_URL / JIRA_API_TOKEN not set in environment.")

#     session = get_session(args.auth)

#     if not args.apply:
#         print("=== DRY RUN (no changes will be written). Pass --apply to write. ===\n")

#     results = []
#     rows_by_squad = {}  # target_child -> list of record dicts, for Excel export

#     with open(args.tsv_path, newline="", encoding="utf-8") as f:
#         reader = csv.DictReader(f, delimiter="\t")
#         for i, row in enumerate(reader):
#             if args.limit_rows is not None and i >= args.limit_rows:
#                 break

#             fr_project = row.get("FR Project", "")
#             fr_subproject = row.get("FR Subproject", "")
#             current_squad = row.get("current squad name", "")
#             target_child = row.get("to be updated Jira Squad", "").strip()

#             if not fr_project:
#                 continue
#             if not target_child:
#                 print(f"[Row {i}] skipping — no 'to be updated Jira Squad' value")
#                 continue

#             jql = build_jql(fr_project, fr_subproject, current_squad)
#             print(f"\n[Row {i}] {fr_project} / {fr_subproject or '(none)'} / "
#                   f"{current_squad or '(none)'} -> child: {target_child}")
#             print(f"  JQL: {jql}")

#             row_result = {
#                 "row_index": i,
#                 "fr_project": fr_project,
#                 "fr_subproject": fr_subproject,
#                 "current_squad": current_squad,
#                 "target_child": target_child,
#                 "jql": jql,
#                 "issues": [],
#                 "error": None,
#             }

#             try:
#                 issue_iter = search_issues_with_field(session, jql, SQUAD_FIELD_ID)
#                 processed = 0
#                 for issue in issue_iter:
#                     if (args.limit_issues_per_row is not None
#                             and processed >= args.limit_issues_per_row):
#                         break
#                     processed += 1

#                     key = issue["key"]
#                     field_value = issue["field_value"] or {}
#                     parent_value = field_value.get("value")
#                     parent_id = field_value.get("id")
#                     existing_child = (field_value.get("child") or {}).get("value")

#                     if parent_value is None:
#                         print(f"    SKIP {key}: Squad field is empty on this "
#                               f"issue -- no parent to preserve, skipping to "
#                               f"avoid guessing.")
#                         row_result["issues"].append({
#                             "key": key, "status": "skipped_empty_parent"
#                         })
#                         rows_by_squad.setdefault(target_child, []).append({
#                             "row_index": i,
#                             "fr_project": fr_project,
#                             "fr_subproject": fr_subproject,
#                             "current_squad_parent": current_squad,
#                             "issue_key": key,
#                             "previous_child": None,
#                             "target_child": target_child,
#                             "status": "skipped_empty_parent",
#                         })
#                         continue

#                     if existing_child == target_child:
#                         print(f"    SKIP {key}: child already = {target_child!r}")
#                         row_result["issues"].append({
#                             "key": key, "status": "already_correct"
#                         })
#                         rows_by_squad.setdefault(target_child, []).append({
#                             "row_index": i,
#                             "fr_project": fr_project,
#                             "fr_subproject": fr_subproject,
#                             "current_squad_parent": parent_value,
#                             "issue_key": key,
#                             "previous_child": existing_child,
#                             "target_child": target_child,
#                             "status": "already_correct",
#                         })
#                         continue

#                     ok, err = update_squad_child(
#                         session,
#                         issue_key=key,
#                         field_id=SQUAD_FIELD_ID,
#                         parent_value=parent_value,
#                         parent_id=parent_id,
#                         new_child_value=target_child,
#                         dry_run=not args.apply,
#                     )
#                     row_result["issues"].append({
#                         "key": key,
#                         "status": "ok" if ok else "failed",
#                         "previous_child": existing_child,
#                         "new_child": target_child,
#                         "parent_kept": parent_value,
#                         "error": err,
#                     })
#                     rows_by_squad.setdefault(target_child, []).append({
#                         "row_index": i,
#                         "fr_project": fr_project,
#                         "fr_subproject": fr_subproject,
#                         "current_squad_parent": parent_value,
#                         "issue_key": key,
#                         "previous_child": existing_child,
#                         "target_child": target_child,
#                         "status": "ok" if ok else "failed",
#                         "error": err,
#                     })
#                     if args.apply:
#                         time.sleep(0.2)  # be gentle with the API

#             except Exception as e:
#                 print(f"  ERROR: {e}")
#                 row_result["error"] = str(e)

#             results.append(row_result)

#     with open(args.out, "w", encoding="utf-8") as out_f:
#         json.dump(results, out_f, indent=2)

#     print(f"\nDone. Wrote results for {len(results)} row(s) to {args.out}")
#     if not args.apply:
#         print("This was a DRY RUN — nothing was written. Re-run with --apply to write.")

#     written_files = write_project_wise_excels(rows_by_squad, args.export_dir)
#     print(f"Wrote {len(written_files)} Excel file(s) to '{args.export_dir}/':")
#     for p in written_files:
#         print(f"  {p}")


# if __name__ == "__main__":
#     main()
#!/usr/bin/env python3
"""
Reads a TSV mapping file (FR Project / FR Subproject / current squad /
target squad, etc.) and for each row:

  1. Builds a JQL filter:
       "FR Project[Short text]" ~ "<FR Project>"
       AND "FR Sub-Project[Short text]" ~ "<FR Subproject>"
       AND "Squad[Select List (cascading)]" = "<current squad name>"
  2. Finds all matching issues.
  3. For each issue, updates the Squad cascading-select field
     (customfield_10973) so that:
       - the PARENT value is left exactly as it currently is on the issue
         (never touched/overwritten)
       - the CHILD value is set to the row's "to be updated Jira Squad"
  4. Writes an Excel file per "to be updated Jira Squad" value into a
     project_wise/ folder, listing every issue matched for that squad
     (across all TSV rows sharing that same target squad name).

DEFAULT MODE IS DRY-RUN: it will print every planned change but will NOT
call PUT on any issue unless you pass --apply.

ENV VARS REQUIRED:
    JIRA_URL          e.g. https://api.atlassian.com/ex/jira/<cloudId>
    JIRA_EMAIL
    JIRA_API_TOKEN
    (JIRA_ACCOUNT_ID not needed here)

USAGE:
    # Safe: prints planned changes, no writes
    python3 fetch_jira_issues.py input.tsv --limit-rows 1

    # Actually performs the updates
    python3 fetch_jira_issues.py input.tsv --limit-rows 1 --apply

    python3 fetch_jira_issues.py input.tsv --auth basic   # force basic auth
    python3 fetch_jira_issues.py input.tsv --auth bearer  # force bearer auth
"""

import os
import re
import sys
import csv
import json
import time
import base64
import argparse
import requests
import pandas as pd

try:
    from dotenv import load_dotenv
    # Loads a .env file sitting in the current working directory (if present).
    # Does NOT override variables that are already set in the real shell env.
    load_dotenv()
except ImportError:
    pass  # dotenv not installed -- fall back to whatever's already exported

# ----------------------------------------------------------------------------
# Config from environment
# ----------------------------------------------------------------------------
JIRA_URL = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

# Custom field NAMES as they appear in your example JQL.
# If Jira doesn't resolve these by name, replace with the customfield_XXXXX id.
FR_PROJECT_FIELD = '"FR Project[Short text]"'
FR_SUBPROJECT_FIELD = '"FR Sub-Project[Short text]"'
SQUAD_FIELD = '"Squad[Select List (cascading)]"'

# The actual field ID used for reading/writing via the REST API (JQL can use
# the display name above, but issue field updates need the customfield_ id).
SQUAD_FIELD_ID = "customfield_10973"

# NOTE: /rest/api/3/search is deprecated/removed by Atlassian (410 Gone).
# The new /rest/api/3/search/jql endpoint no longer returns a "total" count
# at all -- for just a count, Atlassian's dedicated endpoint is
# /rest/api/3/search/approximate-count.
COUNT_ENDPOINT = f"{JIRA_URL}/rest/api/3/search/approximate-count"
SEARCH_ENDPOINT = f"{JIRA_URL}/rest/api/3/search/jql"


def get_session(auth_mode: str) -> requests.Session:
    """
    api.atlassian.com/ex/jira/<cloudId> is the OAuth 2.0 (3LO) gateway.
    That gateway expects:  Authorization: Bearer <oauth_access_token>
    A classic Jira API token (email + token, Basic auth) does NOT work
    against api.atlassian.com — it only works against
    https://yoursite.atlassian.net directly.

    So: if JIRA_URL contains "api.atlassian.com", default to bearer.
    Otherwise default to basic (email + token).
    You can override with --auth basic|bearer.
    """
    session = requests.Session()

    if auth_mode == "bearer":
        session.headers.update({
            "Authorization": f"Bearer {JIRA_API_TOKEN}",
        })
    else:  # basic
        session.auth = (JIRA_EMAIL, JIRA_API_TOKEN)

    session.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return session


def escape_jql_value(value: str) -> str:
    """
    Prepare a raw TSV cell value for safe use inside a double-quoted JQL
    string literal.

    - Strips leading/trailing whitespace.
    - Collapses any run of internal whitespace (double spaces, stray tabs)
      down to a single space -- messy TSV exports often have these.
    - Escapes backslashes FIRST, then double quotes (order matters: if you
      escape quotes first, escaping backslashes afterwards would corrupt
      the quote-escapes you just added).

    Ordinary characters like hyphens, spaces, ampersands, etc. need NO
    escaping inside a quoted JQL string -- "FS - Data Migration" is valid
    JQL exactly as written. Only a literal " or \\ in the value itself
    needs escaping.
    """
    value = value.strip()
    value = re.sub(r"\s+", " ", value)
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return value


def build_jql(fr_project: str, fr_subproject: str, squad: str = "") -> str:
    parts = [f'{FR_PROJECT_FIELD} ~ "{escape_jql_value(fr_project)}"']

    fr_subproject = fr_subproject.strip()
    if fr_subproject:
        parts.append(f'{FR_SUBPROJECT_FIELD} ~ "{escape_jql_value(fr_subproject)}"')

    squad = squad.strip()
    if squad:
        parts.append(f'{SQUAD_FIELD} = "{escape_jql_value(squad)}"')

    return " AND ".join(parts)


def get_issue_count(session: requests.Session, jql: str) -> int:
    """
    Return the approximate count of issues matching the JQL, using
    /rest/api/3/search/approximate-count. Single call, no pagination.
    """
    payload = {"jql": jql}
    resp = session.post(COUNT_ENDPOINT, data=json.dumps(payload))
    if resp.status_code == 401:
        raise RuntimeError(
            "401 Unauthorized — wrong auth mode for this JIRA_URL. "
            "Try re-running with --auth basic or --auth bearer."
        )
    if resp.status_code == 410:
        raise RuntimeError(
            "410 Gone — this Jira site has fully removed the legacy "
            "search endpoint. Double-check COUNT_ENDPOINT is "
            "/rest/api/3/search/approximate-count."
        )
    resp.raise_for_status()
    data = resp.json()
    return data.get("count", 0)


def search_issues_with_field(session: requests.Session, jql: str, field_id: str):
    """
    Yield {"key": ..., "field_value": <raw cascading field dict or None>}
    for every issue matching the JQL, using /rest/api/3/search/jql
    (token-based pagination via nextPageToken).
    """
    next_page_token = None
    while True:
        payload = {
            "jql": jql,
            "maxResults": 100,
            "fields": ["key", field_id],
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        resp = session.post(SEARCH_ENDPOINT, data=json.dumps(payload))
        if resp.status_code == 401:
            raise RuntimeError(
                "401 Unauthorized — wrong auth mode for this JIRA_URL. "
                "Try re-running with --auth basic or --auth bearer."
            )
        if resp.status_code == 410:
            raise RuntimeError(
                "410 Gone — legacy search endpoint removed. "
                "SEARCH_ENDPOINT should be /rest/api/3/search/jql."
            )
        resp.raise_for_status()
        data = resp.json()

        issues = data.get("issues", [])
        for issue in issues:
            yield {
                "key": issue.get("key"),
                "field_value": (issue.get("fields") or {}).get(field_id),
            }

        next_page_token = data.get("nextPageToken")
        if not next_page_token or data.get("isLast") or not issues:
            break
        time.sleep(0.2)  # be gentle with the API


def sanitize_filename(name: str) -> str:
    """Make a string safe to use as a filename (no path separators etc.)."""
    name = name.strip()
    name = re.sub(r'[\\/*?:"<>|]', "-", name)
    name = re.sub(r"\s+", " ", name)
    return name or "unnamed"


def write_project_wise_excels(rows_by_squad: dict, export_dir: str):
    """
    rows_by_squad: { "<to be updated Jira Squad>": [record_dict, ...] }
    Writes one .xlsx per squad name into export_dir/<squad>.xlsx.
    If a squad name repeats across TSV rows, all its issues are combined
    into that same file (appended as more rows, not overwritten).
    """
    os.makedirs(export_dir, exist_ok=True)
    written = []
    for squad_name, records in rows_by_squad.items():
        if not records:
            continue
        df = pd.DataFrame(records)
        filename = sanitize_filename(squad_name) + ".xlsx"
        path = os.path.join(export_dir, filename)
        df.to_excel(path, index=False, sheet_name="Issues")
        written.append(path)
    return written


def update_squad_child(
    session: requests.Session,
    issue_key: str,
    field_id: str,
    parent_value: str,
    parent_id: str,
    new_child_value: str,
    dry_run: bool = True,
):
    """
    PUT the cascading-select field back onto the issue, keeping the
    PARENT exactly as it currently is (by id if we have it, else by value)
    and setting only the CHILD to new_child_value.
    """
    field_payload = {"value": parent_value}
    if parent_id:
        # Prefer id when available -- most exact way to avoid touching parent.
        field_payload = {"id": parent_id}
    field_payload["child"] = {"value": new_child_value}

    body = {"fields": {field_id: field_payload}}

    if dry_run:
        print(f"    [DRY RUN] {issue_key}: keep parent={parent_value!r}, "
              f"set child={new_child_value!r}")
        return True, None

    resp = session.put(
        f"{JIRA_URL}/rest/api/3/issue/{issue_key}",
        data=json.dumps(body),
    )
    if resp.status_code in (204,):
        print(f"    OK {issue_key}: child -> {new_child_value!r} "
              f"(parent unchanged: {parent_value!r})")
        return True, None

    error = f"{resp.status_code} {resp.text[:300]}"
    print(f"    FAILED {issue_key}: {error}")
    return False, error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("tsv_path", help="Path to the TSV mapping file")
    parser.add_argument(
        "--auth",
        choices=["basic", "bearer"],
        default="bearer" if "api.atlassian.com" in JIRA_URL else "basic",
        help="Auth mode against Jira (default inferred from JIRA_URL)",
    )
    parser.add_argument(
        "--limit-rows",
        type=int,
        default=None,
        help="Only process the first N rows of the TSV (useful for testing)",
    )
    parser.add_argument(
        "--limit-issues-per-row",
        type=int,
        default=None,
        help="Only update the first N matching issues per row (safety cap for testing)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually PUT the updates. Without this flag, it's a dry run "
             "that only prints what WOULD change.",
    )
    parser.add_argument(
        "--out",
        default="jira_update_results.json",
        help="Where to write the collected results (JSON)",
    )
    parser.add_argument(
        "--export-dir",
        default="project_wise",
        help="Folder to write one Excel file per 'to be updated Jira Squad' "
             "value, each listing all matching issues for that squad.",
    )
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Just print the issue count per row (approximate-count API) "
             "and exit. No search-with-fields, no updates, no Excel export.",
    )
    args = parser.parse_args()

    if not JIRA_URL or not JIRA_API_TOKEN:
        sys.exit("ERROR: JIRA_URL / JIRA_API_TOKEN not set in environment.")

    session = get_session(args.auth)

    if args.count_only:
        with open(args.tsv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for i, row in enumerate(reader):
                if args.limit_rows is not None and i >= args.limit_rows:
                    break

                fr_project = row.get("FR Project", "")
                fr_subproject = row.get("FR Subproject", "")
                current_squad = row.get("current squad name", "")
                target_child = row.get("to be updated Jira Squad", "")
                target_child = re.sub(r"\s+", " ", target_child.strip())

                if not fr_project:
                    continue

                jql = build_jql(fr_project, fr_subproject, current_squad)
                print(f"\n[Row {i}] {fr_project} / {fr_subproject or '(none)'} / "
                      f"{current_squad or '(none)'} -> child: {target_child or '(none)'}")
                print(f"  JQL: {jql}")

                try:
                    count = get_issue_count(session, jql)
                    print(f"  -> {count} issue(s)")
                except Exception as e:
                    print(f"  ERROR: {e}")
        return

    if not args.apply:
        print("=== DRY RUN (no changes will be written). Pass --apply to write. ===\n")

    results = []
    rows_by_squad = {}  # target_child -> list of record dicts, for Excel export

    with open(args.tsv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for i, row in enumerate(reader):
            if args.limit_rows is not None and i >= args.limit_rows:
                break

            fr_project = row.get("FR Project", "")
            fr_subproject = row.get("FR Subproject", "")
            current_squad = row.get("current squad name", "")
            target_child = row.get("to be updated Jira Squad", "")
            target_child = re.sub(r"\s+", " ", target_child.strip())

            if not fr_project:
                continue
            if not target_child:
                print(f"[Row {i}] skipping — no 'to be updated Jira Squad' value")
                continue

            jql = build_jql(fr_project, fr_subproject, current_squad)
            print(f"\n[Row {i}] {fr_project} / {fr_subproject or '(none)'} / "
                  f"{current_squad or '(none)'} -> child: {target_child}")
            print(f"  JQL: {jql}")

            row_result = {
                "row_index": i,
                "fr_project": fr_project,
                "fr_subproject": fr_subproject,
                "current_squad": current_squad,
                "target_child": target_child,
                "jql": jql,
                "issues": [],
                "error": None,
            }

            try:
                issue_iter = search_issues_with_field(session, jql, SQUAD_FIELD_ID)
                processed = 0
                for issue in issue_iter:
                    if (args.limit_issues_per_row is not None
                            and processed >= args.limit_issues_per_row):
                        break
                    processed += 1

                    key = issue["key"]
                    field_value = issue["field_value"] or {}
                    parent_value = field_value.get("value")
                    parent_id = field_value.get("id")
                    existing_child = (field_value.get("child") or {}).get("value")

                    if parent_value is None:
                        print(f"    SKIP {key}: Squad field is empty on this "
                              f"issue -- no parent to preserve, skipping to "
                              f"avoid guessing.")
                        row_result["issues"].append({
                            "key": key, "status": "skipped_empty_parent"
                        })
                        rows_by_squad.setdefault(target_child, []).append({
                            "row_index": i,
                            "fr_project": fr_project,
                            "fr_subproject": fr_subproject,
                            "current_squad_parent": current_squad,
                            "issue_key": key,
                            "previous_child": None,
                            "target_child": target_child,
                            "status": "skipped_empty_parent",
                        })
                        continue

                    if existing_child == target_child:
                        print(f"    SKIP {key}: child already = {target_child!r}")
                        row_result["issues"].append({
                            "key": key, "status": "already_correct"
                        })
                        rows_by_squad.setdefault(target_child, []).append({
                            "row_index": i,
                            "fr_project": fr_project,
                            "fr_subproject": fr_subproject,
                            "current_squad_parent": parent_value,
                            "issue_key": key,
                            "previous_child": existing_child,
                            "target_child": target_child,
                            "status": "already_correct",
                        })
                        continue

                    ok, err = update_squad_child(
                        session,
                        issue_key=key,
                        field_id=SQUAD_FIELD_ID,
                        parent_value=parent_value,
                        parent_id=parent_id,
                        new_child_value=target_child,
                        dry_run=not args.apply,
                    )
                    row_result["issues"].append({
                        "key": key,
                        "status": "ok" if ok else "failed",
                        "previous_child": existing_child,
                        "new_child": target_child,
                        "parent_kept": parent_value,
                        "error": err,
                    })
                    rows_by_squad.setdefault(target_child, []).append({
                        "row_index": i,
                        "fr_project": fr_project,
                        "fr_subproject": fr_subproject,
                        "current_squad_parent": parent_value,
                        "issue_key": key,
                        "previous_child": existing_child,
                        "target_child": target_child,
                        "status": "ok" if ok else "failed",
                        "error": err,
                    })
                    if args.apply:
                        time.sleep(0.2)  # be gentle with the API

            except Exception as e:
                print(f"  ERROR: {e}")
                row_result["error"] = str(e)

            results.append(row_result)

    with open(args.out, "w", encoding="utf-8") as out_f:
        json.dump(results, out_f, indent=2)

    print(f"\nDone. Wrote results for {len(results)} row(s) to {args.out}")
    if not args.apply:
        print("This was a DRY RUN — nothing was written. Re-run with --apply to write.")

    written_files = write_project_wise_excels(rows_by_squad, args.export_dir)
    print(f"Wrote {len(written_files)} Excel file(s) to '{args.export_dir}/':")
    for p in written_files:
        print(f"  {p}")


if __name__ == "__main__":
    main()