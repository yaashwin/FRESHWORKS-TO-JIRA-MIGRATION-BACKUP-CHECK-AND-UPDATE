"""
audit_wrong_versions.py

READ-ONLY. Goes through every issue in issues_meta.json that has a
non-empty source_release_id, resolves the correct version the same way
fix_issue_versions.py does (strict (release_id, project) lookup in
versions_mapping.xlsx, validated live against Jira, with a live name-match
fallback if the Excel id is stale), and compares it to what's ACTUALLY
currently set on the issue (via GET).

It makes NO writes to Jira. It only writes two local files for you to
review:

    wrong_versions.json      — issues whose current fixVersions do NOT
                                match the resolved-correct version. Each
                                entry has exactly what a fix would do:
                                which id(s) to remove, which id (if any)
                                to add. This is what you review, and this
                                is what apply_wrong_versions_fix.py reads.

    unresolved_versions.json — issues where we couldn't resolve a correct
                                version at all (no Excel row, or Excel id
                                is stale AND no live name match either).
                                These need manual attention; the apply
                                script does nothing with these.

Already-correct issues are not written anywhere — they're simply counted.

Usage:
    python audit_wrong_versions.py                # full audit, 10 workers
    python audit_wrong_versions.py --limit 500      # audit first 500 matching issues
    python audit_wrong_versions.py --workers 15
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

try:
    import jira_credentials
    _HAVE_CREDENTIALS_MODULE = True
except Exception:
    _HAVE_CREDENTIALS_MODULE = False

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

# =============================================================================
# CONFIG
# =============================================================================

BASE_FS_DATA = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data"

ISSUES_META_PATH   = os.path.join(BASE_FS_DATA, "payload", "issues_meta.json")
ID_MAPPING_PATH     = os.path.join(BASE_FS_DATA, "mapping", "id_mapping.json")
VERSIONS_XLSX_PATH = os.path.join(BASE_FS_DATA, "mapping", "versions_mapping.xlsx")

JIRA_URL       = os.getenv("JIRA_URL")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

MAX_WORKERS    = int(os.getenv("VERSION_AUDIT_MAX_WORKERS", "10"))
SINGLE_TIMEOUT = 60
RETRY_DELAY    = 5
MAX_RETRIES    = 3
RATE_LIMIT_DELAY = 0.3

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WRONG_PATH       = os.path.join(_SCRIPT_DIR, "wrong_versions.json")
UNRESOLVED_PATH  = os.path.join(_SCRIPT_DIR, "unresolved_versions.json")
# Same cache file fix_issue_versions.py uses/reads — the audit also
# consults it so already-confirmed-correct issues aren't re-GET'd.
KNOWN_CORRECT_PATH = os.path.join(_SCRIPT_DIR, "known_correct_versions.json")

print_lock  = Lock()
counts_lock = Lock()
counts = {
    "already_correct_cached": 0,
    "already_correct_live":   0,
    "wrong":                  0,
    "unresolved":              0,
    "skipped_no_version":      0,
}

wrong_items = []
wrong_lock = Lock()
unresolved_items = []
unresolved_lock = Lock()

known_correct = {}

# Heartbeat — printed every PROGRESS_EVERY issues REGARDLESS of outcome, so
# a long silent stretch (most issues are "already correct" and print
# nothing on their own) doesn't look like the script has hung.
PROGRESS_EVERY = 500
_progress_count = 0
_progress_lock = Lock()
_progress_total = 0
_progress_start = None


def bump_progress():
    global _progress_count
    with _progress_lock:
        _progress_count += 1
        n = _progress_count
    if n % PROGRESS_EVERY == 0 or n == _progress_total:
        elapsed = time.time() - _progress_start
        rate = n / elapsed if elapsed > 0 else 0
        remaining = (_progress_total - n) / rate if rate > 0 else 0
        log(f"  ... {n}/{_progress_total} processed ({elapsed:.0f}s elapsed, "
            f"~{remaining:.0f}s remaining at current rate)")


def log(msg):
    with print_lock:
        print(msg)


def bump(key):
    with counts_lock:
        counts[key] += 1


def load_known_correct():
    global known_correct
    if os.path.exists(KNOWN_CORRECT_PATH):
        try:
            with open(KNOWN_CORRECT_PATH, "r", encoding="utf-8") as f:
                known_correct = json.load(f)
        except (json.JSONDecodeError, OSError):
            known_correct = {}


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


def jira_request(method, endpoint, retries=MAX_RETRIES):
    url = f"{JIRA_URL}/rest/{endpoint.lstrip('/')}"
    auth = _get_auth()

    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=JIRA_HEADERS, auth=auth, timeout=SINGLE_TIMEOUT)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", RETRY_DELAY))
                log(f"  [RATE LIMIT] 429 on GET {endpoint} — sleeping {retry_after}s")
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


# =============================================================================
# LOAD INPUTS  (identical to fix_issue_versions.py)
# =============================================================================

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


def load_issues_meta(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("issues", "data", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
        raise ValueError(f"Could not find a list of issues inside {path}")
    return data


def load_id_mapping(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "issues" in data:
        return data["issues"]
    return data


def load_versions_excel(path):
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
    for _, row in df.iterrows():
        rid = norm_release_id(row.get(release_col))
        if rid is None:
            continue
        project = str(row.get(project_col)).strip() if pd.notna(row.get(project_col)) else ""
        if not project:
            continue
        by_key[(rid, project.upper())] = {
            "version_id":   str(row.get(verid_col)).strip(),
            "version_name": str(row.get(name_col)).strip() if pd.notna(row.get(name_col)) else "",
        }
    return by_key


def project_from_jira_key(jira_key):
    if not jira_key:
        return None
    m = re.match(r"^(.*)-\d+$", jira_key.strip())
    return m.group(1) if m else None


def resolve_version(by_key, release_id, jira_key):
    project = project_from_jira_key(jira_key)
    if not project:
        return None, None, None
    entry = by_key.get((release_id, project.upper()))
    if not entry:
        return None, None, None
    return entry["version_id"], entry["version_name"], project


# =============================================================================
# LIVE VALIDATION (identical logic to fix_issue_versions.py)
# =============================================================================

_project_versions_cache = {}
_project_versions_lock = Lock()


def get_live_project_versions(project_key):
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
    excel_id, version_name, project = resolve_version(by_key, release_id, jira_key)
    if not excel_id:
        p = project_from_jira_key(jira_key)
        return None, None, f"no versions_mapping.xlsx row for (release_id={release_id}, project={p})"

    id_to_name, name_to_id = get_live_project_versions(project)
    expected_name_norm = version_name.strip().lower() if version_name else ""

    live_name_of_excel_id = id_to_name.get(excel_id)
    if live_name_of_excel_id is not None and live_name_of_excel_id.strip().lower() == expected_name_norm:
        return excel_id, version_name, None

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
# PER-ISSUE AUDIT
# =============================================================================

def audit_issue(item, by_key, id_mapping_issues):
    source_issue_id = str(item.get("source_issue_id", "")).strip()
    release_id = norm_release_id(item.get("source_release_id"))

    try:
        if not release_id:
            bump("skipped_no_version")
            return

        issue_info = id_mapping_issues.get(source_issue_id)
        if not issue_info or not issue_info.get("jira_key"):
            with unresolved_lock:
                unresolved_items.append({
                    "source_issue_id": source_issue_id,
                    "jira_key": None,
                    "reason": "no jira_key in id_mapping for this source_issue_id",
                })
            bump("unresolved")
            return

        jira_key = issue_info["jira_key"]

        version_id, version_name, resolve_error = resolve_and_validate_version(by_key, release_id, jira_key)
        if not version_id:
            with unresolved_lock:
                unresolved_items.append({
                    "source_issue_id": source_issue_id,
                    "jira_key": jira_key,
                    "reason": resolve_error,
                })
            bump("unresolved")
            log(f"  ? {jira_key} (src {source_issue_id}): {resolve_error}")
            return

        # "never touch it again" cache check — still read-only, just skips the GET.
        if known_correct.get(jira_key) == version_id:
            bump("already_correct_cached")
            return

        data, status = jira_get(f"api/3/issue/{jira_key}?fields=fixVersions")
        if status != 200 or data is None:
            with unresolved_lock:
                unresolved_items.append({
                    "source_issue_id": source_issue_id,
                    "jira_key": jira_key,
                    "reason": f"GET issue failed (HTTP {status}): {data}",
                })
            bump("unresolved")
            log(f"  ✖ {jira_key}: could not fetch current fixVersions (HTTP {status})")
            return

        current_versions = data.get("fields", {}).get("fixVersions", []) or []
        current_ids = [v.get("id") for v in current_versions if v.get("id")]

        if current_ids == [version_id]:
            bump("already_correct_live")
            return

        wrong_ids = [vid for vid in current_ids if vid != version_id]
        correct_already_present = version_id in current_ids

        entry = {
            "source_issue_id": source_issue_id,
            "jira_key": jira_key,
            "correct_version_id": version_id,
            "correct_version_name": version_name,
            "current_fix_versions": [
                {"id": v.get("id"), "name": v.get("name")} for v in current_versions
            ],
            "remove_ids": wrong_ids,
            "add_id": None if correct_already_present else version_id,
            "action": "clean" if correct_already_present else "reassign",
        }
        with wrong_lock:
            wrong_items.append(entry)
        bump("wrong")

        if correct_already_present:
            log(f"  ~ {jira_key} (src {source_issue_id}): CLEAN needed — keep "
                f"'{version_name}' ({version_id}), remove {wrong_ids}")
        else:
            log(f"  ~ {jira_key} (src {source_issue_id}): REASSIGN needed — remove "
                f"{wrong_ids or 'none'} → add '{version_name}' ({version_id})")
    finally:
        bump_progress()


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Audit-only: find issues with wrong/missing versions.")
    parser.add_argument("--limit", type=int, default=None, help="Only audit the first N matching issues.")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="Thread pool size (default 10).")
    args = parser.parse_args()

    if not JIRA_URL:
        print("✖ JIRA_URL not set (check your .env). Aborting.")
        return 1

    print("=" * 70)
    print("  AUDIT WRONG VERSIONS (read-only — no writes to Jira)")
    print("=" * 70)
    print(f"  issues_meta.json     : {ISSUES_META_PATH}")
    print(f"  id_mapping.json       : {ID_MAPPING_PATH}")
    print(f"  versions_mapping.xlsx : {VERSIONS_XLSX_PATH}")
    print(f"  workers               : {args.workers}")
    print()

    issues = load_issues_meta(ISSUES_META_PATH)
    id_mapping_issues = load_id_mapping(ID_MAPPING_PATH)
    by_key = load_versions_excel(VERSIONS_XLSX_PATH)
    load_known_correct()
    print(f"  known_correct_versions.json : {len(known_correct)} issue(s) pre-confirmed\n")

    work_items = [it for it in issues if norm_release_id(it.get("source_release_id"))]
    if args.limit:
        work_items = work_items[:args.limit]

    print(f"  {len(issues)} total issues in issues_meta.json")
    print(f"  {len(work_items)} issue(s) have a source_release_id (version) to audit\n")

    if not work_items:
        print("Nothing to do.")
        return 0

    global _progress_total, _progress_start
    _progress_total = len(work_items)
    _progress_start = time.time()

    start = time.time()
    workers = min(args.workers, max(len(work_items), 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(audit_issue, item, by_key, id_mapping_issues) for item in work_items]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as e:
                log(f"   ✖ Worker exception: {e}")

    elapsed = time.time() - start

    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  Already correct (checked live)        : {counts['already_correct_live']}")
    print(f"  Already correct (cached, untouched)   : {counts['already_correct_cached']}")
    print(f"  WRONG — needs fix                      : {counts['wrong']}")
    print(f"  Unresolved (needs manual attention)   : {counts['unresolved']}")
    print(f"  Skipped (no version value)            : {counts['skipped_no_version']}")
    print(f"  Time elapsed                          : {elapsed:.2f}s")
    print("=" * 70)

    with open(WRONG_PATH, "w", encoding="utf-8") as f:
        json.dump(wrong_items, f, indent=2)
    print(f"\n  ✔ {len(wrong_items)} issue(s) needing a fix written → {WRONG_PATH}")
    print(f"    Review/edit this file (remove any entries you don't want touched), then run:")
    print(f"    python apply_wrong_versions_fix.py")

    with open(UNRESOLVED_PATH, "w", encoding="utf-8") as f:
        json.dump(unresolved_items, f, indent=2)
    if unresolved_items:
        print(f"\n  ⚠ {len(unresolved_items)} issue(s) could not be resolved at all → {UNRESOLVED_PATH}")
        print(f"    These need manual attention — the apply script does nothing with these.")

    return 0


if __name__ == "__main__":
    sys.exit(main())