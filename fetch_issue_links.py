"""
fetch_issue_links.py

Standalone script — reads a list of Jira issue keys from an Excel file
(or a failed_keys.json produced by a previous run), fetches each issue's
links (issuelinks) directly from Jira, and writes the results to an
output Excel with columns:

    jira_source_key | jira_target_key | link_type | direction

INPUT:
    /home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/
    transfer_summary/FS_export_summary.xlsx
    columns expected: jira_key, issue_type, FR_key   (only "jira_key" is used)

    You can ALSO pass a *_failed_keys.json (produced by a previous run of
    this script) as --input, and it will be read as a plain list of keys
    to retry — no need to go back to the original Excel.

CONCURRENCY:
    Uses the SAME multi-account pool as bulk_load_to_jira.py
    (jira_credentials.py). With 50 service accounts configured and
    WORKERS_PER_ACCOUNT = 5, this spins up 250 worker threads total —
    each account backs ~5 threads running in parallel (round-robin
    pinned on first use, same pattern as the main migration script).

RATE LIMITING:
    On HTTP 429, waits 30 seconds (or the Retry-After header value if
    it's larger) and retries the SAME request — up to MAX_RETRIES_429
    times — before giving up and logging it as a failure.

PER-ISSUE LOG:
    Every single issue processed — success with links, success with NO
    links, or a genuine failure — gets one line written to a log file
    (default: <output>_fetch_log.log), so you can cross-reference any
    specific jira_key or range of the run afterward and see EXACTLY
    what happened, not just cumulative totals in the console.

    Log line format:
        <timestamp> | <jira_key> | <STATUS> | http=<code> | links=<n> | attempts=<n> | elapsed=<s>s | reason=<...>

    STATUS is one of: OK_WITH_LINKS, OK_NO_LINKS, ERROR

FAILURE OUTPUT (only written when there ARE failures — nothing empty
is ever written to disk):
    <output>_errors.json       -> full detail per failed issue (jira_key,
                                   http_code, attempts, reason). For
                                   diagnosing WHY things failed.
    <output>_failed_keys.json  -> just a plain JSON list of the jira_keys
                                   that failed, nothing else. Meant to be
                                   fed straight back in as --input to
                                   re-run ONLY the failures:

                                       python fetch_issue_links.py \\
                                           --input FS_export_summary_links_failed_keys.json \\
                                           --output FS_export_summary_links_retry.xlsx

    If nothing failed, NEITHER of these files is written (no empty
    files left behind to confuse a later run).

Usage:
    python fetch_issue_links.py
    python fetch_issue_links.py --input /path/to/input.xlsx --output /path/to/output.xlsx
    python fetch_issue_links.py --input /path/to/output_failed_keys.json --output /path/to/retry.xlsx
    python fetch_issue_links.py --workers-per-account 5
"""

import os
import sys
import time
import json
import logging
import argparse
import concurrent.futures
from threading import Lock

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import pandas as pd

import jira_credentials  # SAME pool module used by bulk_load_to_jira.py

load_dotenv()

# =============================================================================
# CONFIGURATION
# =============================================================================
JIRA_URL = os.getenv("JIRA_URL")

DEFAULT_INPUT_PATH = (
    "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/"
    "transfer_summary/FS_export_summary.xlsx"
)
DEFAULT_OUTPUT_PATH = (
    "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/"
    "transfer_summary/FS_export_summary_links.xlsx"
)

WORKERS_PER_ACCOUNT_DEFAULT = 5     # each service account backs this many parallel threads
MAX_RETRIES_429 = 10                # give up after this many 429s on a single issue
DEFAULT_429_WAIT_SECONDS = 30       # wait this long on a 429 unless Retry-After says more
REQUEST_TIMEOUT = 30
CHECKPOINT_EVERY = 500              # flush partial results to disk every N issues processed

HEADERS = {"Accept": "application/json"}

print_lock = Lock()
results_lock = Lock()
stats_lock = Lock()


def thread_safe_print(msg):
    with print_lock:
        print(msg)


# =============================================================================
# PER-ISSUE RESULT LOGGER — set up in main() once we know the output path
# =============================================================================
_result_logger = None


def setup_result_logger(log_path: str):
    global _result_logger
    logger = logging.getLogger("fetch_issue_links.result")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        logger.addHandler(handler)
    _result_logger = logger
    return logger


def log_result(jira_key, status, http_code, link_count, attempts, elapsed, reason=""):
    msg = (
        f"{jira_key} | {status} | http={http_code} | links={link_count} | "
        f"attempts={attempts} | elapsed={elapsed:.2f}s"
    )
    if reason:
        msg += f" | reason={reason}"
    _result_logger.info(msg)


# =============================================================================
# AUTH — pinned per worker thread, round-robin across the account pool
# =============================================================================

def _get_auth_for_this_thread() -> HTTPBasicAuth:
    email, token = jira_credentials.get_auth_for_this_thread()
    return HTTPBasicAuth(email, token)


def _get_email_for_this_thread() -> str:
    """Whichever account this worker thread is pinned to — used to name names
    in 401/403 error reasons, so you don't need a separate diagnostic pass to
    find out which account is bad."""
    try:
        return jira_credentials.get_assigned_account_for_this_thread().get("email", "unknown")
    except Exception:
        return "unknown"


# =============================================================================
# FETCH ONE ISSUE'S LINKS (with 429 retry) — fully instrumented
# =============================================================================

def fetch_issue_links(jira_key: str):
    """
    Returns a dict with everything needed both for the output Excel AND
    the per-issue log line:
        {
          "rows": [...],            # list of link-row dicts (possibly empty)
          "status": "OK_WITH_LINKS" | "OK_NO_LINKS" | "ERROR",
          "http_code": int or None, # last HTTP status code seen
          "attempts": int,          # how many HTTP attempts this took
          "elapsed": float,         # seconds spent on this issue (all attempts)
          "reason": str,            # empty on success, exact failure detail on ERROR
        }
    """
    url = f"{JIRA_URL}/rest/api/3/issue/{jira_key}?fields=issuelinks"
    auth = _get_auth_for_this_thread()

    start = time.time()
    attempt = 0
    last_http_code = None

    while attempt < MAX_RETRIES_429:
        attempt += 1
        try:
            response = requests.get(url, headers=HEADERS, auth=auth, timeout=REQUEST_TIMEOUT)
            last_http_code = response.status_code
        except requests.exceptions.RequestException as e:
            if attempt >= MAX_RETRIES_429:
                return {
                    "rows": [], "status": "ERROR", "http_code": last_http_code,
                    "attempts": attempt, "elapsed": time.time() - start,
                    "reason": f"Request exception after {attempt} attempts: {type(e).__name__}: {e}",
                }
            time.sleep(5)
            continue

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", DEFAULT_429_WAIT_SECONDS))
            wait_seconds = max(retry_after, DEFAULT_429_WAIT_SECONDS)
            thread_safe_print(
                f"  [RATE LIMIT] 429 on {jira_key} — attempt {attempt}/{MAX_RETRIES_429}, "
                f"waiting {wait_seconds}s..."
            )
            if attempt >= MAX_RETRIES_429:
                return {
                    "rows": [], "status": "ERROR", "http_code": 429,
                    "attempts": attempt, "elapsed": time.time() - start,
                    "reason": f"Persistent 429 — gave up after {attempt} attempts",
                }
            time.sleep(wait_seconds)
            continue

        if response.status_code == 404:
            return {
                "rows": [], "status": "ERROR", "http_code": 404,
                "attempts": attempt, "elapsed": time.time() - start,
                "reason": "Issue not found (404) — check the key exists / account has project access",
            }

        if response.status_code == 401:
            acct_email = _get_email_for_this_thread()
            return {
                "rows": [], "status": "ERROR", "http_code": 401,
                "attempts": attempt, "elapsed": time.time() - start,
                "reason": f"Unauthorized (401) — bad/expired token for account "
                          f"{acct_email}: {response.text[:150]}",
                "account_email": acct_email,
            }

        if response.status_code == 403:
            acct_email = _get_email_for_this_thread()
            return {
                "rows": [], "status": "ERROR", "http_code": 403,
                "attempts": attempt, "elapsed": time.time() - start,
                "reason": f"Forbidden (403) — account {acct_email} "
                          f"lacks permission on this issue/project: {response.text[:150]}",
                "account_email": acct_email,
            }

        if response.status_code != 200:
            if attempt >= MAX_RETRIES_429:
                return {
                    "rows": [], "status": "ERROR", "http_code": response.status_code,
                    "attempts": attempt, "elapsed": time.time() - start,
                    "reason": f"HTTP {response.status_code}: {response.text[:200]}",
                }
            time.sleep(5)
            continue

        # ── Success — parse issuelinks ──────────────────────────────────
        try:
            data = response.json()
        except Exception as e:
            return {
                "rows": [], "status": "ERROR", "http_code": 200,
                "attempts": attempt, "elapsed": time.time() - start,
                "reason": f"Failed to parse JSON response: {type(e).__name__}: {e}",
            }

        fields = data.get("fields")
        if fields is None:
            return {
                "rows": [], "status": "ERROR", "http_code": 200,
                "attempts": attempt, "elapsed": time.time() - start,
                "reason": f"Response had no 'fields' key at all — raw keys: {list(data.keys())}",
            }

        issuelinks = fields.get("issuelinks", []) or []
        rows = []

        for link in issuelinks:
            link_type = link.get("type", {}).get("name", "")

            if "inwardIssue" in link:
                target_key = link["inwardIssue"].get("key", "")
                direction = "inward"
            elif "outwardIssue" in link:
                target_key = link["outwardIssue"].get("key", "")
                direction = "outward"
            else:
                continue  # malformed link entry, skip

            if not target_key:
                continue

            rows.append({
                "jira_source_key": jira_key,
                "jira_target_key": target_key,
                "link_type": link_type,
                "direction": direction,
            })

        status = "OK_WITH_LINKS" if rows else "OK_NO_LINKS"
        return {
            "rows": rows, "status": status, "http_code": 200,
            "attempts": attempt, "elapsed": time.time() - start,
            "reason": "",
        }

    return {
        "rows": [], "status": "ERROR", "http_code": last_http_code,
        "attempts": attempt, "elapsed": time.time() - start,
        "reason": f"Gave up after {MAX_RETRIES_429} attempts (persistent failure)",
    }


# =============================================================================
# LOAD INPUT — accepts either the original Excel, or a *_failed_keys.json
# produced by a previous run (so you can retry just the failures).
# =============================================================================

def load_jira_keys(input_path: str) -> list[str]:
    if not os.path.exists(input_path):
        print(f"\n✖ FATAL: input file not found: {input_path}")
        sys.exit(1)

    ext = os.path.splitext(input_path)[1].lower()

    if ext == ".json":
        # Expected shape: a plain JSON list of jira_key strings, exactly
        # what this script itself writes to *_failed_keys.json.
        with open(input_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception as e:
                print(f"\n✖ FATAL: could not parse JSON input {input_path}: {e}")
                sys.exit(1)

        if not isinstance(data, list):
            print(f"\n✖ FATAL: expected {input_path} to contain a JSON list of jira_key strings, "
                  f"got {type(data).__name__}")
            sys.exit(1)

        keys = []
        seen = set()
        for val in data:
            key = str(val).strip()
            if not key or key.lower() in ("nan", "none"):
                continue
            if key not in seen:
                seen.add(key)
                keys.append(key)

        print(f"  ✔ Loaded {len(keys)} unique Jira key(s) from {input_path} (retry list)")
        return keys

    # Otherwise treat it as Excel, same as before.
    df = pd.read_excel(input_path, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    if "jira_key" not in df.columns:
        print(f"\n✖ FATAL: 'jira_key' column not found in {input_path}. "
              f"Columns present: {list(df.columns)}")
        sys.exit(1)

    keys = []
    seen = set()
    for val in df["jira_key"]:
        if val is None:
            continue
        key = str(val).strip()
        if not key or key.lower() in ("nan", "none"):
            continue
        if key not in seen:
            seen.add(key)
            keys.append(key)

    print(f"  ✔ Loaded {len(keys)} unique Jira key(s) from {input_path}")
    return keys


# =============================================================================
# SAVE OUTPUT
# =============================================================================

def save_output(rows: list[dict], output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    df = pd.DataFrame(rows, columns=["jira_source_key", "jira_target_key", "link_type", "direction"])
    df.to_excel(output_path, index=False)
    thread_safe_print(f"   ✔ Excel saved -> {output_path}  ({len(df)} link rows)")


def save_failure_files(errors: list[dict], output_path: str):
    """
    Writes the two failure-tracking files — but ONLY if there are actual
    failures. If `errors` is empty, this does nothing (no empty JSON files
    left on disk from a run that had zero errors).

        <output>_errors.json       -> full detail (jira_key, http_code,
                                       attempts, reason) for diagnosis.
        <output>_failed_keys.json  -> plain list of just the jira_keys,
                                       ready to feed back in as --input
                                       for a retry-only rerun.
    """
    if not errors:
        return None, None

    base = os.path.splitext(output_path)[0]

    err_path = base + "_errors.json"
    with open(err_path, "w", encoding="utf-8") as f:
        json.dump(errors, f, indent=2)

    failed_keys = [e["jira_key"] for e in errors]
    failed_keys_path = base + "_failed_keys.json"
    with open(failed_keys_path, "w", encoding="utf-8") as f:
        json.dump(failed_keys, f, indent=2)

    thread_safe_print(
        f"   ⚠ {len(errors)} issue(s) failed — details -> {err_path}  |  "
        f"retry list -> {failed_keys_path}"
    )
    return err_path, failed_keys_path


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Fetch Jira issue links for a list of issue keys from Excel (or a previous *_failed_keys.json)"
    )
    parser.add_argument("--input", default=DEFAULT_INPUT_PATH,
                         help="Path to input Excel (needs a 'jira_key' column) OR a *_failed_keys.json retry list")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Path to write the output Excel")
    parser.add_argument("--workers-per-account", type=int, default=WORKERS_PER_ACCOUNT_DEFAULT,
                         help="Parallel threads per service account (default: 5)")
    args = parser.parse_args()

    if not JIRA_URL:
        print("\n✖ FATAL: JIRA_URL not set in .env")
        sys.exit(1)

    log_path = os.path.splitext(args.output)[0] + "_fetch_log.log"
    setup_result_logger(log_path)

    print("=" * 70)
    print("  FETCH JIRA ISSUE LINKS")
    print("=" * 70)
    print(f"  Target:   {JIRA_URL}")
    print(f"  Input:    {args.input}")
    print(f"  Output:   {args.output}")
    print(f"  Log file: {log_path}   (per-issue status/reason for cross-reference)")

    jira_credentials.print_pool_summary()
    pool_n = jira_credentials.pool_size()
    total_workers = pool_n * args.workers_per_account
    print(f"  Accounts: {pool_n}  |  Workers/account: {args.workers_per_account}  |  Total workers: {total_workers}")
    print("=" * 70)

    jira_keys = load_jira_keys(args.input)
    if not jira_keys:
        print("  No Jira keys to process — exiting.")
        return 0

    all_rows = []
    errors = []
    status_counts = {"OK_WITH_LINKS": 0, "OK_NO_LINKS": 0, "ERROR": 0}
    processed = 0
    total = len(jira_keys)

    def _process(jira_key):
        nonlocal processed
        result = fetch_issue_links(jira_key)

        log_result(
            jira_key, result["status"], result["http_code"],
            len(result["rows"]), result["attempts"], result["elapsed"], result["reason"],
        )

        with results_lock:
            if result["rows"]:
                all_rows.extend(result["rows"])
            if result["status"] == "ERROR":
                errors.append({
                    "jira_key": jira_key,
                    "http_code": result["http_code"],
                    "attempts": result["attempts"],
                    "reason": result["reason"],
                    "account_email": result.get("account_email", ""),
                })
            status_counts[result["status"]] += 1

        with stats_lock:
            processed += 1
            current = processed

        if current % 100 == 0 or current == total:
            thread_safe_print(
                f"  [{current}/{total}] processed  |  "
                f"with_links={status_counts['OK_WITH_LINKS']}  "
                f"no_links={status_counts['OK_NO_LINKS']}  "
                f"errors={status_counts['ERROR']}  |  "
                f"{len(all_rows)} link rows so far"
            )

        if current % CHECKPOINT_EVERY == 0:
            with results_lock:
                save_output(all_rows, args.output)
                save_failure_files(errors, args.output)

    print(f"\n  Fetching links for {total} issue(s)...\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=total_workers) as executor:
        futures = [executor.submit(_process, key) for key in jira_keys]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as e:
                thread_safe_print(f"   ✖ Worker exception: {e}")

    # Final save
    save_output(all_rows, args.output)
    save_failure_files(errors, args.output)  # no-ops silently if errors is empty

    print("\n" + "=" * 70)
    print("  DONE")
    print(f"    Issues processed:      {total}")
    print(f"    OK with links:         {status_counts['OK_WITH_LINKS']}")
    print(f"    OK, genuinely no links: {status_counts['OK_NO_LINKS']}")
    print(f"    Errors:                 {status_counts['ERROR']}")
    print(f"    Link rows written:      {len(all_rows)}")
    print(f"    Per-issue log:          {log_path}")
    if errors:
        base = os.path.splitext(args.output)[0]
        print(f"    Failure detail:         {base}_errors.json")
        print(f"    Retry list:             {base}_failed_keys.json")
    else:
        print("    Failures:               none — no errors.json / failed_keys.json written")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    exit(main())