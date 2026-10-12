"""
run_status_assign_only.py

Standalone runner for applying status transitions (initial -> correct
target status) to already-created issues — the step that's normally baked
inline into process_batch_worker() / load_single_issue() during issue
creation. Use this to (re)run status transitions on their own, e.g. after
fixing workflow/permission issues, without re-running the whole migration.

WHY A PRE-CHECK IS NEEDED:
    apply_status_transition() (in bulk_load_to_jira.py) works by fetching
    the issue's currently AVAILABLE transitions and looking for one whose
    "to" status matches the target name. If the issue is already sitting
    in the target status, there is no such transition (you can't
    transition to your own current status), so it would print a false
    "No transition to 'X' found. Available: [...]" and report failure —
    even though the issue is already correct.

    This script fetches each issue's CURRENT status first and skips (as a
    clean no-op, not a failure) if it already matches the target. Only
    issues that are genuinely wrong get a transition attempt.

NOTE ON MULTI-HOP WORKFLOWS:
    apply_status_transition() does a single hop only. If your workflow
    requires e.g. To Do -> In Progress -> Done and an issue is sitting at
    "To Do" but needs to land on "Done", a single call won't get it there
    in one step (Jira's public transitions API doesn't support skipping
    workflow steps). This script reports that case as a failure with the
    available transitions listed, so you can see which issues need more
    than one hop.

Usage:
    python run_status_assign_only.py
"""

import sys
import os
import concurrent.futures
from threading import Lock

import bulk_load_to_jira as core

STATUS_ASSIGN_MAX_WORKERS = int(os.getenv("STATUS_ASSIGN_MAX_WORKERS", "6"))


def main():
    print("=" * 70)
    print("  STATUS TRANSITION ONLY (initial -> target status)")
    print("=" * 70)

    core.start_rate_monitor()
    if not core.validate_all_accounts():
        core.stop_rate_monitor()
        return 1

    try:
        issues = core.load_issues_meta()
        if not issues:
            print("  No issues found (issues_meta.json / issues.json empty or missing)")
            return 0

        # ── Build work list: only issues that are created AND have a target status ──
        work_items = []
        skipped_no_mapping = 0
        skipped_no_status  = 0

        for item in issues:
            source_issue_id = str(item.get("source_issue_id", ""))
            target_status   = item.get("jira_status", "")

            if not target_status or target_status in ("", "nan", "None", "NaT"):
                skipped_no_status += 1
                continue

            issue_info = core.id_mapping.get("issues", {}).get(source_issue_id)
            if not issue_info:
                skipped_no_mapping += 1
                continue

            work_items.append((source_issue_id, issue_info["jira_key"], target_status))

        print(f"\n  {len(work_items)} issue(s) to check/transition  |  "
              f"Workers: {min(STATUS_ASSIGN_MAX_WORKERS, max(len(work_items), 1))}")
        if skipped_no_mapping or skipped_no_status:
            print(f"    (skipped: {skipped_no_mapping} not yet created, "
                  f"{skipped_no_status} no target status in source data)\n")

        already_correct = {"n": 0}
        transitioned    = {"n": 0}
        failed          = {"n": 0}
        auth_errors     = {"n": 0}
        lock = Lock()

        # Circuit breaker: if we see too many consecutive 401/403s, it's an
        # account/permission problem, not a per-issue problem — stop
        # attempting transitions (which will just also fail with the same
        # bad auth) and surface it clearly instead of burning through the
        # whole issue list with useless calls.
        AUTH_ERROR_CIRCUIT_BREAKER = 15
        _circuit_tripped = {"flag": False}

        def _process_one(work_item):
            source_issue_id, jira_key, target_status = work_item

            if _circuit_tripped["flag"]:
                return  # stop doing work once we know auth is broken

            # ── Pre-check current status to avoid false "no transition found" ──
            current, get_status = core.jira_get(f"api/3/issue/{jira_key}?fields=status")

            if get_status in (401, 403):
                with lock:
                    auth_errors["n"] += 1
                    count = auth_errors["n"]
                err_body = current if current else "no body"
                core.thread_safe_print(
                    f"   ✖ {jira_key}: AUTH ERROR (HTTP {get_status}) — {err_body} "
                    f"— NOT attempting transition (would also fail)"
                )
                core.log_error(
                    "status_transition_auth", source_issue_id,
                    f"{jira_key}: HTTP {get_status} on status GET — {err_body}"
                )
                if count >= AUTH_ERROR_CIRCUIT_BREAKER and not _circuit_tripped["flag"]:
                    _circuit_tripped["flag"] = True
                    core.thread_safe_print(
                        f"\n   🛑 {count} consecutive auth errors — this looks like a "
                        f"missing-permission or invalidated-token problem for whichever "
                        f"service account is pinned to this thread, not a per-issue issue. "
                        f"Stopping remaining work in this run. Fix the account/permission "
                        f"and re-run — already-correct issues will be skipped safely.\n"
                    )
                return

            if get_status == 200 and current:
                current_status_name = current.get("fields", {}).get("status", {}).get("name", "")
                if current_status_name.lower() == target_status.lower():
                    with lock:
                        already_correct["n"] += 1
                    return
            else:
                core.thread_safe_print(
                    f"   ⚠ {jira_key}: couldn't fetch current status (HTTP {get_status}) — "
                    f"attempting transition anyway"
                )

            ok = core.apply_status_transition(jira_key, target_status)
            if ok:
                with lock:
                    transitioned["n"] += 1
                core.thread_safe_print(f"   ✔ {jira_key} -> '{target_status}'")
            else:
                with lock:
                    failed["n"] += 1
                core.log_error(
                    "status_transition", source_issue_id,
                    f"{jira_key}: could not reach target status '{target_status}' "
                    f"in a single hop (may need multi-step workflow transition)"
                )

        if work_items:
            workers = min(STATUS_ASSIGN_MAX_WORKERS, len(work_items))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_process_one, wi) for wi in work_items]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        core.thread_safe_print(f"   ✖ Worker exception: {e}")

        print(
            f"\n  Summary: {transitioned['n']} ✔ transitioned | "
            f"{already_correct['n']} ⏭ already correct | "
            f"{failed['n']} ✖ failed | "
            f"{auth_errors['n']} 🔒 auth errors"
        )
        if failed["n"]:
            print(f"  See data/mapping/errors.json (entity_type=status_transition) for the "
                  f"issues that couldn't reach their target status in one hop — these likely "
                  f"need a multi-step workflow transition (e.g. To Do -> In Progress -> Done).")
        if auth_errors["n"]:
            print(f"  See data/mapping/errors.json (entity_type=status_transition_auth) for the "
                  f"full Jira error body on each auth failure — fix the account/permission and "
                  f"re-run; already-correct issues will be skipped safely.")

    finally:
        core.save_error_log()
        core.stop_rate_monitor()

    return 0


if __name__ == "__main__":
    sys.exit(main())