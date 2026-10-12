# """
# assign_versions_remaining_projects.py

# Assigns fixVersions ONLY to issues belonging to specific projects
# (default: CEMIG, PLAT) — for when other projects (e.g. EX) are already
# fully assigned and you don't want to re-touch ~7,500 already-correct
# issues just to pick up the couple of projects still missing.

# Uses the SAME composite-key lookup as fix_multiproject_versions.py
# (source_release_id::project), so each issue only ever gets a version
# that belongs to its OWN project — the actual fix for the earlier
# HTTP 400s (an issue in one project being handed a version ID that
# belongs to a different project).

# VERSION RESOLUTION (in order):
#   1. id_mapping["versions"][source_release_id::project]  (already known-good)
#   2. versions_mapping.xlsx, same composite key             (healed into id_mapping)
#   3. If STILL no valid version for this exact project — i.e. neither of the
#      above has an entry scoped to this project — the version is CREATED in
#      Jira under the target project (reusing the original version's name,
#      looked up from versions.json), then that new id is used and healed
#      into id_mapping.

# RETRY ON "Specify a valid value for fixVersions":
#   This error means the version id we sent doesn't belong to the issue's
#   project in Jira (e.g. an id that's actually an EX version, reused by
#   mistake for a PLAT/CEMIG issue via a bad id_mapping/Excel entry). Any
#   issue that fails with this specific error is automatically retried:
#   we look up (or create) the correct version for that issue's OWN project
#   by name, heal id_mapping with the corrected id, and PUT again once.

# Usage:
#     python assign_versions_remaining_projects.py
#     python assign_versions_remaining_projects.py --projects CEMIG,PLAT
#     python assign_versions_remaining_projects.py --dry-run
# """

# import os
# import json
# import argparse
# import concurrent.futures
# from threading import Lock

# import pandas as pd

# import bulk_load_to_jira as core  # reuse .env, jira_credentials, jira_put, id_mapping, etc.


# def _project_from_jira_key(jira_key):
#     return jira_key.split("-")[0] if jira_key and "-" in jira_key else None


# def _version_key(source_release_id, project_key):
#     return f"{source_release_id}::{project_key}"


# def _clean_val(val):
#     if val is None:
#         return ""
#     s = str(val).strip()
#     return "" if s.lower() in ("nan", "none") else s


# def _is_invalid_fixversion_error(error_msg):
#     msg = str(error_msg).lower()
#     return "fixversions" in msg and "valid value" in msg


# # =============================================================================
# # VERSION NAME LOOKUP (source_release_id -> name), independent of project
# # =============================================================================

# def load_source_version_names():
#     """
#     source_release_id -> version name, taken from versions.json's own
#     payload (the original create-version payload prepare_payload.py wrote).
#     This is project-agnostic — the same release name is what gets created
#     in whichever project actually needs it.
#     """
#     versions = core.load_json("versions.json")
#     names = {}
#     for item in versions:
#         source_id = str(item.get("source_release_id", ""))
#         payload = item.get("payload", {}) or {}
#         name = payload.get("name")
#         if source_id and name:
#             names[source_id] = name
#     return names


# def load_versions_excel_fallback():
#     """
#     Reads versions_mapping.xlsx (Version Name, Source Release ID, Project,
#     Jira Version ID) and returns {composite_key: {jira_version_id,
#     jira_version_name}}, keyed the same way as id_mapping["versions"]
#     (source_release_id::project).

#     Returns an empty dict (never raises) if the file doesn't exist or
#     can't be read — this is a fallback source, not a requirement.
#     """
#     xlsx_path = os.path.join(core.MAPPING_DIR, "versions_mapping.xlsx")
#     fallback = {}

#     if not os.path.exists(xlsx_path):
#         print(f"  (no Excel fallback — {xlsx_path} not found)")
#         return fallback

#     try:
#         df = pd.read_excel(xlsx_path, dtype=str)
#         df.columns = [str(c).strip() for c in df.columns]

#         required_cols = {"Source Release ID", "Project", "Jira Version ID"}
#         missing_cols = required_cols - set(df.columns)
#         if missing_cols:
#             print(f"  ⚠ Excel fallback skipped — {xlsx_path} is missing column(s): {missing_cols}")
#             return fallback

#         for _, row in df.iterrows():
#             source_release_id = _clean_val(row.get("Source Release ID"))
#             project = _clean_val(row.get("Project")).upper()
#             jira_version_id = _clean_val(row.get("Jira Version ID"))
#             jira_version_name = _clean_val(row.get("Version Name"))

#             if not source_release_id or not project or not jira_version_id:
#                 continue

#             key = _version_key(source_release_id, project)
#             fallback[key] = {
#                 "jira_version_id": jira_version_id,
#                 "jira_version_name": jira_version_name,
#             }

#         print(f"  ✔ Loaded {len(fallback)} version(s) from Excel fallback ({xlsx_path})")
#     except Exception as e:
#         print(f"  ⚠ Excel fallback skipped — failed to read {xlsx_path}: {e}")

#     return fallback


# # =============================================================================
# # CREATE-OR-FIND VERSION IN A SPECIFIC PROJECT
# # =============================================================================
# # Thread-safe cache of project_key -> {version_name_lower: version_data},
# # same pattern as core.load_versions()'s internal cache, so we only ever
# # fetch a given project's version list once per run.
# _project_versions_cache = {}
# _project_versions_lock = Lock()


# def _find_existing_version_in_project(project_key, version_name):
#     with _project_versions_lock:
#         cached = _project_versions_cache.get(project_key)
#     if cached is None:
#         data, status = core.jira_get(f"api/3/project/{project_key}/versions")
#         lookup = {}
#         if status == 200 and isinstance(data, list):
#             for v in data:
#                 lookup[v.get("name", "").strip().lower()] = v
#         with _project_versions_lock:
#             _project_versions_cache[project_key] = lookup
#         core.thread_safe_print(f"     ↳ Fetched {len(lookup)} existing versions from project '{project_key}'")
#         cached = lookup
#     return cached.get(version_name.strip().lower())


# def get_or_create_version_for_project(project_key, version_name, source_release_id):
#     """
#     Returns (jira_version_id, jira_version_name, was_created) for version_name
#     scoped to project_key — creating it in Jira ONLY if it doesn't already
#     exist there (checked live against Jira's real version list first, so an
#     already-present version is always reused, never duplicated).
#     Updates the project-versions cache so repeat lookups this run are free.
#     Returns (None, None, False) if creation fails outright.
#     """
#     existing = _find_existing_version_in_project(project_key, version_name)
#     if existing:
#         return existing.get("id"), existing.get("name"), False

#     payload = {"name": version_name, "project": project_key}
#     data, status = core.jira_post("api/3/version", payload)

#     if status == 201:
#         with _project_versions_lock:
#             _project_versions_cache.setdefault(project_key, {})[version_name.strip().lower()] = data
#         core.thread_safe_print(f"     ✔ Created version '{version_name}' in project '{project_key}' (id {data.get('id')})")
#         return data.get("id"), data.get("name"), True

#     error_msg = str(data.get("errors", data.get("errorMessages", data)) if data else f"HTTP {status}")

#     # Race condition / already exists — re-fetch and use it (not a creation).
#     if "already exists" in error_msg.lower():
#         with _project_versions_lock:
#             _project_versions_cache.pop(project_key, None)  # force refetch
#         existing = _find_existing_version_in_project(project_key, version_name)
#         if existing:
#             return existing.get("id"), existing.get("name"), False

#     core.thread_safe_print(
#         f"     ✖ Failed to create version '{version_name}' in project '{project_key}': {error_msg[:150]}"
#     )
#     return None, None, False


# def _heal_id_mapping(key, jira_version_id, jira_version_name, source_release_id, note):
#     with core.mapping_lock:
#         core.id_mapping["versions"][key] = {
#             "jira_version_id": jira_version_id,
#             "jira_version_name": jira_version_name,
#             "source_release_id": source_release_id,
#             "resolved_via": note,
#         }


# # =============================================================================
# # BUILD WORK ITEMS
# # =============================================================================

# def _resolve_version_name(key, source_release_id, source_version_names, excel_fallback):
#     """
#     Resolve a version NAME (never trust a stored ID directly) for this
#     composite key, checking in order:
#       1. id_mapping["versions"][key]["jira_version_name"]  (if present)
#       2. versions_mapping.xlsx row for this exact key (name column)
#       3. versions.json's own payload for this source_release_id
#          (project-agnostic — same release name regardless of project)
#       4. any Excel row for this source_release_id, any project
#          (last resort, just for the name)
#     Returns "" if no name can be found anywhere.
#     """
#     mapped = core.id_mapping["versions"].get(key)
#     if mapped and mapped.get("jira_version_name"):
#         return mapped["jira_version_name"]

#     excel_entry = excel_fallback.get(key)
#     if excel_entry and excel_entry.get("jira_version_name"):
#         return excel_entry["jira_version_name"]

#     name = source_version_names.get(source_release_id, "")
#     if name:
#         return name

#     for k, v in excel_fallback.items():
#         if k.startswith(f"{source_release_id}::"):
#             return v.get("jira_version_name", "")

#     return ""


# def build_work_items(target_projects, excel_fallback, source_version_names):
#     """
#     Resolves every issue's version by NAME against Jira's live version list
#     for its own project (get_or_create_version_for_project) — never trusts
#     a stored id_mapping/Excel id directly, since those can be stale or
#     scoped to the wrong project (the actual cause of the earlier
#     "Specify a valid value for fixVersions" failures).

#     If a version with that name already exists in the project, it's reused
#     (no duplicate created). Only genuinely missing versions get created.

#     Returns:
#         work_items: list of (source_issue_id, jira_key, jira_version_id, version_name,
#                               source_release_id, project)
#         skipped_no_release: issues with no source_release_id, or no resolvable
#                              version name, or version resolution/creation failed
#         created_count: how many versions were newly created in Jira
#         healed_count: how many id_mapping entries were written/refreshed this run
#     """
#     issues = core.load_issues_meta()

#     work_items = []
#     skipped_no_release = []
#     created_count = 0
#     healed_count = 0

#     for item in issues:
#         source_issue_id = str(item.get("source_issue_id"))
#         source_release_id = str(item.get("source_release_id", "")).split(".")[0]

#         issue_info = core.id_mapping["issues"].get(source_issue_id)
#         if not issue_info:
#             continue  # issue itself not created yet — nothing to assign to

#         jira_key = issue_info["jira_key"]
#         project = _project_from_jira_key(jira_key)

#         if project not in target_projects:
#             continue  # not one of the projects we're targeting this run

#         if source_release_id in ("", "None", "nan"):
#             skipped_no_release.append({"source_id": source_issue_id, "jira_key": jira_key})
#             continue

#         key = _version_key(source_release_id, project)
#         version_name = _resolve_version_name(key, source_release_id, source_version_names, excel_fallback)

#         if not version_name:
#             skipped_no_release.append({
#                 "source_id": source_issue_id,
#                 "jira_key": jira_key,
#                 "reason": f"No version name resolvable for release '{source_release_id}' "
#                           f"(not in id_mapping, versions.json, or Excel)",
#             })
#             continue

#         # Live-verified against Jira's actual version list for this project —
#         # reuses an existing version by name if found, creates only if missing.
#         jira_version_id, jira_version_name, was_created = get_or_create_version_for_project(
#             project, version_name, source_release_id
#         )

#         if was_created:
#             created_count += 1

#         if not jira_version_id:
#             skipped_no_release.append({
#                 "source_id": source_issue_id,
#                 "jira_key": jira_key,
#                 "reason": f"Could not resolve or create version '{version_name}' in project '{project}'",
#             })
#             continue

#         # Heal id_mapping only if this differs from (or is missing from)
#         # what's already stored, so we're not rewriting the same value
#         # every single issue that shares this release/project.
#         existing_mapped = core.id_mapping["versions"].get(key)
#         if not existing_mapped or str(existing_mapped.get("jira_version_id")) != str(jira_version_id):
#             _heal_id_mapping(
#                 key, jira_version_id, jira_version_name, source_release_id,
#                 "live_verified_lookup",
#             )
#             healed_count += 1

#         work_items.append((
#             source_issue_id,
#             jira_key,
#             jira_version_id,
#             jira_version_name,
#             source_release_id,
#             project,
#         ))

#     return work_items, skipped_no_release, created_count, healed_count


# # =============================================================================
# # ASSIGN (with retry-on-invalid-fixversion)
# # =============================================================================

# def assign_versions_for_projects(target_projects, dry_run=False):
#     print("\n" + "━" * 70)
#     print(f"  Assigning fixVersions — projects: {', '.join(sorted(target_projects))}")
#     print("━" * 70)

#     excel_fallback = load_versions_excel_fallback()
#     source_version_names = load_source_version_names()

#     work_items, skipped_no_release, created_count, healed_count = build_work_items(
#         target_projects, excel_fallback, source_version_names
#     )

#     if healed_count:
#         with core.mapping_lock:
#             core.save_mapping()
#         print(f"  ✔ Healed {healed_count} version(s) into id_mapping.json "
#               f"({created_count} newly created in Jira)")

#     print(f"  {len(work_items)} issue(s) to assign")
#     if skipped_no_release:
#         print(f"  {len(skipped_no_release)} issue(s) skipped — no source_release_id / no resolvable version name")

#     if dry_run:
#         for source_id, jira_key, version_id, version_name, _, _ in work_items[:20]:
#             print(f"  (dry-run) {jira_key} <- version {version_id} ({version_name})")
#         if len(work_items) > 20:
#             print(f"  ... and {len(work_items) - 20} more")
#         return {"success": [], "failed": [], "skipped_no_release": skipped_no_release, "skipped_no_version": []}

#     success = []
#     failed = []
#     lock = Lock()

#     def _put_fixversion(jira_key, version_id):
#         payload = {"update": {"fixVersions": [{"add": {"id": version_id}}]}}
#         return core.jira_put(f"api/3/issue/{jira_key}", payload)

#     def _assign(work_item):
#         source_id, jira_key, version_id, version_name, source_release_id, project = work_item
#         data, status = _put_fixversion(jira_key, version_id)

#         if status == 204:
#             with lock:
#                 success.append({"source_id": source_id, "jira_key": jira_key, "version_id": version_id, "version_name": version_name})
#                 count = len(success)
#             if count % 50 == 0:
#                 core.thread_safe_print(f"  Assigned {count} versions...")
#             return

#         error_msg = str(data.get("errors", data.get("errorMessages", data)) if data else f"HTTP {status}")

#         # ── RETRY: the version id we had doesn't belong to this project ──
#         if _is_invalid_fixversion_error(error_msg):
#             retry_name = version_name or source_version_names.get(source_release_id, "")
#             if retry_name:
#                 core.thread_safe_print(
#                     f"     ↻ [{jira_key}] version id {version_id} invalid for project '{project}' — "
#                     f"resolving/creating '{retry_name}' in '{project}' and retrying"
#                 )
#                 new_id, new_name, _ = get_or_create_version_for_project(project, retry_name, source_release_id)
#                 if new_id:
#                     key = _version_key(source_release_id, project)
#                     _heal_id_mapping(key, new_id, new_name, source_release_id, "healed_after_invalid_fixversion_retry")
#                     retry_data, retry_status = _put_fixversion(jira_key, new_id)
#                     if retry_status == 204:
#                         with lock:
#                             success.append({"source_id": source_id, "jira_key": jira_key, "version_id": new_id, "version_name": new_name})
#                             count = len(success)
#                         if count % 50 == 0:
#                             core.thread_safe_print(f"  Assigned {count} versions...")
#                         return
#                     error_msg = str(
#                         retry_data.get("errors", retry_data.get("errorMessages", retry_data))
#                         if retry_data else f"HTTP {retry_status} (after retry)"
#                     )

#         with lock:
#             failed.append({"source_id": source_id, "jira_key": jira_key, "version_id": version_id, "reason": error_msg})
#         core.thread_safe_print(f"   \u2716 Failed to assign version to {jira_key}: {error_msg[:150]}")

#     if work_items:
#         workers = min(core.VERSION_ASSIGN_MAX_WORKERS, len(work_items))
#         print(f"  Workers: {workers}\n")
#         with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
#             futures = [executor.submit(_assign, wi) for wi in work_items]
#             for future in concurrent.futures.as_completed(futures):
#                 try:
#                     future.result()
#                 except Exception as e:
#                     core.thread_safe_print(f"   \u2716 Version-assign worker exception: {e}")

#     # Flush any healing done during retries.
#     with core.mapping_lock:
#         core.save_mapping()

#     print(f"\n   \u2714 Assigned fixVersions to {len(success)} issues")
#     print(f"   \u2716 Failed: {len(failed)}")

#     return {
#         "success": success,
#         "failed": failed,
#         "skipped_no_release": skipped_no_release,
#         "skipped_no_version": [],
#     }


# def save_report(results, target_projects):
#     report_path = os.path.join(core.MAPPING_DIR, "version_assign_remaining_projects_report.json")
#     with open(report_path, "w", encoding="utf-8") as f:
#         json.dump(results, f, indent=2)
#     print(f"   \u2714 JSON report saved -> {report_path}")

#     rows = []
#     for r in results["success"]:
#         rows.append({"Source ID": r["source_id"], "Jira Key": r["jira_key"], "Status": "Assigned",
#                      "Version ID": r["version_id"], "Version Name": r["version_name"], "Reason": ""})
#     for r in results["failed"]:
#         rows.append({"Source ID": r["source_id"], "Jira Key": r["jira_key"], "Status": "Failed",
#                      "Version ID": r["version_id"], "Version Name": "", "Reason": r["reason"]})
#     for r in results["skipped_no_release"]:
#         rows.append({"Source ID": r["source_id"], "Jira Key": r["jira_key"], "Status": "Skipped",
#                      "Version ID": "", "Version Name": "", "Reason": r.get("reason", "No source_release_id")})

#     if rows:
#         df = pd.DataFrame(rows)
#         xlsx_path = os.path.join(core.MAPPING_DIR, "version_assign_remaining_projects_report.xlsx")
#         df.to_excel(xlsx_path, index=False)
#         print(f"   \u2714 Excel report saved -> {xlsx_path}")


# def main():
#     parser = argparse.ArgumentParser(description="Assign fixVersions only for issues in specific (still-missing) projects")
#     parser.add_argument("--projects", default="CEMIG,PLAT", help="Comma-separated Jira project keys to target (default: CEMIG,PLAT)")
#     parser.add_argument("--dry-run", action="store_true", help="Show what would be assigned, without calling Jira")
#     args = parser.parse_args()

#     target_projects = {p.strip().upper() for p in args.projects.split(",") if p.strip()}

#     print("=" * 70)
#     print("  ASSIGN VERSIONS — REMAINING PROJECTS ONLY")
#     print("=" * 70)
#     print(f"  Target projects: {sorted(target_projects)}")
#     print(f"  Dry run:         {args.dry_run}")
#     print("=" * 70)

#     core.start_rate_monitor()
#     if not args.dry_run and not core.validate_all_accounts():
#         core.stop_rate_monitor()
#         return 1

#     try:
#         results = assign_versions_for_projects(target_projects, dry_run=args.dry_run)
#         if not args.dry_run:
#             save_report(results, target_projects)
#     finally:
#         core.stop_rate_monitor()

#     print("\n" + "=" * 70)
#     print("  DONE")
#     print("=" * 70)
#     return 0


# if __name__ == "__main__":
#     exit(main())
"""
assign_versions_remaining_projects.py

Assigns fixVersions ONLY to issues belonging to specific projects
(default: CEMIG, PLAT) — for when other projects (e.g. EX) are already
fully assigned and you don't want to re-touch ~7,500 already-correct
issues just to pick up the couple of projects still missing.

Uses the SAME composite-key lookup as fix_multiproject_versions.py
(source_release_id::project), so each issue only ever gets a version
that belongs to its OWN project — the actual fix for the earlier
HTTP 400s (an issue in one project being handed a version ID that
belongs to a different project).

VERSION RESOLUTION (in order):
  1. id_mapping["versions"][source_release_id::project]  (already known-good)
  2. versions_mapping.xlsx, same composite key             (healed into id_mapping)
  3. If STILL no valid version for this exact project — i.e. neither of the
     above has an entry scoped to this project — the version is CREATED in
     Jira under the target project (reusing the original version's name,
     looked up from versions.json), then that new id is used and healed
     into id_mapping.

RETRY ON "Specify a valid value for fixVersions":
  This error means the version id we sent doesn't belong to the issue's
  project in Jira (e.g. an id that's actually an EX version, reused by
  mistake for a PLAT/CEMIG issue via a bad id_mapping/Excel entry). Any
  issue that fails with this specific error is automatically retried:
  we look up (or create) the correct version for that issue's OWN project
  by name, heal id_mapping with the corrected id, and PUT again once.

Usage:
    python assign_versions_remaining_projects.py
    python assign_versions_remaining_projects.py --projects CEMIG,PLAT
    python assign_versions_remaining_projects.py --dry-run
"""

import os
import json
import argparse
import concurrent.futures
from threading import Lock

import pandas as pd

import bulk_load_to_jira as core  # reuse .env, jira_credentials, jira_put, id_mapping, etc.


def _project_from_jira_key(jira_key):
    return jira_key.split("-")[0] if jira_key and "-" in jira_key else None


def _version_key(source_release_id, project_key):
    return f"{source_release_id}::{project_key}"


def _clean_val(val):
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s.lower() in ("nan", "none") else s


def _is_invalid_fixversion_error(error_msg):
    msg = str(error_msg).lower()
    return "fixversions" in msg and "valid value" in msg


# =============================================================================
# VERSION NAME LOOKUP (source_release_id -> name), independent of project
# =============================================================================

def load_source_version_names():
    """
    source_release_id -> version name, taken from versions.json's own
    payload (the original create-version payload prepare_payload.py wrote).
    This is project-agnostic — the same release name is what gets created
    in whichever project actually needs it.
    """
    versions = core.load_json("versions.json")
    names = {}
    for item in versions:
        source_id = str(item.get("source_release_id", ""))
        payload = item.get("payload", {}) or {}
        name = payload.get("name")
        if source_id and name:
            names[source_id] = name
    return names


def load_versions_excel_fallback():
    """
    Reads versions_mapping.xlsx (Version Name, Source Release ID, Project,
    Jira Version ID) and returns {composite_key: {jira_version_id,
    jira_version_name}}, keyed the same way as id_mapping["versions"]
    (source_release_id::project).

    Returns an empty dict (never raises) if the file doesn't exist or
    can't be read — this is a fallback source, not a requirement.
    """
    xlsx_path = os.path.join(core.MAPPING_DIR, "versions_mapping.xlsx")
    fallback = {}

    if not os.path.exists(xlsx_path):
        print(f"  (no Excel fallback — {xlsx_path} not found)")
        return fallback

    try:
        df = pd.read_excel(xlsx_path, dtype=str)
        df.columns = [str(c).strip() for c in df.columns]

        required_cols = {"Source Release ID", "Project", "Jira Version ID"}
        missing_cols = required_cols - set(df.columns)
        if missing_cols:
            print(f"  ⚠ Excel fallback skipped — {xlsx_path} is missing column(s): {missing_cols}")
            return fallback

        for _, row in df.iterrows():
            source_release_id = _clean_val(row.get("Source Release ID"))
            project = _clean_val(row.get("Project")).upper()
            jira_version_id = _clean_val(row.get("Jira Version ID"))
            jira_version_name = _clean_val(row.get("Version Name"))

            if not source_release_id or not project or not jira_version_id:
                continue

            key = _version_key(source_release_id, project)
            fallback[key] = {
                "jira_version_id": jira_version_id,
                "jira_version_name": jira_version_name,
            }

        print(f"  ✔ Loaded {len(fallback)} version(s) from Excel fallback ({xlsx_path})")
    except Exception as e:
        print(f"  ⚠ Excel fallback skipped — failed to read {xlsx_path}: {e}")

    return fallback


# =============================================================================
# CREATE-OR-FIND VERSION IN A SPECIFIC PROJECT
# =============================================================================
# Thread-safe cache of project_key -> {version_name_lower: version_data},
# same pattern as core.load_versions()'s internal cache, so we only ever
# fetch a given project's version list once per run.
_project_versions_cache = {}
_project_versions_lock = Lock()


def _find_existing_version_in_project(project_key, version_name):
    with _project_versions_lock:
        cached = _project_versions_cache.get(project_key)
    if cached is None:
        data, status = core.jira_get(f"api/3/project/{project_key}/versions")
        lookup = {}
        if status == 200 and isinstance(data, list):
            for v in data:
                lookup[v.get("name", "").strip().lower()] = v
        with _project_versions_lock:
            _project_versions_cache[project_key] = lookup
        core.thread_safe_print(f"     ↳ Fetched {len(lookup)} existing versions from project '{project_key}'")
        cached = lookup
    return cached.get(version_name.strip().lower())


def get_or_create_version_for_project(project_key, version_name, source_release_id):
    """
    Returns (jira_version_id, jira_version_name, was_created) for version_name
    scoped to project_key — creating it in Jira ONLY if it doesn't already
    exist there (checked live against Jira's real version list first, so an
    already-present version is always reused, never duplicated).
    Updates the project-versions cache so repeat lookups this run are free.
    Returns (None, None, False) if creation fails outright.
    """
    existing = _find_existing_version_in_project(project_key, version_name)
    if existing:
        return existing.get("id"), existing.get("name"), False

    payload = {"name": version_name, "project": project_key}
    data, status = core.jira_post("api/3/version", payload)

    if status == 201:
        with _project_versions_lock:
            _project_versions_cache.setdefault(project_key, {})[version_name.strip().lower()] = data
        core.thread_safe_print(f"     ✔ Created version '{version_name}' in project '{project_key}' (id {data.get('id')})")
        return data.get("id"), data.get("name"), True

    error_msg = str(data.get("errors", data.get("errorMessages", data)) if data else f"HTTP {status}")

    # Race condition / already exists — re-fetch and use it (not a creation).
    if "already exists" in error_msg.lower():
        with _project_versions_lock:
            _project_versions_cache.pop(project_key, None)  # force refetch
        existing = _find_existing_version_in_project(project_key, version_name)
        if existing:
            return existing.get("id"), existing.get("name"), False

    core.thread_safe_print(
        f"     ✖ Failed to create version '{version_name}' in project '{project_key}': {error_msg[:150]}"
    )
    return None, None, False


def _heal_id_mapping(key, jira_version_id, jira_version_name, source_release_id, note):
    with core.mapping_lock:
        core.id_mapping["versions"][key] = {
            "jira_version_id": jira_version_id,
            "jira_version_name": jira_version_name,
            "source_release_id": source_release_id,
            "resolved_via": note,
        }


# =============================================================================
# BUILD WORK ITEMS
# =============================================================================

def _resolve_version_name(key, source_release_id, source_version_names, excel_fallback):
    """
    Resolve a version NAME (never trust a stored ID directly) for this
    composite key, checking in order:
      1. id_mapping["versions"][key]["jira_version_name"]  (if present)
      2. versions_mapping.xlsx row for this exact key (name column)
      3. versions.json's own payload for this source_release_id
         (project-agnostic — same release name regardless of project)
      4. any Excel row for this source_release_id, any project
         (last resort, just for the name)
    Returns "" if no name can be found anywhere.
    """
    mapped = core.id_mapping["versions"].get(key)
    if mapped and mapped.get("jira_version_name"):
        return mapped["jira_version_name"]

    excel_entry = excel_fallback.get(key)
    if excel_entry and excel_entry.get("jira_version_name"):
        return excel_entry["jira_version_name"]

    name = source_version_names.get(source_release_id, "")
    if name:
        return name

    for k, v in excel_fallback.items():
        if k.startswith(f"{source_release_id}::"):
            return v.get("jira_version_name", "")

    return ""


def build_work_items(target_projects, excel_fallback, source_version_names):
    """
    Resolves every issue's version by NAME against Jira's live version list
    for its own project (get_or_create_version_for_project) — never trusts
    a stored id_mapping/Excel id directly, since those can be stale or
    scoped to the wrong project (the actual cause of the earlier
    "Specify a valid value for fixVersions" failures).

    If a version with that name already exists in the project, it's reused
    (no duplicate created). Only genuinely missing versions get created.

    Returns:
        work_items: list of (source_issue_id, jira_key, jira_version_id, version_name,
                              source_release_id, project)
        skipped_no_release: issues with no source_release_id, or no resolvable
                             version name, or version resolution/creation failed
        created_count: how many versions were newly created in Jira
        healed_count: how many id_mapping entries were written/refreshed this run
    """
    issues = core.load_issues_meta()

    work_items = []
    skipped_no_release = []
    created_count = 0
    healed_count = 0

    for item in issues:
        source_issue_id = str(item.get("source_issue_id"))
        source_release_id = str(item.get("source_release_id", "")).split(".")[0]

        issue_info = core.id_mapping["issues"].get(source_issue_id)
        if not issue_info:
            continue  # issue itself not created yet — nothing to assign to

        jira_key = issue_info["jira_key"]
        project = _project_from_jira_key(jira_key)

        if project not in target_projects:
            continue  # not one of the projects we're targeting this run

        if source_release_id in ("", "None", "nan"):
            skipped_no_release.append({"source_id": source_issue_id, "jira_key": jira_key})
            continue

        key = _version_key(source_release_id, project)
        version_name = _resolve_version_name(key, source_release_id, source_version_names, excel_fallback)

        if not version_name:
            skipped_no_release.append({
                "source_id": source_issue_id,
                "jira_key": jira_key,
                "reason": f"No version name resolvable for release '{source_release_id}' "
                          f"(not in id_mapping, versions.json, or Excel)",
            })
            continue

        # Live-verified against Jira's actual version list for this project —
        # reuses an existing version by name if found, creates only if missing.
        jira_version_id, jira_version_name, was_created = get_or_create_version_for_project(
            project, version_name, source_release_id
        )

        if was_created:
            created_count += 1

        if not jira_version_id:
            skipped_no_release.append({
                "source_id": source_issue_id,
                "jira_key": jira_key,
                "reason": f"Could not resolve or create version '{version_name}' in project '{project}'",
            })
            continue

        # Heal id_mapping only if this differs from (or is missing from)
        # what's already stored, so we're not rewriting the same value
        # every single issue that shares this release/project.
        existing_mapped = core.id_mapping["versions"].get(key)
        if not existing_mapped or str(existing_mapped.get("jira_version_id")) != str(jira_version_id):
            _heal_id_mapping(
                key, jira_version_id, jira_version_name, source_release_id,
                "live_verified_lookup",
            )
            healed_count += 1

        work_items.append((
            source_issue_id,
            jira_key,
            jira_version_id,
            jira_version_name,
            source_release_id,
            project,
        ))

    return work_items, skipped_no_release, created_count, healed_count


# =============================================================================
# ASSIGN (with retry-on-invalid-fixversion)
# =============================================================================

def assign_versions_for_projects(target_projects, dry_run=False):
    print("\n" + "━" * 70)
    print(f"  Assigning fixVersions — projects: {', '.join(sorted(target_projects))}")
    print("━" * 70)

    excel_fallback = load_versions_excel_fallback()
    source_version_names = load_source_version_names()

    work_items, skipped_no_release, created_count, healed_count = build_work_items(
        target_projects, excel_fallback, source_version_names
    )

    if healed_count:
        with core.mapping_lock:
            core.save_mapping()
        print(f"  ✔ Healed {healed_count} version(s) into id_mapping.json "
              f"({created_count} newly created in Jira)")

    print(f"  {len(work_items)} issue(s) to assign")
    if skipped_no_release:
        print(f"  {len(skipped_no_release)} issue(s) skipped — no source_release_id / no resolvable version name")

    if dry_run:
        for source_id, jira_key, version_id, version_name, _, _ in work_items[:20]:
            print(f"  (dry-run) {jira_key} <- version {version_id} ({version_name})")
        if len(work_items) > 20:
            print(f"  ... and {len(work_items) - 20} more")
        return {"success": [], "already_assigned": [], "failed": [], "skipped_no_release": skipped_no_release, "skipped_no_version": []}

    success = []
    failed = []
    already_assigned = []
    lock = Lock()

    def _put_fixversion(jira_key, version_id):
        payload = {"update": {"fixVersions": [{"add": {"id": version_id}}]}}
        return core.jira_put(f"api/3/issue/{jira_key}", payload)

    def _get_current_fixversion_ids(jira_key):
        """
        STEP 2: Ask Jira what fixVersions this issue currently has, so we
        never blind-PUT an issue that's already correctly assigned.
        Returns a set of version id strings, or None if the GET failed
        (caller treats that as "unknown — attempt the assign anyway").
        """
        data, status = core.jira_get(f"api/3/issue/{jira_key}?fields=fixVersions")
        if status != 200 or not data:
            return None
        versions = data.get("fields", {}).get("fixVersions", []) or []
        return {str(v.get("id")) for v in versions if v.get("id")}

    def _assign(work_item):
        source_id, jira_key, version_id, version_name, source_release_id, project = work_item

        # ── STEP 2: check Jira's CURRENT state before writing anything ──
        current_ids = _get_current_fixversion_ids(jira_key)
        if current_ids is not None and str(version_id) in current_ids:
            with lock:
                already_assigned.append({
                    "source_id": source_id, "jira_key": jira_key,
                    "version_id": version_id, "version_name": version_name,
                })
                count = len(already_assigned)
            if count % 200 == 0:
                core.thread_safe_print(f"  {count} already correctly assigned (skipped)...")
            return  # already has it — no PUT sent at all

        # ── Not present yet (or GET failed — attempt anyway) — assign it ─
        data, status = _put_fixversion(jira_key, version_id)

        if status == 204:
            with lock:
                success.append({"source_id": source_id, "jira_key": jira_key, "version_id": version_id, "version_name": version_name})
                count = len(success)
            if count % 50 == 0:
                core.thread_safe_print(f"  Assigned {count} versions...")
            return

        error_msg = str(data.get("errors", data.get("errorMessages", data)) if data else f"HTTP {status}")

        # ── RETRY: the version id we had doesn't belong to this project ──
        if _is_invalid_fixversion_error(error_msg):
            retry_name = version_name or source_version_names.get(source_release_id, "")
            if retry_name:
                core.thread_safe_print(
                    f"     ↻ [{jira_key}] version id {version_id} invalid for project '{project}' — "
                    f"resolving/creating '{retry_name}' in '{project}' and retrying"
                )
                new_id, new_name, _ = get_or_create_version_for_project(project, retry_name, source_release_id)
                if new_id:
                    key = _version_key(source_release_id, project)
                    _heal_id_mapping(key, new_id, new_name, source_release_id, "healed_after_invalid_fixversion_retry")

                    # Re-check current state with the corrected id too —
                    # it's possible the issue already has THIS version.
                    if current_ids is not None and str(new_id) in current_ids:
                        with lock:
                            already_assigned.append({
                                "source_id": source_id, "jira_key": jira_key,
                                "version_id": new_id, "version_name": new_name,
                            })
                        return

                    retry_data, retry_status = _put_fixversion(jira_key, new_id)
                    if retry_status == 204:
                        with lock:
                            success.append({"source_id": source_id, "jira_key": jira_key, "version_id": new_id, "version_name": new_name})
                            count = len(success)
                        if count % 50 == 0:
                            core.thread_safe_print(f"  Assigned {count} versions...")
                        return
                    error_msg = str(
                        retry_data.get("errors", retry_data.get("errorMessages", retry_data))
                        if retry_data else f"HTTP {retry_status} (after retry)"
                    )

        with lock:
            failed.append({"source_id": source_id, "jira_key": jira_key, "version_id": version_id, "reason": error_msg})
        core.thread_safe_print(f"   \u2716 Failed to assign version to {jira_key}: {error_msg[:150]}")

    if work_items:
        workers = min(core.VERSION_ASSIGN_MAX_WORKERS, len(work_items))
        print(f"  Workers: {workers}\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_assign, wi) for wi in work_items]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    core.thread_safe_print(f"   \u2716 Version-assign worker exception: {e}")

    # Flush any healing done during retries.
    with core.mapping_lock:
        core.save_mapping()

    print(f"\n   \u2714 Newly assigned fixVersions: {len(success)} issues")
    print(f"   \u2713 Already correctly assigned (skipped, no PUT sent): {len(already_assigned)} issues")
    print(f"   \u2716 Failed: {len(failed)}")

    return {
        "success": success,
        "already_assigned": already_assigned,
        "failed": failed,
        "skipped_no_release": skipped_no_release,
        "skipped_no_version": [],
    }


def save_report(results, target_projects):
    report_path = os.path.join(core.MAPPING_DIR, "version_assign_remaining_projects_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"   \u2714 JSON report saved -> {report_path}")

    rows = []
    for r in results["success"]:
        rows.append({"Source ID": r["source_id"], "Jira Key": r["jira_key"], "Status": "Assigned",
                     "Version ID": r["version_id"], "Version Name": r["version_name"], "Reason": ""})
    for r in results.get("already_assigned", []):
        rows.append({"Source ID": r["source_id"], "Jira Key": r["jira_key"], "Status": "Already Assigned (skipped)",
                     "Version ID": r["version_id"], "Version Name": r["version_name"], "Reason": ""})
    for r in results["failed"]:
        rows.append({"Source ID": r["source_id"], "Jira Key": r["jira_key"], "Status": "Failed",
                     "Version ID": r["version_id"], "Version Name": "", "Reason": r["reason"]})
    for r in results["skipped_no_release"]:
        rows.append({"Source ID": r["source_id"], "Jira Key": r["jira_key"], "Status": "Skipped",
                     "Version ID": "", "Version Name": "", "Reason": r.get("reason", "No source_release_id")})

    if rows:
        df = pd.DataFrame(rows)
        xlsx_path = os.path.join(core.MAPPING_DIR, "version_assign_remaining_projects_report.xlsx")
        df.to_excel(xlsx_path, index=False)
        print(f"   \u2714 Excel report saved -> {xlsx_path}")


def main():
    parser = argparse.ArgumentParser(description="Assign fixVersions only for issues in specific (still-missing) projects")
    parser.add_argument("--projects", default="CEMIG,PLAT", help="Comma-separated Jira project keys to target (default: CEMIG,PLAT)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be assigned, without calling Jira")
    args = parser.parse_args()

    target_projects = {p.strip().upper() for p in args.projects.split(",") if p.strip()}

    print("=" * 70)
    print("  ASSIGN VERSIONS — REMAINING PROJECTS ONLY")
    print("=" * 70)
    print(f"  Target projects: {sorted(target_projects)}")
    print(f"  Dry run:         {args.dry_run}")
    print("=" * 70)

    core.start_rate_monitor()
    if not args.dry_run and not core.validate_all_accounts():
        core.stop_rate_monitor()
        return 1

    try:
        results = assign_versions_for_projects(target_projects, dry_run=args.dry_run)
        if not args.dry_run:
            save_report(results, target_projects)
    finally:
        core.stop_rate_monitor()

    print("\n" + "=" * 70)
    print("  DONE")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    exit(main())