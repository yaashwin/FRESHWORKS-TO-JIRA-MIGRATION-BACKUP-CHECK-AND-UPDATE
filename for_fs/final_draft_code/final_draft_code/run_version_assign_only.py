"""
run_version_assign_only.py

Standalone runner for version ASSIGNMENT only (the "attach fixVersions to
already-created issues" step) — use this when versions have already been
posted to Jira (via post_versions_to_jira.py) and issues are already
created, and you just want to (re)run the assignment step on its own.

Equivalent to:
    python post_versions_to_jira.py --assign-only

...just as its own file, same as run_sprint_assign_only.py, in case you'd
rather not pass CLI flags.

Reuses assign_versions_per_project() from post_versions_to_jira.py as-is
— no logic duplicated here. That function is already thread-pooled:
ThreadPoolExecutor(max_workers=core.VERSION_ASSIGN_MAX_WORKERS), same
pattern as everything else in bulk_load_to_jira.py. It matches each issue
to a version using the issue's OWN project (parsed from its Jira key) via
the composite (source_release_id, project) key — no cross-project
fallback, so it won't hand an issue a version from the wrong project.

Any failure is logged under entity_type == "version_assign" in
data/mapping/errors.json with the real Jira error body attached.

NOTE — like the sprint runner, this has no resume/skip tracking for
already-succeeded assignments; Jira's fixVersions "add" is idempotent
though (adding a version that's already on the issue is a harmless no-op),
so re-running this is safe, just not free — it re-checks/re-sends every
matched issue each time.

Usage:
    python run_version_assign_only.py
"""

import sys

import bulk_load_to_jira as core
from post_versions_to_jira import assign_versions_per_project


def main():
    print("=" * 70)
    print("  VERSION ASSIGNMENT ONLY (fixVersions -> issues)")
    print("=" * 70)

    core.start_rate_monitor()

    if not core.validate_all_accounts():
        core.stop_rate_monitor()
        return 1

    try:
        assign_versions_per_project()
    finally:
        core.save_error_log()
        core.stop_rate_monitor()

    print("\n" + "=" * 70)
    print("  DONE — check data/mapping/errors.json (entity_type=version_assign)")
    print("  for any tickets that failed to get their fixVersion, with the")
    print("  full Jira error body.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())