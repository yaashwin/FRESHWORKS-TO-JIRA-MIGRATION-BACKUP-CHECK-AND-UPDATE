# # import os
# # import sys

# # import requests
# # from dotenv import load_dotenv

# # load_dotenv(override=True)

# # # =============================================================
# # # CONFIG — Jira (same .env as the other scripts)
# # # =============================================================
# # JIRA_BASE_URL  = os.getenv("JIRA_BASE_URL", "").rstrip("/")
# # JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
# # JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
# # JIRA_PAT       = os.getenv("JIRA_PAT")

# # JIRA_SESSION = requests.Session()
# # if JIRA_EMAIL and JIRA_API_TOKEN:
# #     JIRA_SESSION.auth = (JIRA_EMAIL, JIRA_API_TOKEN)   # Basic auth (Jira Cloud)
# # elif JIRA_PAT:
# #     JIRA_SESSION.headers.update({"Authorization": f"Bearer {JIRA_PAT}"})
# # JIRA_SESSION.headers.update({"Content-Type": "application/json"})

# # TARGET_FIELD = "customfield_13034"   # "Default FR Field" (ADF paragraph field)
# # FR_KEY_FIELD = "customfield_13038"   # "FR Key"


# # # =============================================================
# # # Jira helper (GET) — self-contained, no external module
# # # =============================================================
# # def jira_get(path, params=None):
# #     url = f"{JIRA_BASE_URL}/rest/{path}"
# #     resp = JIRA_SESSION.get(url, params=params, timeout=30)
# #     data = resp.json() if resp.content else None
# #     return data, resp.status_code


# # def jira_post(path, payload):
# #     url = f"{JIRA_BASE_URL}/rest/{path}"
# #     resp = JIRA_SESSION.post(url, json=payload, timeout=30)
# #     data = resp.json() if resp.content else None
# #     return data, resp.status_code


# # # =============================================================
# # # STEP 1 — build the JQL from the user's input
# # # =============================================================
# # def build_jql(field_value, indepth_clause=None):
# #     """
# #     No indepth search:
# #         "Default FR Field[Paragraph]" ~ "\"{field_value} : \""

# #     Indepth search (extra clause ANDed on):
# #         "Default FR Field[Paragraph]" ~ "\"{field_value}\"" and {indepth_clause}
# #     """
# #     if indepth_clause:
# #         base = f'"Default FR Field[Paragraph]" ~ "\\"{field_value}\\""'
# #         return f"{base} and {indepth_clause}"

# #     base = f'"Default FR Field[Paragraph]" ~ "\\"{field_value} : \\""'
# #     return base


# # # =============================================================
# # # STEP 2 — flatten a paragraph's ADF content into plain text
# # # =============================================================
# # def _paragraph_plain_text(node):
# #     parts = []
# #     for child in node.get("content", []) or []:
# #         if child.get("type") == "text":
# #             parts.append(child.get("text", ""))
# #         elif child.get("type") == "hardBreak":
# #             parts.append("\n")
# #     return "".join(parts)


# # def matching_paragraph_text(adf_doc, field_value):
# #     """Find and return the plain text of the paragraph(s) in TARGET_FIELD
# #     whose text contains field_value, for display/verification."""
# #     if not adf_doc:
# #         return []
# #     matches = []
# #     for node in adf_doc.get("content", []) or []:
# #         if node.get("type") != "paragraph":
# #             continue
# #         text = _paragraph_plain_text(node)
# #         if field_value in text:
# #             matches.append(text)
# #     return matches


# # # =============================================================
# # # STEP 3 — run the JQL, paginating through all results
# # # =============================================================
# # def search_all_issues(jql, fields=(FR_KEY_FIELD, TARGET_FIELD)):
# #     """
# #     Uses the new POST /rest/api/3/search/jql endpoint (the old GET
# #     /rest/api/3/search was removed by Atlassian). This endpoint pages
# #     with a nextPageToken instead of startAt, and doesn't return a
# #     "total" up front — the count is simply how many issues we collect
# #     once every page has been walked.
# #     """
# #     issues = []
# #     next_page_token = None

# #     while True:
# #         payload = {
# #             "jql": jql,
# #             "maxResults": 100,
# #             "fields": list(fields),
# #         }
# #         if next_page_token:
# #             payload["nextPageToken"] = next_page_token

# #         data, status_code = jira_post("api/3/search/jql", payload)
# #         if status_code != 200 or not data:
# #             raise RuntimeError(f"Search failed (status {status_code}): {data}")

# #         page_issues = data.get("issues", [])
# #         issues.extend(page_issues)

# #         next_page_token = data.get("nextPageToken")
# #         is_last = data.get("isLast", not next_page_token)

# #         if is_last or not page_issues:
# #             break

# #     return issues, len(issues)


# # # =============================================================
# # # MAIN
# # # =============================================================
# # def main():
# #     if not JIRA_BASE_URL or not ((JIRA_EMAIL and JIRA_API_TOKEN) or JIRA_PAT):
# #         print("ERROR: Jira credentials missing. Set JIRA_BASE_URL and either "
# #               "(JIRA_EMAIL + JIRA_API_TOKEN) or JIRA_PAT in .env")
# #         return 1

# #     field_value = input('Enter the value to search for (e.g. cf_release_quarter): ').strip()
# #     if not field_value:
# #         print("No value given, exiting.")
# #         return 1

# #     indepth_answer = input("Do you need to add indepth search? (yes/no): ").strip().lower()
# #     indepth_clause = None
# #     if indepth_answer in ("y", "yes"):
# #         indepth_clause = input(
# #             'Enter the extra JQL clause to AND on '
# #             '(e.g. "FR key[Short text]" IS NOT EMPTY): '
# #         ).strip()
# #         if not indepth_clause:
# #             print("No indepth clause given — continuing without it.")
# #             indepth_clause = None

# #     jql = build_jql(field_value, indepth_clause)
# #     print(f"\nJQL:\n  {jql}\n")

# #     try:
# #         issues, total = search_all_issues(jql)
# #     except Exception as e:
# #         print(f"ERROR: {e}")
# #         return 1

# #     print(f"Found {total} issue(s):\n")
# #     for issue in issues:
# #         key = issue.get("key")
# #         fields = issue.get("fields", {}) or {}
# #         fr_key = fields.get(FR_KEY_FIELD) or "-"
# #         target_adf = fields.get(TARGET_FIELD)
# #         matches = matching_paragraph_text(target_adf, field_value)

# #         print(f"  - {key}  (FR Key: {fr_key})")
# #         if matches:
# #             for m in matches:
# #                 print(f"      matched paragraph: {m}")
# #         else:
# #             print(f"      (matched by JQL, but no paragraph text containing "
# #                   f"'{field_value}' found in {TARGET_FIELD} fields returned)")

# #     print("\n" + "=" * 60)
# #     print(f"Total count: {total}")
# #     print("=" * 60)

# #     return 0


# # if __name__ == "__main__":
# #     sys.exit(main())
# import os
# import sys
# import threading
# from concurrent.futures import ThreadPoolExecutor, as_completed

# import requests
# from dotenv import load_dotenv

# load_dotenv(override=True)

# WORKER_COUNT = 20   # parallel threads used to fetch each issue's field data

# # =============================================================
# # CONFIG — Jira (same .env as the other scripts)
# # =============================================================
# JIRA_BASE_URL  = os.getenv("JIRA_BASE_URL", "").rstrip("/")
# JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
# JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
# JIRA_PAT       = os.getenv("JIRA_PAT")

# JIRA_SESSION = requests.Session()
# if JIRA_EMAIL and JIRA_API_TOKEN:
#     JIRA_SESSION.auth = (JIRA_EMAIL, JIRA_API_TOKEN)   # Basic auth (Jira Cloud)
# elif JIRA_PAT:
#     JIRA_SESSION.headers.update({"Authorization": f"Bearer {JIRA_PAT}"})
# JIRA_SESSION.headers.update({"Content-Type": "application/json"})

# TARGET_FIELD = "customfield_13034"   # "Default FR Field" (ADF paragraph field)
# FR_KEY_FIELD = "customfield_13038"   # "FR Key"


# # =============================================================
# # Jira helper (GET) — self-contained, no external module
# # =============================================================
# def jira_get(path, params=None):
#     url = f"{JIRA_BASE_URL}/rest/{path}"
#     resp = JIRA_SESSION.get(url, params=params, timeout=30)
#     data = resp.json() if resp.content else None
#     return data, resp.status_code


# def jira_post(path, payload):
#     url = f"{JIRA_BASE_URL}/rest/{path}"
#     resp = JIRA_SESSION.post(url, json=payload, timeout=30)
#     data = resp.json() if resp.content else None
#     return data, resp.status_code


# # =============================================================
# # STEP 1 — build the JQL from the user's input
# # =============================================================
# def build_jql(field_value, indepth_clause=None):
#     """
#     No indepth search:
#         "Default FR Field[Paragraph]" ~ "\"{field_value} : \""

#     Indepth search (extra clause ANDed on):
#         "Default FR Field[Paragraph]" ~ "\"{field_value}\"" and {indepth_clause}
#     """
#     if indepth_clause:
#         base = f'"Default FR Field[Paragraph]" ~ "\\"{field_value}\\""'
#         return f"{base} and {indepth_clause}"

#     base = f'"Default FR Field[Paragraph]" ~ "\\"{field_value} : \\""'
#     return base


# # =============================================================
# # STEP 2 — flatten a paragraph's ADF content into plain text
# # =============================================================
# def _paragraph_plain_text(node):
#     parts = []
#     for child in node.get("content", []) or []:
#         if child.get("type") == "text":
#             parts.append(child.get("text", ""))
#         elif child.get("type") == "hardBreak":
#             parts.append("\n")
#     return "".join(parts)


# def matching_paragraph_text(adf_doc, field_value):
#     """Find and return the plain text of the paragraph(s) in TARGET_FIELD
#     whose text contains field_value, for display/verification."""
#     if not adf_doc:
#         return []
#     matches = []
#     for node in adf_doc.get("content", []) or []:
#         if node.get("type") != "paragraph":
#             continue
#         text = _paragraph_plain_text(node)
#         if field_value in text:
#             matches.append(text)
#     return matches


# # =============================================================
# # STEP 3a — quick pass: get just the matching issue KEYS
# # =============================================================
# def search_issue_keys(jql):
#     """
#     Uses the new POST /rest/api/3/search/jql endpoint (the old GET
#     /rest/api/3/search was removed by Atlassian). This endpoint pages
#     with a nextPageToken instead of startAt.

#     Only asks for "key" here, so this pass is small/fast even when the
#     JQL matches a lot of issues — the heavier field data is fetched
#     afterwards, in parallel, per issue.
#     """
#     keys = []
#     next_page_token = None
#     page_num = 0

#     while True:
#         payload = {
#             "jql": jql,
#             "maxResults": 100,
#             "fields": ["key"],
#         }
#         if next_page_token:
#             payload["nextPageToken"] = next_page_token

#         data, status_code = jira_post("api/3/search/jql", payload)
#         if status_code != 200 or not data:
#             raise RuntimeError(f"Search failed (status {status_code}): {data}")

#         page_issues = data.get("issues", [])
#         keys.extend(issue.get("key") for issue in page_issues)

#         page_num += 1
#         print(f"  ...page {page_num}: {len(keys)} issue(s) found so far")

#         next_page_token = data.get("nextPageToken")
#         is_last = data.get("isLast", not next_page_token)

#         if is_last or not page_issues:
#             break

#     return keys


# # =============================================================
# # STEP 3b — parallel pass: fetch FR Key + Default FR Field for
# #            each matched issue, WORKER_COUNT at a time
# # =============================================================
# def fetch_issue_fields(jira_key, fields=(FR_KEY_FIELD, TARGET_FIELD)):
#     data, status_code = jira_get(f"api/3/issue/{jira_key}", params={"fields": ",".join(fields)})
#     if status_code != 200 or not data:
#         raise RuntimeError(f"[{jira_key}] fetch failed (status {status_code}): {data}")
#     return {"key": jira_key, "fields": data.get("fields", {})}


# def fetch_all_issue_details(keys, worker_count=WORKER_COUNT):
#     results = [None] * len(keys)
#     failures = []
#     completed = 0
#     total = len(keys)
#     lock = threading.Lock()

#     with ThreadPoolExecutor(max_workers=worker_count) as pool:
#         future_to_index = {
#             pool.submit(fetch_issue_fields, key): i for i, key in enumerate(keys)
#         }

#         for future in as_completed(future_to_index):
#             index = future_to_index[future]
#             key = keys[index]
#             try:
#                 results[index] = future.result()
#             except Exception as e:
#                 failures.append((key, str(e)))

#             with lock:
#                 completed += 1
#                 print(f"  ...completed {completed}/{total}")

#     issues = [r for r in results if r is not None]
#     return issues, failures


# # =============================================================
# # MAIN
# # =============================================================
# def main():
#     if not JIRA_BASE_URL or not ((JIRA_EMAIL and JIRA_API_TOKEN) or JIRA_PAT):
#         print("ERROR: Jira credentials missing. Set JIRA_BASE_URL and either "
#               "(JIRA_EMAIL + JIRA_API_TOKEN) or JIRA_PAT in .env")
#         return 1

#     field_value = input('Enter the value to search for (e.g. cf_release_quarter): ').strip()
#     if not field_value:
#         print("No value given, exiting.")
#         return 1

#     indepth_answer = input("Do you need to add indepth search? (yes/no): ").strip().lower()
#     indepth_clause = None
#     if indepth_answer in ("y", "yes"):
#         indepth_clause = input(
#             'Enter the extra JQL clause to AND on '
#             '(e.g. "FR key[Short text]" IS NOT EMPTY): '
#         ).strip()
#         if not indepth_clause:
#             print("No indepth clause given — continuing without it.")
#             indepth_clause = None

#     jql = build_jql(field_value, indepth_clause)
#     print(f"\nJQL:\n  {jql}\n")

#     print("Searching for matching issue keys...")
#     try:
#         keys = search_issue_keys(jql)
#     except Exception as e:
#         print(f"ERROR: {e}")
#         return 1

#     total = len(keys)
#     print(f"\n{total} issue(s) matched. Fetching field data with {WORKER_COUNT} parallel workers...")

#     issues, failures = fetch_all_issue_details(keys)

#     print(f"\nFound {len(issues)}/{total} issue(s) (fetched successfully):\n")
#     for issue in sorted(issues, key=lambda x: x["key"]):
#         key = issue["key"]
#         fields = issue.get("fields", {}) or {}
#         fr_key = fields.get(FR_KEY_FIELD) or "-"
#         target_adf = fields.get(TARGET_FIELD)
#         matches = matching_paragraph_text(target_adf, field_value)

#         print(f"  - {key}  (FR Key: {fr_key})")
#         if matches:
#             for m in matches:
#                 print(f"      matched paragraph: {m}")
#         else:
#             print(f"      (matched by JQL, but no paragraph text containing "
#                   f"'{field_value}' found in {TARGET_FIELD} fields returned)")

#     if failures:
#         print(f"\n{len(failures)} issue(s) failed to fetch:")
#         for key, err in failures:
#             print(f"  ✖ {key}: {err}")

#     print("\n" + "=" * 60)
#     print(f"Total matched by JQL: {total}")
#     print(f"Successfully fetched:  {len(issues)}")
#     if failures:
#         print(f"Failed to fetch:       {len(failures)}")
#     print("=" * 60)

#     return 0


# if __name__ == "__main__":
#     sys.exit(main())
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font
from dotenv import load_dotenv

load_dotenv(override=True)

WORKER_COUNT = 20   # parallel threads used to fetch each issue's field data

USER_EXPORT_DEFAULT = "export-users.csv"   # columns: User id, User name, email, User status
OUTPUT_FOLDER = "jql_output"

# =============================================================
# CONFIG — Jira (same .env as the other scripts)
# =============================================================
JIRA_BASE_URL  = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PAT       = os.getenv("JIRA_PAT")

JIRA_SESSION = requests.Session()
if JIRA_EMAIL and JIRA_API_TOKEN:
    JIRA_SESSION.auth = (JIRA_EMAIL, JIRA_API_TOKEN)   # Basic auth (Jira Cloud)
elif JIRA_PAT:
    JIRA_SESSION.headers.update({"Authorization": f"Bearer {JIRA_PAT}"})
JIRA_SESSION.headers.update({"Content-Type": "application/json"})

TARGET_FIELD = "customfield_13034"   # "Default FR Field" (ADF paragraph field)
FR_KEY_FIELD = "customfield_13038"   # "FR Key"


# =============================================================
# Jira helper (GET) — self-contained, no external module
# =============================================================
def jira_get(path, params=None):
    url = f"{JIRA_BASE_URL}/rest/{path}"
    resp = JIRA_SESSION.get(url, params=params, timeout=30)
    data = resp.json() if resp.content else None
    return data, resp.status_code


def jira_post(path, payload):
    url = f"{JIRA_BASE_URL}/rest/{path}"
    resp = JIRA_SESSION.post(url, json=payload, timeout=30)
    data = resp.json() if resp.content else None
    return data, resp.status_code


# =============================================================
# STEP 1 — build the JQL from the user's input
# =============================================================
def build_jql(field_value, indepth_clause=None):
    """
    No indepth search:
        "Default FR Field[Paragraph]" ~ "\"{field_value} : \""

    Indepth search (extra clause ANDed on):
        "Default FR Field[Paragraph]" ~ "\"{field_value}\"" and {indepth_clause}
    """
    if indepth_clause:
        base = f'"Default FR Field[Paragraph]" ~ "\\"{field_value}\\""'
        return f"{base} and {indepth_clause}"

    base = f'"Default FR Field[Paragraph]" ~ "\\"{field_value} : \\""'
    return base


# =============================================================
# STEP 2 — flatten a paragraph's ADF content into plain text
# =============================================================
def _paragraph_plain_text(node):
    parts = []
    for child in node.get("content", []) or []:
        if child.get("type") == "text":
            parts.append(child.get("text", ""))
        elif child.get("type") == "hardBreak":
            parts.append("\n")
    return "".join(parts)


def matching_paragraph_text(adf_doc, field_value):
    """Find and return the plain text of the paragraph(s) in TARGET_FIELD
    whose text contains field_value, for display/verification."""
    if not adf_doc:
        return []
    matches = []
    for node in adf_doc.get("content", []) or []:
        if node.get("type") != "paragraph":
            continue
        text = _paragraph_plain_text(node)
        if field_value in text:
            matches.append(text)
    return matches


def parse_label_value(text):
    """'cf_qa_owner : Vikraman Ramalingam' -> ('cf_qa_owner', 'Vikraman Ramalingam')"""
    if " : " in text:
        label, _, value = text.partition(" : ")
        return label.strip(), value.strip()
    return text.strip(), ""


# =============================================================
# STEP — load the User name -> User id lookup from export-user.xlsx
# =============================================================
def load_user_lookup(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        df = pd.read_csv(path, dtype=str)
    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        raise RuntimeError(f"Unsupported file type '{ext}' — use .xlsx or .csv")

    df.columns = [str(c).strip() for c in df.columns]

    required = {"User name", "User id", "User status"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(
            f"'{path}' must have {sorted(required)} columns "
            f"(missing: {sorted(missing)}; found: {list(df.columns)})"
        )

    lookup = {}
    for _, row in df.iterrows():
        name = str(row.get("User name") or "").strip()
        user_id = str(row.get("User id") or "").strip()
        status = str(row.get("User status") or "").strip()
        if name:
            lookup[name.lower()] = {"user_id": user_id, "status": status}
    return lookup


def normalize_status(status):
    """Excel might say Active/Inactive/blank/etc. — collapse to 'Active' or 'In-active'."""
    return "Active" if str(status).strip().lower().startswith("active") else "In-active"


# =============================================================
# STEP — write Issue Key / Input Value / Value / User Id to xlsx
# =============================================================
def write_output_excel(rows, field_value):
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)
    safe_value = "".join(c if c.isalnum() or c in "-_" else "_" for c in field_value)
    out_path = os.path.join(OUTPUT_FOLDER, f"{safe_value}_lookup.xlsx")

    headers = ["Issue Key", "Input Value", "Value", "User Id", "User Status"]

    wb = Workbook()
    ws = wb.active
    ws.title = "Results"

    header_font = Font(name="Arial", bold=True)
    body_font = Font(name="Arial")

    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font

    for r, row in enumerate(rows, start=2):
        for c, header in enumerate(headers, start=1):
            cell = ws.cell(row=r, column=c, value=row.get(header, ""))
            cell.font = body_font

    for col_cells in ws.columns:
        values = [str(cell.value) for cell in col_cells if cell.value is not None]
        width = max((len(v) for v in values), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(width + 2, 12), 60)

    wb.save(out_path)
    return out_path


# =============================================================
# STEP 3a — quick pass: get just the matching issue KEYS
# =============================================================
def search_issue_keys(jql):
    """
    Uses the new POST /rest/api/3/search/jql endpoint (the old GET
    /rest/api/3/search was removed by Atlassian). This endpoint pages
    with a nextPageToken instead of startAt.

    Only asks for "key" here, so this pass is small/fast even when the
    JQL matches a lot of issues — the heavier field data is fetched
    afterwards, in parallel, per issue.
    """
    keys = []
    next_page_token = None
    page_num = 0

    while True:
        payload = {
            "jql": jql,
            "maxResults": 100,
            "fields": ["key"],
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        data, status_code = jira_post("api/3/search/jql", payload)
        if status_code != 200 or not data:
            raise RuntimeError(f"Search failed (status {status_code}): {data}")

        page_issues = data.get("issues", [])
        keys.extend(issue.get("key") for issue in page_issues)

        page_num += 1
        print(f"  ...page {page_num}: {len(keys)} issue(s) found so far")

        next_page_token = data.get("nextPageToken")
        is_last = data.get("isLast", not next_page_token)

        if is_last or not page_issues:
            break

    return keys


# =============================================================
# STEP 3b — parallel pass: fetch FR Key + Default FR Field for
#            each matched issue, WORKER_COUNT at a time
# =============================================================
def fetch_issue_fields(jira_key, fields=(FR_KEY_FIELD, TARGET_FIELD)):
    data, status_code = jira_get(f"api/3/issue/{jira_key}", params={"fields": ",".join(fields)})
    if status_code != 200 or not data:
        raise RuntimeError(f"[{jira_key}] fetch failed (status {status_code}): {data}")
    return {"key": jira_key, "fields": data.get("fields", {})}


def fetch_all_issue_details(keys, worker_count=WORKER_COUNT):
    results = [None] * len(keys)
    failures = []
    completed = 0
    total = len(keys)
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_to_index = {
            pool.submit(fetch_issue_fields, key): i for i, key in enumerate(keys)
        }

        for future in as_completed(future_to_index):
            index = future_to_index[future]
            key = keys[index]
            try:
                results[index] = future.result()
            except Exception as e:
                failures.append((key, str(e)))

            with lock:
                completed += 1
                print(f"  ...completed {completed}/{total}")

    issues = [r for r in results if r is not None]
    return issues, failures


# =============================================================
# MAIN
# =============================================================
def main():
    if not JIRA_BASE_URL or not ((JIRA_EMAIL and JIRA_API_TOKEN) or JIRA_PAT):
        print("ERROR: Jira credentials missing. Set JIRA_BASE_URL and either "
              "(JIRA_EMAIL + JIRA_API_TOKEN) or JIRA_PAT in .env")
        return 1

    field_value = input('Enter the value to search for (e.g. cf_release_quarter): ').strip()
    if not field_value:
        print("No value given, exiting.")
        return 1

    indepth_answer = input("Do you need to add indepth search? (yes/no): ").strip().lower()
    indepth_clause = None
    if indepth_answer in ("y", "yes"):
        indepth_clause = input(
            'Enter the extra JQL clause to AND on '
            '(e.g. "FR key[Short text]" IS NOT EMPTY): '
        ).strip()
        if not indepth_clause:
            print("No indepth clause given — continuing without it.")
            indepth_clause = None

    jql = build_jql(field_value, indepth_clause)
    print(f"\nJQL:\n  {jql}\n")

    export_path = input(
        f'Path to export-user file, .xlsx or .csv (default: {USER_EXPORT_DEFAULT}): '
    ).strip() or USER_EXPORT_DEFAULT

    try:
        user_lookup = load_user_lookup(export_path)
    except Exception as e:
        print(f"ERROR loading '{export_path}': {e}")
        return 1

    print("Searching for matching issue keys...")
    try:
        keys = search_issue_keys(jql)
    except Exception as e:
        print(f"ERROR: {e}")
        return 1

    total = len(keys)
    print(f"\n{total} issue(s) matched. Fetching field data with {WORKER_COUNT} parallel workers...")

    issues, failures = fetch_all_issue_details(keys)

    print(f"\nFound {len(issues)}/{total} issue(s) (fetched successfully):\n")

    rows = []
    not_found_count = 0

    for issue in sorted(issues, key=lambda x: x["key"]):
        key = issue["key"]
        fields = issue.get("fields", {}) or {}
        fr_key = fields.get(FR_KEY_FIELD) or "-"
        target_adf = fields.get(TARGET_FIELD)
        matches = matching_paragraph_text(target_adf, field_value)

        print(f"  - {key}  (FR Key: {fr_key})")
        if matches:
            for m in matches:
                print(f"      matched paragraph: {m}")
                label, value = parse_label_value(m)
                match = user_lookup.get(value.lower())
                if match is None:
                    user_id = "NOT FOUND"
                    user_status = "In-active"
                    not_found_count += 1
                else:
                    user_id = match["user_id"]
                    user_status = normalize_status(match["status"])
                rows.append({
                    "Issue Key": key,
                    "Input Value": field_value,
                    "Value": value,
                    "User Id": user_id,
                    "User Status": user_status,
                })
        else:
            print(f"      (matched by JQL, but no paragraph text containing "
                  f"'{field_value}' found in {TARGET_FIELD} fields returned)")

    if failures:
        print(f"\n{len(failures)} issue(s) failed to fetch:")
        for key, err in failures:
            print(f"  ✖ {key}: {err}")

    out_path = write_output_excel(rows, field_value)

    print("\n" + "=" * 60)
    print(f"Total matched by JQL: {total}")
    print(f"Successfully fetched:  {len(issues)}")
    if failures:
        print(f"Failed to fetch:       {len(failures)}")
    print(f"Rows written:          {len(rows)}")
    if not_found_count:
        print(f"User id not found for: {not_found_count} row(s)")
    print(f"Output file:            {out_path}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())