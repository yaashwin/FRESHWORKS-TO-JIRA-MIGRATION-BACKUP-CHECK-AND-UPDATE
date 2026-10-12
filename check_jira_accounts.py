"""
check_jira_accounts.py

Standalone diagnostic — hits Jira's /rest/api/3/myself endpoint with
EVERY service account in the jira_credentials.py pool, one at a time,
and reports which accounts are healthy vs. which are returning
401 (bad/expired token) or 403 (forbidden) or anything else unusual.

This does NOT rely on round-robin thread assignment like the main
fetch/load scripts do — it walks the full account list directly via
jira_credentials.get_account_pool(), so every single account gets
checked exactly once, regardless of thread count.

Usage:
    python check_jira_accounts.py
    python check_jira_accounts.py --verbose     # print full response body on failures
"""

import os
import sys
import argparse

import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

import jira_credentials  # same pool module used by the other scripts

load_dotenv()

JIRA_URL = os.getenv("JIRA_URL")
REQUEST_TIMEOUT = 15


def mask_token(token: str) -> str:
    if not token:
        return "****"
    return (token[:4] + "…" + token[-4:]) if len(token) > 10 else "****"


def check_account(account: dict, verbose: bool) -> dict:
    email = account["email"]
    token = account["token"]
    label = account["label"]

    url = f"{JIRA_URL}/rest/api/3/myself"
    auth = HTTPBasicAuth(email, token)

    try:
        resp = requests.get(url, headers={"Accept": "application/json"}, auth=auth, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        return {
            "label": label, "email": email, "status": "NETWORK_ERROR",
            "http_code": None, "detail": f"{type(e).__name__}: {e}",
        }

    if resp.status_code == 200:
        try:
            data = resp.json()
            display_name = data.get("displayName", "")
            account_id = data.get("accountId", "")
        except Exception:
            display_name, account_id = "", ""
        return {
            "label": label, "email": email, "status": "OK",
            "http_code": 200, "detail": f"displayName={display_name!r} accountId={account_id}",
        }

    if resp.status_code == 401:
        detail = resp.text[:300] if verbose else "Unauthorized — bad/expired token"
        return {"label": label, "email": email, "status": "UNAUTHORIZED", "http_code": 401, "detail": detail}

    if resp.status_code == 403:
        detail = resp.text[:300] if verbose else "Forbidden — token valid but account restricted"
        return {"label": label, "email": email, "status": "FORBIDDEN", "http_code": 403, "detail": detail}

    detail = resp.text[:300] if verbose else f"Unexpected HTTP {resp.status_code}"
    return {"label": label, "email": email, "status": "UNEXPECTED", "http_code": resp.status_code, "detail": detail}


def main():
    parser = argparse.ArgumentParser(description="Check every Jira service account in the pool individually")
    parser.add_argument("--verbose", action="store_true", help="Print full response body on failures")
    args = parser.parse_args()

    if not JIRA_URL:
        print("\n✖ FATAL: JIRA_URL not set in .env")
        sys.exit(1)

    accounts = jira_credentials.get_account_pool()
    print("=" * 78)
    print(f"  CHECKING {len(accounts)} JIRA SERVICE ACCOUNT(S) AGAINST {JIRA_URL}")
    print("=" * 78)

    results = []
    for i, acc in enumerate(accounts, 1):
        masked = mask_token(acc["token"])
        print(f"  [{i}/{len(accounts)}] {acc['label']:14s} {acc['email']:40s} token={masked} ... ", end="", flush=True)
        result = check_account(acc, args.verbose)
        results.append(result)
        print(result["status"])

    print("\n" + "=" * 78)
    print("  SUMMARY")
    print("=" * 78)

    ok = [r for r in results if r["status"] == "OK"]
    bad = [r for r in results if r["status"] != "OK"]

    print(f"  Healthy accounts:   {len(ok)}/{len(results)}")
    print(f"  Problem accounts:   {len(bad)}/{len(results)}")

    if bad:
        print("\n  --- PROBLEM ACCOUNTS ---")
        for r in bad:
            print(f"    [{r['label']}] {r['email']}")
            print(f"        http={r['http_code']}  status={r['status']}")
            print(f"        {r['detail']}")
        print("\n  Fix or remove/replace these accounts' tokens in .env, then re-run this")
        print("  script to confirm before re-running the main fetch/load job.")
    else:
        print("\n  All accounts authenticated fine — a 401 during the main run is more")
        print("  likely a token that expired mid-run, or a transient issue, rather than")
        print("  a permanently broken account.")

    print("=" * 78)

    return 1 if bad else 0


if __name__ == "__main__":
    exit(main())