"""
apply_wrong_versions_fix.py

Reads wrong_versions.json (produced and hand-reviewed after running
audit_wrong_versions.py) and applies exactly what's in it — nothing is
recomputed or re-resolved. If you deleted an entry from the file because
you didn't want it touched, this script will never see it and will never
touch that issue.

For each entry it sends ONE PUT per issue:
  - removes every id in "remove_ids"
  - adds "add_id" if it isn't null
(this is exactly "only remove the wrong version(s), keep/add the correct
one" — the same semantics as fix_issue_versions.py).

On success, the issue is recorded into known_correct_versions.json so
fix_issue_versions.py / audit_wrong_versions.py will treat it as
already-confirmed-correct on any future run.

Runs with a ThreadPoolExecutor (10 workers).

Usage:
    python apply_wrong_versions_fix.py                 # apply for real
    python apply_wrong_versions_fix.py --dry-run         # show what would happen, no PUTs
    python apply_wrong_versions_fix.py --limit 50         # only apply first 50 entries
    python apply_wrong_versions_fix.py --input custom.json   # use a different reviewed file
"""

import os
import sys
import json
import time
import argparse
import concurrent.futures
from threading import Lock

import requests
from dotenv import load_dotenv

try:
    import jira_credentials
    _HAVE_CREDENTIALS_MODULE = True
except Exception:
    _HAVE_CREDENTIALS_MODULE = False

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

JIRA_URL       = os.getenv("JIRA_URL")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

MAX_WORKERS    = int(os.getenv("VERSION_APPLY_MAX_WORKERS", "10"))
SINGLE_TIMEOUT = 60
RETRY_DELAY    = 5
MAX_RETRIES    = 3
RATE_LIMIT_DELAY = 0.3

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT_PATH = os.path.join(_SCRIPT_DIR, "wrong_versions.json")
APPLY_FAILED_PATH  = os.path.join(_SCRIPT_DIR, "apply_failed.json")
APPLY_REPORT_PATH  = os.path.join(_SCRIPT_DIR, "apply_report.json")
KNOWN_CORRECT_PATH = os.path.join(_SCRIPT_DIR, "known_correct_versions.json")
KNOWN_CORRECT_SAVE_EVERY = 50

print_lock  = Lock()
counts_lock = Lock()
counts = {"fixed": 0, "failed": 0}
failed_items = []
failed_lock = Lock()

known_correct = {}
known_correct_lock = Lock()
_known_correct_dirty_count = 0


def log(msg):
    with print_lock:
        print(msg)


def bump(key):
    with counts_lock:
        counts[key] += 1


def record_failure(entry, reason):
    with failed_lock:
        failed_items.append({
            "source_issue_id": entry.get("source_issue_id"),
            "jira_key": entry.get("jira_key"),
            "reason": reason,
        })
    bump("failed")


def load_known_correct():
    global known_correct
    if os.path.exists(KNOWN_CORRECT_PATH):
        try:
            with open(KNOWN_CORRECT_PATH, "r", encoding="utf-8") as f:
                known_correct = json.load(f)
        except (json.JSONDecodeError, OSError):
            known_correct = {}


def save_known_correct(force=False):
    global _known_correct_dirty_count
    with known_correct_lock:
        _known_correct_dirty_count += 1
        if not force and _known_correct_dirty_count % KNOWN_CORRECT_SAVE_EVERY != 0:
            return
        with open(KNOWN_CORRECT_PATH, "w", encoding="utf-8") as f:
            json.dump(known_correct, f, indent=2)


def mark_known_correct(jira_key, version_id):
    with known_correct_lock:
        known_correct[jira_key] = version_id
    save_known_correct()


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


def jira_put(endpoint, payload, retries=MAX_RETRIES):
    url = f"{JIRA_URL}/rest/{endpoint.lstrip('/')}"
    auth = _get_auth()

    for attempt in range(retries):
        try:
            resp = requests.put(url, json=payload, headers=JIRA_HEADERS, auth=auth, timeout=SINGLE_TIMEOUT)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", RETRY_DELAY))
                log(f"  [RATE LIMIT] 429 — sleeping {retry_after}s")
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


# =============================================================================
# PER-ENTRY APPLY
# =============================================================================

def apply_entry(entry, dry_run):
    jira_key = entry.get("jira_key")
    source_issue_id = entry.get("source_issue_id")
    version_id = entry.get("correct_version_id")
    version_name = entry.get("correct_version_name")
    remove_ids = entry.get("remove_ids") or []
    add_id = entry.get("add_id")

    if not jira_key or not version_id:
        record_failure(entry, "entry is missing jira_key or correct_version_id — skipped")
        log(f"  ✖ (src {source_issue_id}): malformed entry, skipped")
        return

    update_ops = [{"remove": {"id": vid}} for vid in remove_ids]
    if add_id:
        update_ops.append({"add": {"id": add_id}})

    if not update_ops:
        # Nothing to do — shouldn't normally happen since the audit only
        # writes entries that need a change, but be safe.
        log(f"  = {jira_key} (src {source_issue_id}): no-op entry, nothing to apply")
        return

    if dry_run:
        log(f"  ~ [DRY RUN] {jira_key} (src {source_issue_id}): would remove {remove_ids or 'none'} "
            f"→ {'add ' + str(add_id) if add_id else 'keep as is'} (target: '{version_name}')")
        bump("fixed")
        return

    payload = {"update": {"fixVersions": update_ops}}
    put_body, put_status = jira_put(f"api/3/issue/{jira_key}", payload)

    if put_status == 204:
        mark_known_correct(jira_key, version_id)
        bump("fixed")
        log(f"  ✔ {jira_key} (src {source_issue_id}): removed {remove_ids or 'none'} "
            f"{'and added ' + str(add_id) if add_id else ''} → now '{version_name}'")
    else:
        err_detail = None
        if isinstance(put_body, dict):
            err_detail = put_body.get("errors") or put_body.get("errorMessages") or put_body
        record_failure(entry, f"PUT failed (HTTP {put_status}): {err_detail}")
        log(f"  ✖ {jira_key} (src {source_issue_id}): PUT failed (HTTP {put_status}) — {err_detail}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Apply the reviewed wrong_versions.json to Jira.")
    parser.add_argument("--input", default=DEFAULT_INPUT_PATH, help="Path to the reviewed JSON (default: wrong_versions.json).")
    parser.add_argument("--dry-run", action="store_true", help="Don't call PUT, just show what would happen.")
    parser.add_argument("--limit", type=int, default=None, help="Only apply the first N entries.")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="Thread pool size (default 10).")
    args = parser.parse_args()

    if not JIRA_URL:
        print("✖ JIRA_URL not set (check your .env). Aborting.")
        return 1

    if not os.path.exists(args.input):
        print(f"✖ Input file not found: {args.input}")
        print("  Run audit_wrong_versions.py first, review its output, then run this.")
        return 1

    with open(args.input, "r", encoding="utf-8") as f:
        entries = json.load(f)

    if args.limit:
        entries = entries[:args.limit]

    load_known_correct()

    print("=" * 70)
    print("  APPLY WRONG VERSIONS FIX")
    print("=" * 70)
    print(f"  input file : {args.input}")
    print(f"  entries     : {len(entries)}")
    print(f"  dry run     : {args.dry_run}")
    print(f"  workers     : {args.workers}\n")

    if not entries:
        print("Nothing to do.")
        return 0

    start = time.time()
    workers = min(args.workers, max(len(entries), 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(apply_entry, entry, args.dry_run) for entry in entries]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as e:
                log(f"   ✖ Worker exception: {e}")

    elapsed = time.time() - start

    if not args.dry_run:
        save_known_correct(force=True)

    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  Fixed   : {counts['fixed']}")
    print(f"  Failed  : {counts['failed']}")
    print(f"  Time elapsed : {elapsed:.2f}s")
    print("=" * 70)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "input_file": args.input,
        "dry_run": args.dry_run,
        "counts": counts,
        "elapsed_seconds": elapsed,
    }
    with open(APPLY_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"  ✔ Report written → {APPLY_REPORT_PATH}")

    if failed_items:
        with open(APPLY_FAILED_PATH, "w", encoding="utf-8") as f:
            json.dump(failed_items, f, indent=2)
        print(f"  ⚠ {len(failed_items)} failed item(s) written → {APPLY_FAILED_PATH}")

    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())