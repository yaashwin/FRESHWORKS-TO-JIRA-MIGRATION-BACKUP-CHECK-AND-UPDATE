"""
run_sprint_assign_only.py

Standalone runner for Phase 3A (sprint assignment) only — use this when
versions/sprints/issues are already fully loaded and you just need to
(re)run sprint assignment without re-running the whole migration.

This does NOT re-implement the thread pool — bulk_load_to_jira.py's
assign_issues_to_sprints() is already parallelized the exact same way as
everything else in that script: it flattens all (sprint, chunk) work items
across every sprint into one list, then runs them through a
ThreadPoolExecutor sized to SPRINT_ASSIGN_MAX_WORKERS (env var
SPRINT_ASSIGN_MAX_WORKERS, default 6) — same pattern as MAX_ISSUE_WORKERS /
MAX_VERSION_WORKERS elsewhere. Calling it here just gives you that same
parallel behavior without re-running Phases 1-2.

Any failure is already logged via log_error("sprint_assign", ...) inside
assign_issues_to_sprints() with the real Jira error body attached — check
data/mapping/errors.json -> by_type -> "sprint_assign" after running this
to see exactly why a given issue didn't get linked to its sprint (most
common cause: the sprint's board isn't associated with that issue's
project — same class of problem as the version cross-project issue).

Usage:
    python run_sprint_assign_only.py
"""

import bulk_load_to_jira as core


def main():
    print("=" * 70)
    print("  SPRINT ASSIGNMENT ONLY (Phase 3A)")
    print("=" * 70)

    core.start_rate_monitor()

    if not core.validate_all_accounts():
        core.stop_rate_monitor()
        return 1

    try:
        core.assign_issues_to_sprints()
    finally:
        core.save_error_log()
        core.stop_rate_monitor()

    print("\n" + "=" * 70)
    print("  DONE — check data/mapping/errors.json (entity_type=sprint_assign)")
    print("  for any tickets that failed to link, with the full Jira error body.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())