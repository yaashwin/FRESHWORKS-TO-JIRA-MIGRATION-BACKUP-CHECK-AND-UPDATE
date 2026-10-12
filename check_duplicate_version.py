"""
check_duplicate_version.py

READ-ONLY audit script. Does NOT change anything in Jira, id_mapping.json,
or versions_mapping.xlsx.

FLOW (as requested):
  1. Drive off id_mapping["issues"]  ->  {source_id: {jira_key, jira_id, ...}}
     This is the definitive list of issues that actually exist in Jira.

  2. For each source_id, look it up in issues_meta.json (matched on
     "source_issue_id") to get its source_release_id.

  3. Build the composite key  source_release_id::project  (project = the
     prefix of jira_key, e.g. EX / PLAT / CEMIG — NOT source_project_key,
     since that's the source-system project, not the Jira one) and look
     THAT key up directly in versions_mapping.xlsx. The Excel is treated
     as the single ground truth for "what version this issue SHOULD have"
     — NOT id_mapping["versions"], because id_mapping is exactly what was
     corrupted in the original bug, so checking against itself would be
     circular and could hide the very mismatches we're looking for.

  4. As a free diagnostic (no extra API calls — both are already in
     memory), it also checks whether id_mapping["versions"] AGREES or
     DISAGREES with the Excel for that same key. This tells you which
     bad id_mapping entries are still lying around even if the Jira
     issue itself happens to be correct (or vice versa).

  5. Fetches the issue's ACTUAL current fixVersions live from Jira and
     classifies:
        OK                   actual == exactly the one correct version
        MISSING               no fixVersion assigned at all
        WRONG_ONLY             has version(s), none of them correct
        CORRECT_PLUS_EXTRA      has the correct version AND extra wrong one(s)
        MULTIPLE_WRONG          2+ versions attached, none correct
        UNRESOLVED              Excel has no entry for this key at all
        NO_META                 source_id not found in issues_meta.json
        GET_FAILED              live Jira GET failed for this issue

  Any issue with 2+ fixVersions is flagged "Has Multiple Versions", and
  3+ gets its own explicit "More Than 2 Versions" flag.

Output:
    ./mapping/check_duplicate_version.xlsx
    ./mapping/check_duplicate_version.json

Usage:
    python check_duplicate_version.py
    python check_duplicate_version.py --projects CEMIG,PLAT
    python check_duplicate_version.py --projects CEMIG,PLAT,EX
    python check_duplicate_version.py --only-mismatches
"""

import os
import json
import argparse
import concurrent.futures
from threading import Lock
from collections import Counter

import pandas as pd

import bulk_load_to_jira as core  # reuse .env, jira_credentials, jira_get, id_mapping, etc.


def _project_from_jira_key(jira_key):
    return jira_key.split("-")[0] if jira_key and "-" in jira_key else None


def _version_key(source_release_id, project_key):
    return f"{source_release_id}::{project_key}"


def _clean_val(val):
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none") else s


def _clean_release_id(val):
    """'21573.0' -> '21573'. issues_meta stores release ids as floats-as-strings."""
    return _clean_val(val).split(".")[0]


# =============================================================================
# LOAD versions_mapping.xlsx — THIS IS THE GROUND TRUTH
# =============================================================================

def load_versions_excel():
    """
    {composite_key: {jira_version_id, jira_version_name}} from
    versions_mapping.xlsx (Version Name, Source Release ID, Project,
    Jira Version ID), keyed source_release_id::project.

    This is the authoritative "correct" answer for every check in this
    script — never id_mapping.
    """
    xlsx_path = os.path.join(core.MAPPING_DIR, "versions_mapping.xlsx")
    excel_map = {}

    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(
            f"versions_mapping.xlsx not found at {xlsx_path} — this script requires it "
            f"as the ground-truth source, it cannot run without it."
        )

    df = pd.read_excel(xlsx_path, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    required_cols = {"Source Release ID", "Project", "Jira Version ID"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        raise ValueError(f"versions_mapping.xlsx is missing required column(s): {missing_cols}")

    for _, row in df.iterrows():
        source_release_id = _clean_val(row.get("Source Release ID"))
        project = _clean_val(row.get("Project")).upper()
        jira_version_id = _clean_val(row.get("Jira Version ID"))
        jira_version_name = _clean_val(row.get("Version Name"))

        if not source_release_id or not project or not jira_version_id:
            continue

        key = _version_key(source_release_id, project)
        excel_map[key] = {
            "jira_version_id": jira_version_id,
            "jira_version_name": jira_version_name,
        }

    print(f"  ✔ Loaded {len(excel_map)} version(s) from ground-truth Excel ({xlsx_path})")
    return excel_map


# =============================================================================
# LOAD issues_meta.json AS A LOOKUP KEYED BY source_issue_id
# =============================================================================

def load_meta_lookup():
    meta_list = core.load_issues_meta()
    lookup = {}
    for rec in meta_list:
        sid = str(rec.get("source_issue_id"))
        if sid:
            lookup[sid] = rec
    print(f"  ✔ Loaded {len(lookup)} record(s) from issues_meta.json")
    return lookup


# =============================================================================
# BUILD THE CHECK LIST — driven by id_mapping["issues"]
# =============================================================================

def build_check_list(target_projects, meta_lookup, excel_map):
    """
    Returns a list of dicts, one per issue in id_mapping["issues"] whose
    Jira project is in target_projects:
        {source_id, jira_key, jira_id, title, project,
         source_release_id, meta_found,
         expected_id, expected_name,                # from Excel (ground truth)
         id_mapping_id, id_mapping_name,             # from id_mapping (diagnostic only)
         mapping_agreement}                          # MATCH / MISMATCH / EXCEL_ONLY / ID_MAPPING_ONLY / NONE
    """
    check_list = []
    skipped_wrong_project = 0
    no_meta_count = 0

    for source_id, issue_info in core.id_mapping["issues"].items():
        jira_key = issue_info.get("jira_key")
        jira_id = issue_info.get("jira_id", "")
        title = issue_info.get("title", "")
        project = _project_from_jira_key(jira_key)

        if project not in target_projects:
            skipped_wrong_project += 1
            continue

        meta_rec = meta_lookup.get(str(source_id))
        if not meta_rec:
            no_meta_count += 1
            check_list.append({
                "source_id": source_id, "jira_key": jira_key, "jira_id": jira_id, "title": title,
                "project": project, "source_release_id": "", "meta_found": False,
                "expected_id": None, "expected_name": None,
                "id_mapping_id": None, "id_mapping_name": None,
                "mapping_agreement": "NONE",
                "forced_status": "NO_META",
            })
            continue

        source_release_id = _clean_release_id(meta_rec.get("source_release_id"))
        if not source_release_id:
            check_list.append({
                "source_id": source_id, "jira_key": jira_key, "jira_id": jira_id, "title": title,
                "project": project, "source_release_id": "", "meta_found": True,
                "expected_id": None, "expected_name": None,
                "id_mapping_id": None, "id_mapping_name": None,
                "mapping_agreement": "NONE",
                "forced_status": "NO_RELEASE_ID",
            })
            continue

        key = _version_key(source_release_id, project)

        excel_entry = excel_map.get(key)
        expected_id = excel_entry.get("jira_version_id") if excel_entry else None
        expected_name = excel_entry.get("jira_version_name") if excel_entry else None

        id_mapping_entry = core.id_mapping.get("versions", {}).get(key)
        id_mapping_id = str(id_mapping_entry.get("jira_version_id")) if id_mapping_entry and id_mapping_entry.get("jira_version_id") else None
        id_mapping_name = id_mapping_entry.get("jira_version_name") if id_mapping_entry else None

        if expected_id and id_mapping_id:
            mapping_agreement = "MATCH" if str(expected_id) == str(id_mapping_id) else "MISMATCH"
        elif expected_id and not id_mapping_id:
            mapping_agreement = "EXCEL_ONLY"
        elif id_mapping_id and not expected_id:
            mapping_agreement = "ID_MAPPING_ONLY"
        else:
            mapping_agreement = "NONE"

        check_list.append({
            "source_id": source_id, "jira_key": jira_key, "jira_id": jira_id, "title": title,
            "project": project, "source_release_id": source_release_id, "meta_found": True,
            "expected_id": str(expected_id) if expected_id else None,
            "expected_name": expected_name,
            "id_mapping_id": id_mapping_id, "id_mapping_name": id_mapping_name,
            "mapping_agreement": mapping_agreement,
            "forced_status": None,
        })

    print(f"  {len(check_list)} issue(s) in target project(s)")
    if no_meta_count:
        print(f"  ⚠ {no_meta_count} of those have NO matching issues_meta.json record (status=NO_META)")
    if skipped_wrong_project:
        print(f"  ({skipped_wrong_project} issue(s) in id_mapping skipped — different project)")

    return check_list


# =============================================================================
# FETCH ACTUAL fixVersions FROM JIRA (parallel, read-only)
# =============================================================================

def fetch_actual_versions(jira_key):
    data, status = core.jira_get(f"api/3/issue/{jira_key}?fields=fixVersions")
    if status != 200 or not data:
        return None
    versions = data.get("fields", {}).get("fixVersions", []) or []
    return [{"id": str(v.get("id")), "name": v.get("name", "")} for v in versions if v.get("id")]


# =============================================================================
# CLASSIFY ONE ISSUE
# =============================================================================

def classify(row, actual_versions):
    out = dict(row)

    if row.get("forced_status"):
        out.update({
            "actual_ids": "", "actual_names": "", "wrong_ids": "", "wrong_names": "",
            "version_count": 0, "status": row["forced_status"],
            "multiple_versions": False, "more_than_two": False,
        })
        return out

    if actual_versions is None:
        out.update({
            "actual_ids": "", "actual_names": "", "wrong_ids": "", "wrong_names": "",
            "version_count": 0, "status": "GET_FAILED",
            "multiple_versions": False, "more_than_two": False,
        })
        return out

    expected_id = row["expected_id"]
    resolved = expected_id is not None

    actual_ids = [v["id"] for v in actual_versions]
    actual_names = [v["name"] for v in actual_versions]
    version_count = len(actual_versions)

    wrong = [v for v in actual_versions if v["id"] != expected_id] if resolved else list(actual_versions)
    has_correct = resolved and expected_id in actual_ids

    if not resolved:
        status = "UNRESOLVED"
    elif version_count == 0:
        status = "MISSING"
    elif has_correct and version_count == 1:
        status = "OK"
    elif has_correct and version_count > 1:
        status = "CORRECT_PLUS_EXTRA"
    elif not has_correct and version_count == 1:
        status = "WRONG_ONLY"
    else:
        status = "MULTIPLE_WRONG"

    out.update({
        "actual_ids": ", ".join(actual_ids),
        "actual_names": ", ".join(actual_names),
        "wrong_ids": ", ".join(v["id"] for v in wrong),
        "wrong_names": ", ".join(v["name"] for v in wrong),
        "version_count": version_count,
        "status": status,
        "multiple_versions": version_count > 1,
        "more_than_two": version_count > 2,
    })
    return out


# =============================================================================
# MAIN CHECK
# =============================================================================

def run_check(target_projects, only_mismatches=False):
    print("\n" + "━" * 70)
    print(f"  Checking fixVersions — projects: {', '.join(sorted(target_projects))}")
    print("━" * 70)

    excel_map = load_versions_excel()
    meta_lookup = load_meta_lookup()
    check_list = build_check_list(target_projects, meta_lookup, excel_map)
    print()

    results = []
    lock = Lock()

    def _check_one(row):
        if row.get("forced_status"):
            classified = classify(row, None)
        else:
            actual = fetch_actual_versions(row["jira_key"])
            classified = classify(row, actual)
        with lock:
            results.append(classified)
            done_local = len(results)
        if done_local % 500 == 0:
            core.thread_safe_print(f"  Checked {done_local}/{len(check_list)}...")
        return classified

    if check_list:
        workers = min(getattr(core, "VERSION_ASSIGN_MAX_WORKERS", 20), len(check_list))
        print(f"  Workers: {workers}\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_check_one, row) for row in check_list]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    core.thread_safe_print(f"   ✖ Check worker exception: {e}")

    status_counts = Counter(r["status"] for r in results)
    agreement_counts = Counter(r["mapping_agreement"] for r in results)
    multi_count = sum(1 for r in results if r["multiple_versions"])
    more_than_two_count = sum(1 for r in results if r["more_than_two"])

    print("\n  Jira-state summary:")
    for status in ["OK", "MISSING", "WRONG_ONLY", "CORRECT_PLUS_EXTRA", "MULTIPLE_WRONG",
                    "UNRESOLVED", "NO_META", "NO_RELEASE_ID", "GET_FAILED"]:
        if status_counts.get(status):
            print(f"    {status:20s}: {status_counts[status]}")
    print(f"    {'Has 2+ versions':20s}: {multi_count}")
    print(f"    {'Has MORE than 2':20s}: {more_than_two_count}")

    print("\n  id_mapping vs Excel agreement (diagnostic, independent of live Jira state):")
    for label in ["MATCH", "MISMATCH", "EXCEL_ONLY", "ID_MAPPING_ONLY", "NONE"]:
        if agreement_counts.get(label):
            print(f"    {label:20s}: {agreement_counts[label]}")

    if only_mismatches:
        results = [r for r in results if r["status"] != "OK"]

    return results


def save_report(results):
    json_path = os.path.join(core.MAPPING_DIR, "check_duplicate_version.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\n   ✔ JSON report saved -> {json_path}")

    rows = []
    for r in results:
        rows.append({
            "Source ID": r["source_id"],
            "Jira Key": r["jira_key"],
            "Jira ID": r.get("jira_id", ""),
            "Title": r.get("title", ""),
            "Project": r["project"],
            "Source Release ID": r["source_release_id"],
            "Status": r["status"],
            "Expected Version ID (Excel)": r["expected_id"] or "",
            "Expected Version Name (Excel)": r["expected_name"] or "",
            "Actual Version IDs (Jira)": r["actual_ids"],
            "Actual Version Names (Jira)": r["actual_names"],
            "Wrong Version IDs": r["wrong_ids"],
            "Wrong Version Names": r["wrong_names"],
            "Version Count": r["version_count"],
            "Has Multiple Versions": "Yes" if r["multiple_versions"] else "No",
            "More Than 2 Versions": "Yes" if r["more_than_two"] else "No",
            "id_mapping Version ID": r.get("id_mapping_id") or "",
            "Excel vs id_mapping": r.get("mapping_agreement", ""),
        })

    xlsx_path = os.path.join(core.MAPPING_DIR, "check_duplicate_version.xlsx")
    if rows:
        df = pd.DataFrame(rows)
        status_order = {"MULTIPLE_WRONG": 0, "CORRECT_PLUS_EXTRA": 1, "WRONG_ONLY": 2,
                         "MISSING": 3, "NO_META": 4, "NO_RELEASE_ID": 5, "UNRESOLVED": 6,
                         "GET_FAILED": 7, "OK": 8}
        df["_sort"] = df["Status"].map(status_order).fillna(9)
        df = df.sort_values(["_sort", "Version Count"], ascending=[True, False]).drop(columns="_sort")
        df.to_excel(xlsx_path, index=False)
        print(f"   ✔ Excel report saved -> {xlsx_path}")
    else:
        print("   (no rows to write)")


def main():
    parser = argparse.ArgumentParser(
        description="Read-only audit: compare live Jira fixVersions against versions_mapping.xlsx ground truth, flag wrong/duplicate versions"
    )
    parser.add_argument("--projects", default="CEMIG,PLAT,EX", help="Comma-separated Jira project keys to check (default: CEMIG,PLAT)")
    parser.add_argument("--only-mismatches", action="store_true", help="Only write rows that are NOT status=OK to the Excel")
    args = parser.parse_args()

    target_projects = {p.strip().upper() for p in args.projects.split(",") if p.strip()}

    print("=" * 70)
    print("  CHECK DUPLICATE / WRONG VERSIONS (read-only, no writes anywhere)")
    print("=" * 70)
    print(f"  Target projects: {sorted(target_projects)}")
    print("=" * 70)

    core.start_rate_monitor()
    if not core.validate_all_accounts():
        core.stop_rate_monitor()
        return 1

    try:
        results = run_check(target_projects, only_mismatches=args.only_mismatches)
        save_report(results)
    finally:
        core.stop_rate_monitor()

    print("\n" + "=" * 70)
    print("  DONE")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    exit(main())