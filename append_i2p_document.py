# """
# append_i2p_document.py  (STANDALONE — no dependency on any other script)

# For each Jira key you provide:

#     1. GET the Jira issue, read customfield_13038 (the "FR Key" —
#        source-system key, e.g. "FD-197277").
#     2. Call the backend:  {BACKEND_BASE_URL}/{BACKEND_PROJECT_KEY}/issues/{fr_key}
#        and pull "cf_i2p_document" out of the returned JSON. This value
#        is an HTML snippet, e.g.:
#            <p><a href="https://...">https://...</a></p>
#     3. Convert that HTML into ADF (Atlassian Document Format) nodes,
#        since customfield_13034 is a rich-text (paragraph) field and
#        Jira's API only accepts ADF for such fields, not raw HTML.
#     4. GET the issue's CURRENT customfield_13034 value (its existing
#        ADF document).
#     5. Append the new ADF node(s) to the END of the existing
#        document's "content" array — nothing existing is removed or
#        modified.
#     6. PUT the issue back to Jira with the merged ADF document.

# -----------------------------------------------------------------------
# .env variables needed
# -----------------------------------------------------------------------
# # Jira — fill in EITHER the email+token pair (Jira Cloud) OR the PAT
# # (Jira Server/Data Center / personal access token). If both are
# # present, the API token pair wins.
# JIRA_BASE_URL=https://yourcompany.atlassian.net
# JIRA_EMAIL=you@yourcompany.com
# JIRA_API_TOKEN=xxxxxxxxxxxxxxxx
# # JIRA_PAT=xxxxxxxxxxxxxxxx          # alternative to email+token

# # Backend (source system)
# BACKEND_BASE_URL=https://backend.example.com
# BACKEND_AUTH_TOKEN=xxxxxxxxxxxxxxxx
# BACKEND_PROJECT_KEY=FD
# -----------------------------------------------------------------------

# Usage:
#     python append_i2p_document.py PLG-4129 PLG-4130 PLG-4131
#     python append_i2p_document.py --keys-file keys.txt
#     python append_i2p_document.py PLG-4129 --dry-run
# """

# import os
# import sys
# import json
# import argparse

# import requests
# from bs4 import BeautifulSoup, NavigableString, Tag
# from dotenv import load_dotenv

# load_dotenv(override=True)

# # =============================================================
# # CONFIG — Jira
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

# # =============================================================
# # CONFIG — Backend (source system)
# # =============================================================
# BACKEND_BASE_URL    = os.getenv("BACKEND_BASE_URL", "").rstrip("/")
# BACKEND_AUTH_TOKEN  = os.getenv("BACKEND_AUTH_TOKEN") or os.getenv("AUTH_TOKEN")
# BACKEND_PROJECT_KEY = os.getenv("BACKEND_PROJECT_KEY") or os.getenv("project_key")

# BACKEND_HEADERS = {
#     "Authorization": f"Token {BACKEND_AUTH_TOKEN}",
#     "Content-Type":  "application/json",
#     "Accept":        "application/json",
# }

# FR_KEY_FIELD    = "customfield_13038"   # "FR Key" field on the Jira issue
# TARGET_FIELD    = "customfield_13034"   # "Default FR Field" (ADF paragraph field)
# BACKEND_DOC_KEY = "cf_i2p_document"     # field inside the backend JSON we want


# # =============================================================
# # Jira helpers (GET / PUT) — self-contained, no external module
# # =============================================================
# def jira_get(path):
#     url = f"{JIRA_BASE_URL}/rest/{path}"
#     resp = JIRA_SESSION.get(url, timeout=30)
#     data = resp.json() if resp.content else None
#     return data, resp.status_code


# def jira_put(path, payload):
#     url = f"{JIRA_BASE_URL}/rest/{path}"
#     resp = JIRA_SESSION.put(url, json=payload, params={"notifyUsers": "false"}, timeout=30)
#     data = resp.json() if resp.content else None
#     return data, resp.status_code


# # =============================================================
# # STEP 1 — Jira: get the FR key for a given Jira issue
# # =============================================================
# def get_fr_key(jira_key):
#     data, status_code = jira_get(f"api/3/issue/{jira_key}?fields={FR_KEY_FIELD}")
#     if status_code != 200 or not data:
#         raise RuntimeError(f"[{jira_key}] Failed to fetch {FR_KEY_FIELD} (status {status_code}): {data}")

#     fr_key = data.get("fields", {}).get(FR_KEY_FIELD)
#     if not fr_key:
#         raise RuntimeError(f"[{jira_key}] {FR_KEY_FIELD} is empty — nothing to look up in backend")

#     return fr_key


# # =============================================================
# # STEP 2 — Backend: get cf_i2p_document for that FR key
# # =============================================================
# def get_backend_i2p_document(fr_key):
#     url = f"{BACKEND_BASE_URL}/{BACKEND_PROJECT_KEY}/issues/{fr_key}.json"
#     resp = requests.get(url, headers=BACKEND_HEADERS, timeout=30)

#     try:
#         resp.raise_for_status()
#     except requests.exceptions.HTTPError as e:
#         raise RuntimeError(
#             f"Backend GET {url} returned {resp.status_code}. "
#             f"Body (first 300 chars): {resp.text[:300]!r}"
#         ) from e

#     try:
#         data = resp.json()
#     except json.JSONDecodeError as e:
#         raise RuntimeError(
#             f"Backend GET {url} returned status {resp.status_code} but the body "
#             f"isn't JSON (Content-Type: {resp.headers.get('Content-Type')}). "
#             f"Body (first 300 chars): {resp.text[:300]!r}"
#         ) from e

#     # Freshrelease wraps the issue under "issue", and custom fields
#     # (including cf_i2p_document) live under "issue.custom_field".
#     issue = data.get("issue", data)
#     custom_fields = issue.get("custom_field", {})

#     html_value = custom_fields.get(BACKEND_DOC_KEY)
#     if not html_value:
#         raise RuntimeError(f"[{fr_key}] Backend has no value for {BACKEND_DOC_KEY}")

#     return html_value


# # =============================================================
# # STEP 3 — HTML -> ADF conversion
# # =============================================================
# def _inline_nodes_from_tag(tag, marks=None):
#     """Recursively walk inline content (text, <a>, <strong>, <em>, <br>)
#     and yield ADF 'text' nodes carrying the right marks."""
#     marks = marks or []

#     if isinstance(tag, NavigableString):
#         text = str(tag)
#         if text.strip() == "" and text != "\n":
#             return
#         if text:
#             node = {"type": "text", "text": text}
#             if marks:
#                 node["marks"] = marks
#             yield node
#         return

#     if not isinstance(tag, Tag):
#         return

#     if tag.name == "br":
#         yield {"type": "hardBreak"}
#         return

#     new_marks = list(marks)
#     if tag.name == "a" and tag.get("href"):
#         new_marks = new_marks + [{"type": "link", "attrs": {"href": tag["href"]}}]
#     elif tag.name in ("strong", "b"):
#         new_marks = new_marks + [{"type": "strong"}]
#     elif tag.name in ("em", "i"):
#         new_marks = new_marks + [{"type": "em"}]

#     for child in tag.children:
#         yield from _inline_nodes_from_tag(child, new_marks)


# def _list_item_to_adf(li_tag):
#     content = list(_inline_nodes_from_tag(li_tag))
#     if not content:
#         content = [{"type": "text", "text": ""}]
#     return {
#         "type": "listItem",
#         "content": [{"type": "paragraph", "content": content}],
#     }


# def html_to_adf_nodes(html):
#     """
#     Converts an HTML snippet (as returned in cf_i2p_document) into a
#     list of top-level ADF block nodes: paragraph / bulletList / orderedList.
#     Falls back to a single plain-text paragraph if the HTML has no
#     recognizable block tags at all.
#     """
#     soup = BeautifulSoup(html, "html.parser")
#     nodes = []

#     top_level = [c for c in soup.children if not (isinstance(c, NavigableString) and not c.strip())]

#     if not top_level:
#         return [{"type": "paragraph", "content": [{"type": "text", "text": html.strip()}]}]

#     for el in top_level:
#         if isinstance(el, NavigableString):
#             text = str(el).strip()
#             if text:
#                 nodes.append({"type": "paragraph", "content": [{"type": "text", "text": text}]})
#             continue

#         if el.name == "p":
#             content = list(_inline_nodes_from_tag(el))
#             if not content:
#                 content = [{"type": "text", "text": ""}]
#             nodes.append({"type": "paragraph", "content": content})

#         elif el.name in ("ul", "ol"):
#             items = [_list_item_to_adf(li) for li in el.find_all("li", recursive=False)]
#             list_type = "bulletList" if el.name == "ul" else "orderedList"
#             nodes.append({"type": list_type, "content": items})

#         else:
#             # Unknown block tag — treat its inline content as one paragraph
#             content = list(_inline_nodes_from_tag(el))
#             if content:
#                 nodes.append({"type": "paragraph", "content": content})

#     if not nodes:
#         nodes = [{"type": "paragraph", "content": [{"type": "text", "text": html.strip()}]}]

#     return nodes


# def build_i2p_document_paragraph(html):
#     """
#     Builds the single ADF paragraph to append for cf_i2p_document, in the
#     same "field_name : value" style as the other fields already in
#     customfield_13034 (e.g. "cf_business_impact : Product Tier 1").

#     Formatting rules for the value part:
#       - If the HTML is just a bare link where the visible text equals the
#         href (e.g. <p><a href="URL">URL</a></p>) -> show the URL alone,
#         hyperlinked.
#       - If the HTML is a link with different visible text
#         (e.g. <a href="URL">Some Label</a>) -> show "Some Label (URL)",
#         with the URL hyperlinked.
#       - If there's no <a> tag at all -> show the plain text.
#     """
#     soup = BeautifulSoup(html, "html.parser")
#     label_prefix = {"type": "text", "text": f"{BACKEND_DOC_KEY} : "}

#     anchors = soup.find_all("a")

#     if len(anchors) == 1 and not soup.get_text(strip=True).replace(anchors[0].get_text(strip=True), "").strip():
#         # Exactly one link, and it's the only meaningful text in the HTML
#         a = anchors[0]
#         href = (a.get("href") or "").strip()
#         link_text = a.get_text(strip=True)

#         if link_text == href:
#             value_nodes = [
#                 {"type": "text", "text": href, "marks": [{"type": "link", "attrs": {"href": href}}]}
#             ]
#         else:
#             value_nodes = [
#                 {"type": "text", "text": f"{link_text} ("},
#                 {"type": "text", "text": href, "marks": [{"type": "link", "attrs": {"href": href}}]},
#                 {"type": "text", "text": ")"},
#             ]
#     elif anchors:
#         # Multiple links or a link mixed with other text — fall back to
#         # rendering everything inline (links stay clickable, other text plain)
#         value_nodes = list(_inline_nodes_from_tag(soup))
#     else:
#         # No links at all — plain text
#         plain_text = soup.get_text(strip=True)
#         value_nodes = [{"type": "text", "text": plain_text}]

#     return {"type": "paragraph", "content": [label_prefix] + value_nodes}


# # =============================================================
# # STEP 4 — Jira: get current ADF value of customfield_13034
# # =============================================================
# def get_current_adf(jira_key):
#     data, status_code = jira_get(f"api/3/issue/{jira_key}?fields={TARGET_FIELD}")
#     if status_code != 200 or not data:
#         raise RuntimeError(f"[{jira_key}] Failed to fetch {TARGET_FIELD} (status {status_code}): {data}")

#     current = data.get("fields", {}).get(TARGET_FIELD)
#     if not current:
#         # Field is empty — start a fresh ADF doc
#         current = {"type": "doc", "version": 1, "content": []}
#     return current


# # =============================================================
# # STEP 5 — merge: append new nodes to existing content, untouched
# # =============================================================
# def append_adf_nodes(existing_doc, new_nodes):
#     merged = dict(existing_doc)  # shallow copy — don't mutate caller's dict
#     merged["content"] = list(existing_doc.get("content", [])) + new_nodes
#     return merged


# # =============================================================
# # STEP 6 — Jira: PUT the merged ADF doc back
# # =============================================================
# def update_jira_field(jira_key, merged_adf_doc, dry_run=False):
#     payload = {"fields": {TARGET_FIELD: merged_adf_doc}}

#     if dry_run:
#         preview_path = f"/tmp/{jira_key}_dry_run_payload.json"
#         with open(preview_path, "w", encoding="utf-8") as f:
#             json.dump(payload, f, indent=2)
#         print(f"  [DRY RUN] Full payload written to {preview_path} "
#               f"({len(json.dumps(payload))} chars total)")
#         print(f"  [DRY RUN] Would PUT with query param notifyUsers=false "
#               f"(no email/notifications will be sent to watchers or assignees)")
#         return True

#     data, status_code = jira_put(f"api/3/issue/{jira_key}", payload)

#     # Jira's PUT /issue endpoint normally returns 204 No Content on success
#     if status_code not in (200, 204):
#         raise RuntimeError(f"[{jira_key}] PUT failed (status {status_code}): {data}")

#     return True


# # =============================================================
# # MAIN — one Jira key end-to-end
# # =============================================================
# def process_key(jira_key, dry_run=False):
#     print(f"\n--- {jira_key} ---")

#     fr_key = get_fr_key(jira_key)
#     print(f"  FR Key ({FR_KEY_FIELD}): {fr_key}")

#     html_value = get_backend_i2p_document(fr_key)
#     print(f"  Backend {BACKEND_DOC_KEY}: {html_value[:120]}{'...' if len(html_value) > 120 else ''}")

#     new_nodes = [build_i2p_document_paragraph(html_value)]

#     current_adf = get_current_adf(jira_key)
#     existing_block_count = len(current_adf.get("content", []))

#     merged_adf = append_adf_nodes(current_adf, new_nodes)

#     update_jira_field(jira_key, merged_adf, dry_run=dry_run)

#     print(
#         f"  ✔ {'[DRY RUN] Would append' if dry_run else 'Appended'} "
#         f"{len(new_nodes)} block(s) to {TARGET_FIELD} "
#         f"({existing_block_count} existing block(s) preserved)"
#     )


# def main():
#     parser = argparse.ArgumentParser(
#         description="Append backend cf_i2p_document into Jira customfield_13034, preserving existing content"
#     )
#     parser.add_argument("keys", nargs="*", help="Jira issue keys, e.g. PLG-4129 PLG-4130")
#     parser.add_argument("--keys-file", default=None, help="Path to a text file with one Jira key per line")
#     parser.add_argument("--dry-run", action="store_true", help="Don't actually PUT — just show what would happen")
#     args = parser.parse_args()

#     jira_keys = list(args.keys)
#     if args.keys_file:
#         with open(args.keys_file, "r", encoding="utf-8") as f:
#             jira_keys.extend(line.strip() for line in f if line.strip())

#     if not jira_keys:
#         print("No Jira keys given. Pass them as arguments or via --keys-file.")
#         return 1

#     if not JIRA_BASE_URL or not ((JIRA_EMAIL and JIRA_API_TOKEN) or JIRA_PAT):
#         print("ERROR: Jira credentials missing. Set JIRA_BASE_URL and either "
#               "(JIRA_EMAIL + JIRA_API_TOKEN) or JIRA_PAT in .env")
#         return 1

#     if not BACKEND_BASE_URL or not BACKEND_AUTH_TOKEN or not BACKEND_PROJECT_KEY:
#         print("ERROR: Backend credentials missing. Set BACKEND_BASE_URL, "
#               "BACKEND_AUTH_TOKEN, BACKEND_PROJECT_KEY in .env")
#         return 1

#     failures = []
#     for jira_key in jira_keys:
#         try:
#             process_key(jira_key, dry_run=args.dry_run)
#         except Exception as e:
#             print(f"  ✖ {jira_key} FAILED: {e}")
#             failures.append(jira_key)

#     print("\n" + "=" * 60)
#     print(f"Done. {len(jira_keys) - len(failures)}/{len(jira_keys)} succeeded.")
#     if failures:
#         print(f"Failed: {failures}")
#     print("=" * 60)

#     return 1 if failures else 0


# if __name__ == "__main__":
#     sys.exit(main())
"""
append_i2p_document.py  (STANDALONE — no dependency on any other script)

For each Jira key you provide:

    1. GET the Jira issue, read customfield_13038 (the "FR Key" —
       source-system key, e.g. "FD-197277").
    2. Derive the backend project key straight from that FR key itself
       (e.g. "FD-197277" -> project key "FD"), since different Jira
       issues can point at different source-system projects — nothing
       is hardcoded or read from .env for this.
    3. Call the backend:  {BACKEND_BASE_URL}/{project_key}/issues/{fr_key}.json
       and pull "cf_i2p_document" out of the returned JSON
       (issue.custom_field.cf_i2p_document). This value is an HTML
       snippet, e.g.:
           <p><a href="https://...">https://...</a></p>
    4. Convert that HTML into a single ADF (Atlassian Document Format)
       paragraph, labeled "cf_i2p_document : ...", since
       customfield_13034 is a rich-text field and Jira's API only
       accepts ADF for such fields, not raw HTML.
    5. GET the issue's CURRENT customfield_13034 value (its existing
       ADF document).
    6. Append the new ADF node to the END of the existing document's
       "content" array — nothing existing is removed or modified.
    7. PUT the issue back to Jira with:
         - the merged ADF document for customfield_13034
         - customfield_13041 ("FR Roadmap classification migration",
           plain text) cleared to an empty string
         - the query param notifyUsers=false, so watchers/assignees/
           reporters get NO email or in-app notification for the update

-----------------------------------------------------------------------
.env variables needed
-----------------------------------------------------------------------
# Jira — fill in EITHER the email+token pair (Jira Cloud) OR the PAT
# (Jira Server/Data Center / personal access token). If both are
# present, the API token pair wins.
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=you@yourcompany.com
JIRA_API_TOKEN=xxxxxxxxxxxxxxxx
# JIRA_PAT=xxxxxxxxxxxxxxxx          # alternative to email+token

# Backend (source system) — NOTE: no project key here anymore, it's
# derived per-issue from that issue's own FR key (see above).
BACKEND_BASE_URL=https://backend.example.com
BACKEND_AUTH_TOKEN=xxxxxxxxxxxxxxxx
-----------------------------------------------------------------------

Usage:
    python append_i2p_document.py PLG-4129 PLG-4130 PLG-4131
    python append_i2p_document.py --keys-file keys.txt
    python append_i2p_document.py PLG-4129 --dry-run
    python append_i2p_document.py                       # uses STATIC_JIRA_KEYS below
"""

import os
import sys
import json
import argparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from dotenv import load_dotenv

load_dotenv(override=True)

# =============================================================
# CONFIG — Jira
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

# =============================================================
# CONFIG — Backend (source system)
# =============================================================
BACKEND_BASE_URL    = os.getenv("BACKEND_BASE_URL", "").rstrip("/")
BACKEND_AUTH_TOKEN  = os.getenv("BACKEND_AUTH_TOKEN") or os.getenv("AUTH_TOKEN")

BACKEND_HEADERS = {
    "Authorization": f"Token {BACKEND_AUTH_TOKEN}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
}

FR_KEY_FIELD     = "customfield_13038"   # "FR Key" field on the Jira issue
TARGET_FIELD     = "customfield_13034"   # "Default FR Field" (ADF paragraph field)
ROADMAP_FIELD    = "customfield_13041"   # "FR Roadmap classification migration" (plain text) — cleared to ""
BACKEND_DOC_KEY  = "cf_i2p_document"      # field inside the backend JSON we want

# =============================================================
# STATIC LIST OF JIRA KEYS
# =============================================================
# Paste your 100+ Jira keys here (one per string, comma-separated).
# These are used automatically if you don't pass any keys on the
# command line and don't use --keys-file. You can still combine this
# with CLI args / --keys-file — all sources are merged, duplicates removed.
STATIC_JIRA_KEYS = [
 "PLG-2798"
 ]


# =============================================================
# Jira helpers (GET / PUT) — self-contained, no external module
# =============================================================
def jira_get(path):
    url = f"{JIRA_BASE_URL}/rest/{path}"
    resp = JIRA_SESSION.get(url, timeout=30)
    data = resp.json() if resp.content else None
    return data, resp.status_code


def jira_put(path, payload):
    url = f"{JIRA_BASE_URL}/rest/{path}"
    resp = JIRA_SESSION.put(url, json=payload, params={"notifyUsers": "false"}, timeout=30)
    data = resp.json() if resp.content else None
    return data, resp.status_code


# =============================================================
# STEP 1 — Jira: get the FR key for a given Jira issue
# =============================================================
def get_fr_key(jira_key):
    data, status_code = jira_get(f"api/3/issue/{jira_key}?fields={FR_KEY_FIELD}")
    if status_code != 200 or not data:
        raise RuntimeError(f"[{jira_key}] Failed to fetch {FR_KEY_FIELD} (status {status_code}): {data}")

    fr_key = data.get("fields", {}).get(FR_KEY_FIELD)
    if not fr_key:
        raise RuntimeError(f"[{jira_key}] {FR_KEY_FIELD} is empty — nothing to look up in backend")

    return fr_key


# =============================================================
# STEP 2 — Backend: get cf_i2p_document for that FR key
# =============================================================
def derive_project_key(fr_key):
    """
    Extracts the backend project key from the FR key itself.
    e.g. "FD-197277" -> "FD", "AI-4501" -> "AI"
    """
    if "-" not in fr_key:
        raise RuntimeError(f"FR key '{fr_key}' doesn't look like PROJECTKEY-NUMBER — can't derive project key")
    project_key = fr_key.split("-", 1)[0]
    if not project_key:
        raise RuntimeError(f"FR key '{fr_key}' has an empty project key portion")
    return project_key


def get_backend_i2p_document(fr_key):
    project_key = derive_project_key(fr_key)
    url = f"{BACKEND_BASE_URL}/{project_key}/issues/{fr_key}.json"
    resp = requests.get(url, headers=BACKEND_HEADERS, timeout=30)

    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"Backend GET {url} returned {resp.status_code}. "
            f"Body (first 300 chars): {resp.text[:300]!r}"
        ) from e

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Backend GET {url} returned status {resp.status_code} but the body "
            f"isn't JSON (Content-Type: {resp.headers.get('Content-Type')}). "
            f"Body (first 300 chars): {resp.text[:300]!r}"
        ) from e

    # Freshrelease wraps the issue under "issue", and custom fields
    # (including cf_i2p_document) live under "issue.custom_field".
    issue = data.get("issue", data)
    custom_fields = issue.get("custom_field", {})

    html_value = custom_fields.get(BACKEND_DOC_KEY)
    if not html_value:
        raise RuntimeError(f"[{fr_key}] Backend has no value for {BACKEND_DOC_KEY}")

    return html_value


# =============================================================
# STEP 3 — HTML -> ADF conversion
# =============================================================
def _inline_nodes_from_tag(tag, marks=None):
    """Recursively walk inline content (text, <a>, <strong>, <em>, <br>)
    and yield ADF 'text' nodes carrying the right marks."""
    marks = marks or []

    if isinstance(tag, NavigableString):
        text = str(tag)
        if text.strip() == "" and text != "\n":
            return
        if text:
            node = {"type": "text", "text": text}
            if marks:
                node["marks"] = marks
            yield node
        return

    if not isinstance(tag, Tag):
        return

    if tag.name == "br":
        yield {"type": "hardBreak"}
        return

    new_marks = list(marks)
    if tag.name == "a" and tag.get("href"):
        new_marks = new_marks + [{"type": "link", "attrs": {"href": tag["href"]}}]
    elif tag.name in ("strong", "b"):
        new_marks = new_marks + [{"type": "strong"}]
    elif tag.name in ("em", "i"):
        new_marks = new_marks + [{"type": "em"}]

    for child in tag.children:
        yield from _inline_nodes_from_tag(child, new_marks)


def _list_item_to_adf(li_tag):
    content = list(_inline_nodes_from_tag(li_tag))
    if not content:
        content = [{"type": "text", "text": ""}]
    return {
        "type": "listItem",
        "content": [{"type": "paragraph", "content": content}],
    }


def html_to_adf_nodes(html):
    """
    Converts an HTML snippet (as returned in cf_i2p_document) into a
    list of top-level ADF block nodes: paragraph / bulletList / orderedList.
    Falls back to a single plain-text paragraph if the HTML has no
    recognizable block tags at all.
    """
    soup = BeautifulSoup(html, "html.parser")
    nodes = []

    top_level = [c for c in soup.children if not (isinstance(c, NavigableString) and not c.strip())]

    if not top_level:
        return [{"type": "paragraph", "content": [{"type": "text", "text": html.strip()}]}]

    for el in top_level:
        if isinstance(el, NavigableString):
            text = str(el).strip()
            if text:
                nodes.append({"type": "paragraph", "content": [{"type": "text", "text": text}]})
            continue

        if el.name == "p":
            content = list(_inline_nodes_from_tag(el))
            if not content:
                content = [{"type": "text", "text": ""}]
            nodes.append({"type": "paragraph", "content": content})

        elif el.name in ("ul", "ol"):
            items = [_list_item_to_adf(li) for li in el.find_all("li", recursive=False)]
            list_type = "bulletList" if el.name == "ul" else "orderedList"
            nodes.append({"type": list_type, "content": items})

        else:
            # Unknown block tag — treat its inline content as one paragraph
            content = list(_inline_nodes_from_tag(el))
            if content:
                nodes.append({"type": "paragraph", "content": content})

    if not nodes:
        nodes = [{"type": "paragraph", "content": [{"type": "text", "text": html.strip()}]}]

    return nodes


def build_i2p_document_paragraph(html):
    """
    Builds the single ADF paragraph to append for cf_i2p_document, in the
    same "field_name : value" style as the other fields already in
    customfield_13034 (e.g. "cf_business_impact : Product Tier 1").

    Formatting rules for the value part:
      - If the HTML is just a bare link where the visible text equals the
        href (e.g. <p><a href="URL">URL</a></p>) -> show the URL alone,
        hyperlinked.
      - If the HTML is a link with different visible text
        (e.g. <a href="URL">Some Label</a>) -> show "Some Label (URL)",
        with the URL hyperlinked.
      - If there's no <a> tag at all -> show the plain text.
    """
    soup = BeautifulSoup(html, "html.parser")
    label_prefix = {"type": "text", "text": f"{BACKEND_DOC_KEY} : "}

    anchors = soup.find_all("a")

    if len(anchors) == 1 and not soup.get_text(strip=True).replace(anchors[0].get_text(strip=True), "").strip():
        # Exactly one link, and it's the only meaningful text in the HTML
        a = anchors[0]
        href = (a.get("href") or "").strip()
        link_text = a.get_text(strip=True)

        if link_text == href:
            value_nodes = [
                {"type": "text", "text": href, "marks": [{"type": "link", "attrs": {"href": href}}]}
            ]
        else:
            value_nodes = [
                {"type": "text", "text": f"{link_text} ("},
                {"type": "text", "text": href, "marks": [{"type": "link", "attrs": {"href": href}}]},
                {"type": "text", "text": ")"},
            ]
    elif anchors:
        # Multiple links or a link mixed with other text — fall back to
        # rendering everything inline (links stay clickable, other text plain)
        value_nodes = list(_inline_nodes_from_tag(soup))
    else:
        # No links at all — plain text
        plain_text = soup.get_text(strip=True)
        value_nodes = [{"type": "text", "text": plain_text}]

    return {"type": "paragraph", "content": [label_prefix] + value_nodes}


# =============================================================
# STEP 4 — Jira: get current ADF value of customfield_13034
# =============================================================
def get_current_adf(jira_key):
    data, status_code = jira_get(f"api/3/issue/{jira_key}?fields={TARGET_FIELD}")
    if status_code != 200 or not data:
        raise RuntimeError(f"[{jira_key}] Failed to fetch {TARGET_FIELD} (status {status_code}): {data}")

    current = data.get("fields", {}).get(TARGET_FIELD)
    if not current:
        # Field is empty — start a fresh ADF doc
        current = {"type": "doc", "version": 1, "content": []}
    return current


# =============================================================
# STEP 5 — merge: append new nodes to existing content, untouched
# =============================================================
def append_adf_nodes(existing_doc, new_nodes):
    merged = dict(existing_doc)  # shallow copy — don't mutate caller's dict
    merged["content"] = list(existing_doc.get("content", [])) + new_nodes
    return merged


# =============================================================
# STEP 6 — Jira: PUT the merged ADF doc back
# =============================================================
def update_jira_field(jira_key, merged_adf_doc, dry_run=False):
    payload = {
        "fields": {
            TARGET_FIELD: merged_adf_doc,
            ROADMAP_FIELD: "",   # clear "FR Roadmap classification migration"
        }
    }

    if dry_run:
        preview_path = f"/tmp/{jira_key}_dry_run_payload.json"
        with open(preview_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"  [DRY RUN] Full payload written to {preview_path} "
              f"({len(json.dumps(payload))} chars total)")
        print(f"  [DRY RUN] Would PUT with query param notifyUsers=false "
              f"(no email/notifications will be sent to watchers or assignees)")
        print(f"  [DRY RUN] Would also clear {ROADMAP_FIELD} (set to empty string)")
        return True

    data, status_code = jira_put(f"api/3/issue/{jira_key}", payload)

    # Jira's PUT /issue endpoint normally returns 204 No Content on success
    if status_code not in (200, 204):
        raise RuntimeError(f"[{jira_key}] PUT failed (status {status_code}): {data}")

    return True


# =============================================================
# MAIN — one Jira key end-to-end
# =============================================================
def process_key(jira_key, dry_run=False):
    print(f"\n--- {jira_key} ---")

    fr_key = get_fr_key(jira_key)
    print(f"  FR Key ({FR_KEY_FIELD}): {fr_key}")

    html_value = get_backend_i2p_document(fr_key)
    print(f"  Backend {BACKEND_DOC_KEY}: {html_value[:120]}{'...' if len(html_value) > 120 else ''}")

    new_nodes = [build_i2p_document_paragraph(html_value)]

    current_adf = get_current_adf(jira_key)
    existing_block_count = len(current_adf.get("content", []))

    merged_adf = append_adf_nodes(current_adf, new_nodes)

    update_jira_field(jira_key, merged_adf, dry_run=dry_run)

    print(
        f"  ✔ {'[DRY RUN] Would append' if dry_run else 'Appended'} "
        f"{len(new_nodes)} block(s) to {TARGET_FIELD} "
        f"({existing_block_count} existing block(s) preserved), "
        f"and {'would clear' if dry_run else 'cleared'} {ROADMAP_FIELD}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Append backend cf_i2p_document into Jira customfield_13034, preserving existing content"
    )
    parser.add_argument("keys", nargs="*", help="Jira issue keys, e.g. PLG-4129 PLG-4130")
    parser.add_argument("--keys-file", default=None, help="Path to a text file with one Jira key per line")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually PUT — just show what would happen")
    args = parser.parse_args()

    jira_keys = list(args.keys)
    if args.keys_file:
        with open(args.keys_file, "r", encoding="utf-8") as f:
            jira_keys.extend(line.strip() for line in f if line.strip())

    # Fall back to the hardcoded STATIC_JIRA_KEYS list if nothing was
    # passed on the command line / via --keys-file. If CLI args or a
    # keys-file WERE given, they're merged with STATIC_JIRA_KEYS too,
    # with duplicates removed, so you can freely combine both.
    jira_keys = list(dict.fromkeys(jira_keys + STATIC_JIRA_KEYS))

    if not jira_keys:
        print("No Jira keys given. Pass them as arguments, via --keys-file, "
              "or paste them into STATIC_JIRA_KEYS at the top of the script.")
        return 1

    if not JIRA_BASE_URL or not ((JIRA_EMAIL and JIRA_API_TOKEN) or JIRA_PAT):
        print("ERROR: Jira credentials missing. Set JIRA_BASE_URL and either "
              "(JIRA_EMAIL + JIRA_API_TOKEN) or JIRA_PAT in .env")
        return 1

    if not BACKEND_BASE_URL or not BACKEND_AUTH_TOKEN:
        print("ERROR: Backend credentials missing. Set BACKEND_BASE_URL and "
              "BACKEND_AUTH_TOKEN in .env")
        return 1

    failures = []
    for jira_key in jira_keys:
        try:
            process_key(jira_key, dry_run=args.dry_run)
        except Exception as e:
            print(f"  ✖ {jira_key} FAILED: {e}")
            failures.append(jira_key)

    print("\n" + "=" * 60)
    print(f"Done. {len(jira_keys) - len(failures)}/{len(jira_keys)} succeeded.")
    if failures:
        print(f"Failed: {failures}")
    print("=" * 60)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())