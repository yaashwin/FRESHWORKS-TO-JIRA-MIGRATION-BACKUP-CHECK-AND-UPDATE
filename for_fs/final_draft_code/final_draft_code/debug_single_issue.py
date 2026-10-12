"""
debug_single_issue.py

Diagnose (and optionally fix) fixVersions for ONE issue, by source_issue_id.
Mirrors fix_issue_versions.py's exact resolution pipeline:

  1. Strict (release_id, project) lookup in versions_mapping.xlsx, where
     project = jira_key with the trailing "-<number>" stripped.
  2. Validate that Excel id actually exists LIVE in that project's version
     list (the Excel has repeatedly been found to carry a stale/shared id
     across EX/PLAT/CEMIG rows for the same release id).
  3. If the Excel id is stale, fall back to a live name match in that same
     project.
  4. If neither resolves, fail with a clear reason — no PUT attempted.

Prints every step so a 400 (or an unexpectedly "already correct" result)
can be understood immediately instead of guessed at.

Usage:
    python debug_single_issue.py 87246                 # inspect only, no write
    python debug_single_issue.py 87246 --apply          # inspect AND attempt the PUT
"""

import os
import sys
import json
import re
import argparse

import requests
import pandas as pd
from dotenv import load_dotenv

try:
    import jira_credentials
    _HAVE_CREDENTIALS_MODULE = True
except Exception:
    _HAVE_CREDENTIALS_MODULE = False

load_dotenv()

BASE_FS_DATA = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data"
ISSUES_META_PATH   = os.path.join(BASE_FS_DATA, "payload", "issues_meta.json")
ID_MAPPING_PATH     = os.path.join(BASE_FS_DATA, "mapping", "id_mapping.json")
VERSIONS_XLSX_PATH = os.path.join(BASE_FS_DATA, "mapping", "versions_mapping.xlsx")

JIRA_URL       = os.getenv("JIRA_URL")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def _auth():
    if _HAVE_CREDENTIALS_MODULE:
        try:
            return jira_credentials.get_auth_for_this_thread()
        except Exception:
            pass
    return (JIRA_EMAIL, JIRA_API_TOKEN)


def jget(endpoint):
    r = requests.get(f"{JIRA_URL}/rest/{endpoint.lstrip('/')}", headers=JIRA_HEADERS, auth=_auth(), timeout=60)
    body = r.json() if r.content else {}
    return body, r.status_code


def jput(endpoint, payload):
    r = requests.put(f"{JIRA_URL}/rest/{endpoint.lstrip('/')}", json=payload, headers=JIRA_HEADERS, auth=_auth(), timeout=60)
    body = r.json() if r.content else {}
    return body, r.status_code


def norm_release_id(value):
    if value is None:
        return None
    s = str(value).strip()
    if s in ("", "nan", "None", "NaT"):
        return None
    try:
        return str(int(float(s)))
    except (ValueError, TypeError):
        return s


def project_from_jira_key(jira_key):
    """ "PLAT-2996" -> "PLAT"  (strip the trailing "-<number>"). """
    if not jira_key:
        return None
    m = re.match(r"^(.*)-\d+$", jira_key.strip())
    return m.group(1) if m else None


def load_versions_excel(path):
    df = pd.read_excel(path, dtype=str)
    df.columns = [c.strip() for c in df.columns]
    col_map = {c.lower(): c for c in df.columns}
    name_col    = col_map.get("version name")
    release_col = col_map.get("source release id")
    project_col = col_map.get("project")
    verid_col   = col_map.get("jira version id")

    by_key = {}
    for _, row in df.iterrows():
        rid = norm_release_id(row.get(release_col))
        if rid is None:
            continue
        project = str(row.get(project_col)).strip() if project_col and pd.notna(row.get(project_col)) else ""
        if not project:
            continue
        by_key[(rid, project.upper())] = {
            "version_id":   str(row.get(verid_col)).strip(),
            "version_name": str(row.get(name_col)).strip() if pd.notna(row.get(name_col)) else "",
        }
    return by_key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source_issue_id")
    ap.add_argument("--apply", action="store_true", help="Actually send the PUT (default is inspect-only).")
    args = ap.parse_args()
    sid = str(args.source_issue_id).strip()

    print("=" * 70)
    print(f"  DEBUG single issue — source_issue_id = {sid}")
    print("=" * 70)

    # ── issues_meta.json row ────────────────────────────────────────────
    with open(ISSUES_META_PATH, "r", encoding="utf-8") as f:
        meta = json.load(f)
    if isinstance(meta, dict):
        for key in ("issues", "data", "items"):
            if key in meta and isinstance(meta[key], list):
                meta = meta[key]
                break

    row = next((it for it in meta if str(it.get("source_issue_id")) == sid), None)
    print("\n-- issues_meta.json row --")
    print(json.dumps(row, indent=2) if row else "  NOT FOUND")
    if not row:
        return 1

    release_id_raw = row.get("source_release_id")
    release_id = norm_release_id(release_id_raw)
    print(f"\n  source_release_id (raw)  : {release_id_raw!r}")
    print(f"  source_release_id (norm) : {release_id!r}")

    # ── id_mapping.json entry ───────────────────────────────────────────
    with open(ID_MAPPING_PATH, "r", encoding="utf-8") as f:
        id_mapping = json.load(f)
    issues_map = id_mapping.get("issues", id_mapping)
    issue_info = issues_map.get(sid)
    print("\n-- id_mapping.json[\"issues\"][sid] --")
    print(json.dumps(issue_info, indent=2) if issue_info else "  NOT FOUND")
    if not issue_info or not issue_info.get("jira_key"):
        return 1

    jira_key = issue_info["jira_key"]
    project = project_from_jira_key(jira_key)
    print(f"\n  jira_key : {jira_key}")
    print(f"  project (derived from jira_key) : {project}")

    # ── STEP 1: strict (release_id, project) lookup in Excel ────────────
    by_key = load_versions_excel(VERSIONS_XLSX_PATH)
    excel_entry = by_key.get((release_id, project.upper())) if project else None
    print(f"\n-- STEP 1: versions_mapping.xlsx row for (release_id={release_id!r}, project={project!r}) --")
    print(json.dumps(excel_entry, indent=2) if excel_entry else "  NOT FOUND")
    if not excel_entry:
        print(f"\n  ✖ No Excel row for this exact (release_id, project) pair. Cannot resolve.")
        return 1

    excel_id = excel_entry["version_id"]
    version_name = excel_entry["version_name"]

    # ── live issue state ─────────────────────────────────────────────────
    print(f"\n-- GET {jira_key} (fixVersions + project) --")
    data, status = jget(f"api/3/issue/{jira_key}?fields=fixVersions,project")
    print(f"  HTTP {status}")
    print(json.dumps(data, indent=2))
    if status != 200:
        return 1

    fields = data.get("fields", {})
    current_versions = fields.get("fixVersions", []) or []
    current_ids = [v.get("id") for v in current_versions if v.get("id")]
    current_names = {v.get("id"): v.get("name") for v in current_versions}
    live_project = fields.get("project", {}).get("key")

    print(f"\n  Current fixVersions on issue : {[(v.get('id'), v.get('name')) for v in current_versions]}")
    print(f"  Issue's actual live project    : {live_project}")
    if live_project and project and live_project.upper() != project.upper():
        print(f"  ⚠⚠ Derived project '{project}' from jira_key does NOT match live project "
              f"'{live_project}' — investigate jira_key parsing.")

    # ── STEP 2: validate the Excel id actually exists live AND has the
    #    right name in this project ────────────────────────────────────
    print(f"\n-- STEP 2: GET api/3/project/{project}/versions (validate Excel id BY NAME) --")
    proj_versions, pv_status = jget(f"api/3/project/{project}/versions")
    version_id = None
    if pv_status == 200 and isinstance(proj_versions, list):
        id_to_name = {v.get("id"): (v.get("name") or "") for v in proj_versions}
        name_to_id = {(v.get("name") or "").strip().lower(): v.get("id") for v in proj_versions}
        print(f"  {len(proj_versions)} live versions exist in project {project}")

        live_name_of_excel_id = id_to_name.get(excel_id)
        expected_norm = version_name.strip().lower() if version_name else ""

        if live_name_of_excel_id is not None and live_name_of_excel_id.strip().lower() == expected_norm:
            version_id = excel_id
            print(f"  ✔ Excel id {excel_id} exists live AND is named '{live_name_of_excel_id}' "
                  f"— matches expected '{version_name}'. Using it directly.")
        elif live_name_of_excel_id is not None:
            print(f"  ⚠⚠ Excel id {excel_id} EXISTS live in {project}, but its real name is "
                  f"'{live_name_of_excel_id}', NOT '{version_name}' as the Excel claims. "
                  f"Existence alone would have wrongly accepted this id — it does not actually "
                  f"correspond to the version we want.")
        else:
            print(f"  ⚠⚠ Excel id {excel_id} for '{version_name}' does NOT exist live in "
                  f"project {project} at all.")

        if version_id is None:
            # ── STEP 3: fall back to live name match ────────────────────
            print(f"\n-- STEP 3: fallback — live name match for '{version_name}' in {project} --")
            fallback_id = name_to_id.get(expected_norm) if expected_norm else None
            if fallback_id:
                version_id = fallback_id
                print(f"  ✔ Found live version '{version_name}' in {project} with REAL id {fallback_id} "
                      f"(Excel said {excel_id} — Excel row is wrong).")
            else:
                print(f"  ✖ No live version named '{version_name}' exists in project {project} either.")
                print(f"  Available version names in {project} (first 20): "
                      f"{[v.get('name') for v in proj_versions[:20]]}")
    else:
        print(f"  Could not fetch project versions (HTTP {pv_status}): {proj_versions}")

    if not version_id:
        print("\n  ✖ Could not resolve a valid version id — would fail with no PUT attempted "
              "(exactly what fix_issue_versions.py now does).")
        return 1

    print(f"\n  RESOLVED version to use: id={version_id!r} name={version_name!r}")

    # ── build + show the payload (only remove wrong ones, keep correct) ──
    wrong_ids = [vid for vid in current_ids if vid != version_id]
    correct_already_present = version_id in current_ids
    update_ops = [{"remove": {"id": vid}} for vid in wrong_ids]
    if not correct_already_present:
        update_ops.append({"add": {"id": version_id}})
    payload = {"update": {"fixVersions": update_ops}}

    print(f"\n-- Payload we would PUT --")
    print(json.dumps(payload, indent=2))

    if current_ids == [version_id]:
        print(f"\n  = Already correct — current fixVersion is '{current_names.get(version_id)}' "
              f"(id {version_id}). No PUT needed.")
        return 0

    if correct_already_present:
        print(f"\n  Correct version already present alongside wrong one(s) "
              f"{[(vid, current_names.get(vid)) for vid in wrong_ids]} — those would be removed, "
              f"correct one left untouched.")

    if not args.apply:
        print("\n  (inspect-only run — pass --apply to actually send this PUT)")
        return 0

    print(f"\n-- PUT api/3/issue/{jira_key} --")
    put_body, put_status = jput(f"api/3/issue/{jira_key}", payload)
    print(f"  HTTP {put_status}")
    print(json.dumps(put_body, indent=2))

    if put_status == 204:
        print("\n  ✔ Fixed.")
        return 0
    else:
        print("\n  ✖ Still failing — see Jira error body above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())