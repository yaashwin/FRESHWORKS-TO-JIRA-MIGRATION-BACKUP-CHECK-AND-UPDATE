"""
run_patch_descriptions_fixed.py

Standalone, FIXED version of the "--patch-descriptions" phase.

ROOT CAUSE OF THE BUG YOU HIT:
    update_posted_issues_with_attachments() (in bulk_load_to_jira.py) reads
    each issue's description via hydrate_issue(item), which reads from the
    per-issue cache file at payload/issues_full/{source_id}.json.

    rebuild_issue_payloads_with_attachments() calls
    prepare_payload.prepare_issues(attachment_mapping) to regenerate
    payloads WITH attachment links embedded — but that only rewrites the
    monolithic issues.json (and possibly issues_meta.json), NOT the
    individual issues_full/{id}.json cache files.

    Result: hydrate_issue() keeps returning the OLD, pre-attachment
    description. The PUT "succeeds" (204) because it's valid, but the
    content sent is byte-identical to what's already on the issue — so
    Jira doesn't even record a changelog entry, and no attachment link
    ever actually lands in the description.

FIX:
    Skip the disk round-trip entirely. Take the `issue_payloads` list
    that pp.prepare_issues() already returns in memory (correct,
    freshly-rebuilt descriptions) and patch directly from that — never
    touch hydrate_issue() / issues_full/ for this phase.

    Also added:
      - A pre-check: fetch the issue's CURRENT description from Jira
        before patching, and compare against what we're about to send.
        If they're already identical, we skip and flag it loudly instead
        of quietly "succeeding" — so you never get fooled by a no-op PUT
        again.
      - Thread pool (same PATCH_MAX_WORKERS-style pattern as the rest of
        your pipeline), since the original was a plain sequential loop.

Usage:
    python run_patch_descriptions_fixed.py
"""

import os
import sys
import json
import concurrent.futures
from threading import Lock

import bulk_load_to_jira as core

MAX_DESC_PATCH_WORKERS = int(os.getenv("DESC_PATCH_MAX_WORKERS", "6"))


def main():
    print("=" * 70)
    print("  PATCH (FIXED): Updating Issue Descriptions with Attachment Links")
    print("=" * 70)

    core.start_rate_monitor()
    if not core.validate_all_accounts():
        core.stop_rate_monitor()
        return 1

    try:
        # ── Rebuild payloads WITH attachment links, kept IN MEMORY ─────────
        import prepare_payload as pp
        attachment_mapping = pp.load_attachment_mapping()
        issue_payloads = pp.prepare_issues(attachment_mapping)
        print(f"   ✔ {len(issue_payloads)} issue payloads rebuilt with attachment links (in-memory).")

        # Lookup by source_issue_id straight from the fresh rebuild —
        # this is the fix: no hydrate_issue(), no issues_full/ disk read.
        payload_by_id = {
            str(p.get("source_issue_id")): p for p in issue_payloads
        }

        by_issue = attachment_mapping.get("by_source_issue_id", {})
        if not by_issue:
            print("   ⚠ attachment_mapping has no 'by_source_issue_id' entries — nothing to patch.")
            return 0

        # ── Build work list ─────────────────────────────────────────────
        work_items = []
        skipped_no_mapping = 0
        skipped_no_payload  = 0
        skipped_no_desc     = 0

        for source_issue_id, attachments in by_issue.items():
            posted_info = core.id_mapping.get("issues", {}).get(source_issue_id)
            if not posted_info:
                skipped_no_mapping += 1
                continue

            fresh_item = payload_by_id.get(source_issue_id)
            if not fresh_item:
                skipped_no_payload += 1
                continue

            description = fresh_item.get("payload", {}).get("fields", {}).get("description")
            if not description:
                skipped_no_desc += 1
                continue

            work_items.append((source_issue_id, posted_info["jira_key"], description, len(attachments)))

        print(f"\n  {len(work_items)} issue(s) to patch  |  Workers: {min(MAX_DESC_PATCH_WORKERS, max(len(work_items), 1))}")
        if skipped_no_mapping or skipped_no_payload or skipped_no_desc:
            print(f"    (skipped: {skipped_no_mapping} not yet in id_mapping, "
                  f"{skipped_no_payload} missing from rebuilt payloads, "
                  f"{skipped_no_desc} no description field)\n")

        updated  = {"n": 0}
        noop     = {"n": 0}
        failed   = {"n": 0}
        lock = Lock()

        def _patch_one(work_item):
            source_issue_id, jira_key, new_description, attach_count = work_item

            # ── Pre-check: fetch current description, compare ──────────
            current, get_status = core.jira_get(f"api/3/issue/{jira_key}?fields=description")
            if get_status == 200 and current:
                current_desc = current.get("fields", {}).get("description")
                if current_desc == new_description:
                    with lock:
                        noop["n"] += 1
                    core.thread_safe_print(
                        f"   ⚠ {jira_key}: rebuilt description is IDENTICAL to what's already "
                        f"on the issue — skipping (this would have been a silent no-op PUT). "
                        f"Check attachment_mapping / mediaSingle node generation for source {source_issue_id}."
                    )
                    return

            patch_payload = {"fields": {"description": new_description}}
            data, status = core.jira_put(f"api/3/issue/{jira_key}", patch_payload)

            if status == 204:
                with lock:
                    updated["n"] += 1
                core.thread_safe_print(
                    f"   ✔ Updated description on {jira_key} with {attach_count} attachment(s)"
                )
            else:
                err = str(data.get("errors", data.get("errorMessages", data))) if data else "no body"
                with lock:
                    failed["n"] += 1
                core.thread_safe_print(f"   ✖ Failed to update {jira_key} (HTTP {status}): {err}")
                core.log_error("issue_description_update", source_issue_id, err)

        if work_items:
            workers = min(MAX_DESC_PATCH_WORKERS, len(work_items))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_patch_one, wi) for wi in work_items]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        core.thread_safe_print(f"   ✖ Worker exception: {e}")

        print(f"\n  Summary: {updated['n']} ✔ updated | {noop['n']} ⚠ already-identical (see above) | {failed['n']} ✖ failed")
        if noop["n"]:
            print(f"\n  {noop['n']} issue(s) had a rebuilt description identical to what's already")
            print(f"  in Jira — meaning prepare_issues() isn't actually embedding the attachment")
            print(f"  media link for those. Check prepare_payload.py's attachment-embedding logic")
            print(f"  (e.g. matching key used in attachment_mapping vs what prepare_issues looks up).")

    finally:
        core.save_error_log()
        core.stop_rate_monitor()

    return 0


if __name__ == "__main__":
    sys.exit(main())