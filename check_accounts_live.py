"""
check_accounts_live.py

Standalone diagnostic — tests every configured Jira service account RIGHT
NOW, live, against both /myself (basic auth check) AND a real issue GET
(permission check on actual data), so you can pinpoint exactly which
account is causing the 401s you're seeing mid-run.

Why this is different from validate_all_accounts() (which already runs at
the start of every script): that only proves an account worked at t=0.
Tokens can get revoked, rotated, or rate-limit-blackholed mid-run. This
re-checks live, and also hits a real issue (not just /myself) since some
setups return 200 on /myself but still 401/403 on specific
project/issue data due to permission schemes.

Usage:
    python check_accounts_live.py FSDRY-75950
    (pass any issue key that's currently failing — defaults to a /myself-only
    check if you don't pass one)
"""

import sys
import requests
import jira_credentials
from dotenv import load_dotenv
import os

load_dotenv()
JIRA_URL = os.getenv("JIRA_URL")

JIRA_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def main():
    test_issue = sys.argv[1] if len(sys.argv) > 1 else None

    accounts = jira_credentials.get_account_pool()
    print(f"Testing {len(accounts)} account(s) against {JIRA_URL}")
    if test_issue:
        print(f"Also testing each against: GET /rest/api/3/issue/{test_issue}\n")
    else:
        print("(pass an issue key as an argument to also test issue-level access)\n")

    any_bad = False

    for i, acc in enumerate(accounts, 1):
        auth = (acc["email"], acc["token"])

        # ── /myself check ──────────────────────────────────────────────
        resp = requests.get(f"{JIRA_URL}/rest/api/3/myself", headers=JIRA_HEADERS, auth=auth, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            print(f"[{i}] {acc['email']}")
            print(f"      /myself: ✔ 200 — {data.get('displayName')} ({data.get('emailAddress')})")
        else:
            any_bad = True
            print(f"[{i}] {acc['email']}")
            print(f"      /myself: ✖ {resp.status_code} — {resp.text[:200]}")
            continue  # no point testing issue access if basic auth already fails

        # ── issue-level check (only meaningful if provided) ─────────────
        if test_issue:
            resp2 = requests.get(
                f"{JIRA_URL}/rest/api/3/issue/{test_issue}?fields=status",
                headers=JIRA_HEADERS, auth=auth, timeout=15,
            )
            if resp2.status_code == 200:
                print(f"      {test_issue}: ✔ 200")
            else:
                any_bad = True
                print(f"      {test_issue}: ✖ {resp2.status_code} — {resp2.text[:300]}")
                print(f"      ↳ this account can authenticate but lacks access to this issue/project")

    print()
    if any_bad:
        print("⚠ At least one account failed — fix its .env entry (JIRA_EMAIL_n / JIRA_API_TOKEN_n)")
        print("  or its permission scheme for the affected project, then re-run your phase.")
    else:
        print("✔ All accounts passed both checks against this issue.")


if __name__ == "__main__":
    main()