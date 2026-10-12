"""
fix_multiproject_versions.py

Handles versions.json rows that share the same source_release_id but
belong to DIFFERENT Jira projects (e.g. source release "33752" needs to
become its own "NA" version in PLATDRY, DRY, AND abcdry — three separate
Jira Version objects, not one).

Root cause of the original error:
    id_mapping["versions"] was keyed ONLY by source_release_id. Since
    multiple rows share source_release_id "33752" (one per project), only
    the FIRST one to succeed got saved — every subsequent row for that
    same source_release_id then got compared against the WRONG project's
    existing-versions cache, producing:
        'A version with this name already exists in this project.'
    even though the target project (DRY / abcdry) had ZERO versions.

Fix: key id_mapping["versions"] by (source_release_id, project), so each
project gets its own independent version record for the same source
release id. assign_versions_to_issues is also fixed to look up the
version using the ISSUE's own project (parsed from its Jira key), not
just the bare source_release_id.

Usage:
    python fix_multiproject_versions.py                # post + assign
    python fix_multiproject_versions.py --skip-assign
    python fix_multiproject_versions.py --assign-only
"""

import sys
import argparse
import concurrent.futures
from threading import Lock

import bulk_load_to_jira as core   # reuse .env, jira_credentials, jira_request, id_mapping, etc.


def _version_key(source_release_id, project_key):
    return f"{source_release_id}::{project_key}"


def _project_from_jira_key(jira_key):
    return jira_key.split("-")[0] if jira_key and "-" in jira_key else None


def _migrate_legacy_version_keys():
    legacy = [k for k in core.id_mapping["versions"] if "::" not in k]
    if legacy:
        print(f"   ℹ {len(legacy)} legacy (pre-fix) version mapping entries found "
              f"— left as-is, won't block new per-project posting.")


def post_versions_per_project():
    print("\n" + "━" * 70)
    print("  Posting versions (composite key: source_release_id + project)")
    print("━" * 70)

    versions = core.load_json("versions.json")
    if not versions:
        return

    _migrate_legacy_version_keys()

    _project_versions_cache = {}
    _cache_lock = Lock()

    def _find_existing_version(project_key, version_name):
        with _cache_lock:
            cached = _project_versions_cache.get(project_key)
        if cached is None:
            data, status = core.jira_get(f"api/3/project/{project_key}/versions")
            lookup = {}
            if status == 200 and isinstance(data, list):
                for v in data:
                    lookup[v.get("name", "").strip().lower()] = v
            with _cache_lock:
                _project_versions_cache[project_key] = lookup
            core.thread_safe_print(f"     ↳ Fetched {len(lookup)} existing versions from project '{project_key}'")
            cached = lookup
        return cached.get(version_name.strip().lower())

    success, failed, skipped = {"n": 0}, {"n": 0}, {"n": 0}
    counter_lock = Lock()

    def _process(indexed_item):
        i, item = indexed_item
        source_id = str(item.get("source_release_id", ""))
        payload = item.get("payload", {})
        project_key = payload.get("project", "") or item.get("jira_project_key", "")

        if not project_key:
            core.log_error("version", source_id, "Missing project key in payload")
            with counter_lock:
                failed["n"] += 1
            return

        key = _version_key(source_id, project_key)

        if core.RESUME_MODE and key in core.id_mapping["versions"]:
            with counter_lock:
                skipped["n"] += 1
            return

        data, status = core.jira_post("api/3/version", payload)

        if status == 201:
            with core.mapping_lock:
                core.id_mapping["versions"][key] = {
                    "jira_version_id":   data.get("id"),
                    "jira_version_name": data.get("name"),
                    "source_release_id": source_id,
                    "project":           project_key,
                }
                core.save_mapping()
            with counter_lock:
                success["n"] += 1
            core.thread_safe_print(f"  [{i}/{len(versions)}] ✔ {data.get('name')} → {project_key}")
        else:
            error_msg = str(data.get("errors", data.get("errorMessages", data)))

            if "already exists" in error_msg.lower():
                version_name = payload.get("name", "")
                existing = _find_existing_version(project_key, version_name)
                if existing:
                    with core.mapping_lock:
                        core.id_mapping["versions"][key] = {
                            "jira_version_id":   existing.get("id"),
                            "jira_version_name": existing.get("name"),
                            "source_release_id": source_id,
                            "project":           project_key,
                            "resolved_via":      "already_exists_lookup",
                        }
                        core.save_mapping()
                    with counter_lock:
                        success["n"] += 1
                    core.thread_safe_print(
                        f"  [{i}/{len(versions)}] ✔ {version_name} → {project_key} (already existed — mapped)"
                    )
                    return
                else:
                    core.thread_safe_print(
                        f"  [{i}/{len(versions)}] ⚠ '{version_name}' reported as existing "
                        f"but no exact match in project '{project_key}' — check formatting"
                    )

            core.log_error("version", source_id, error_msg)
            with counter_lock:
                failed["n"] += 1
            core.thread_safe_print(f"  [{i}/{len(versions)}] ✖ {source_id} → {project_key}: {error_msg[:80]}")

    workers = min(core.MAX_VERSION_WORKERS, max(len(versions), 1))
    print(f"  {len(versions)} version row(s)  |  Workers: {workers}\n")
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_process, (i, item)) for i, item in enumerate(versions, 1)]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as e:
                core.thread_safe_print(f"   ✖ Version worker exception: {e}")

    print(f"\n  Summary: {success['n']} ✔ | {failed['n']} ✖ | {skipped['n']} skipped")


def assign_versions_per_project():
    print("\n" + "━" * 70)
    print("  Assigning fixVersions to issues (matched to each issue's own project)")
    print("━" * 70)

    issues = core.load_issues_meta()

    work_items = []
    unmatched = 0
    for item in issues:
        source_issue_id   = str(item.get("source_issue_id"))
        source_release_id = str(item.get("source_release_id", "")).split(".")[0]

        if source_release_id in ("", "None", "nan"):
            continue

        issue_info = core.id_mapping["issues"].get(source_issue_id)
        if not issue_info:
            continue

        jira_key = issue_info["jira_key"]
        project = _project_from_jira_key(jira_key)
        if not project:
            continue

        key = _version_key(source_release_id, project)
        version_info = core.id_mapping["versions"].get(key)

        if not version_info:
            # fall back to legacy single-key mapping (pre-fix data), last resort
            version_info = core.id_mapping["versions"].get(source_release_id)

        if not version_info:
            unmatched += 1
            continue

        work_items.append((jira_key, version_info["jira_version_id"]))

    assigned = {"n": 0}
    lock = Lock()

    def _assign(work_item):
        jira_key, version_id = work_item
        payload = {"update": {"fixVersions": [{"add": {"id": version_id}}]}}
        _, status = core.jira_put(f"api/3/issue/{jira_key}", payload)
        if status == 204:
            with lock:
                assigned["n"] += 1
                current = assigned["n"]
            if current % 50 == 0:
                core.thread_safe_print(f"  Assigned {current} versions...")
        else:
            core.thread_safe_print(f"   ✖ Failed to assign version to {jira_key} (HTTP {status})")

    if work_items:
        workers = min(core.VERSION_ASSIGN_MAX_WORKERS, len(work_items))
        print(f"  {len(work_items)} issue(s) to assign  |  Workers: {workers}\n")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_assign, wi) for wi in work_items]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    core.thread_safe_print(f"   ✖ Version-assign worker exception: {e}")

    print(f"\n   ✔ Assigned fixVersions to {assigned['n']} issues")
    if unmatched:
        print(f"   ⚠ {unmatched} issue(s) had a source_release_id but no matching "
              f"version for their own project — check versions.json coverage")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-assign", action="store_true")
    parser.add_argument("--assign-only", action="store_true")
    args = parser.parse_args()

    print("=" * 70)
    print("  MULTI-PROJECT VERSION FIX (composite key: source_release_id + project)")
    print("=" * 70)

    core.start_rate_monitor()
    if not core.validate_all_accounts():
        core.stop_rate_monitor()
        return 1

    try:
        if not args.assign_only:
            post_versions_per_project()
        if not args.skip_assign:
            assign_versions_per_project()
    finally:
        core.save_error_log()
        core.stop_rate_monitor()

    print("\n" + "=" * 70)
    print("  DONE")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())