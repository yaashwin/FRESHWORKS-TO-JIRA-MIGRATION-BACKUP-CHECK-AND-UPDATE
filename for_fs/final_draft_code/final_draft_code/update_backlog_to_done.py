"""
update_backlog_to_done.py

Fixes issues that were migrated into Jira as "Backlog" when their real final
status (in the source / payload) was "Done".

You give the Jira issue keys as a STATIC list (ISSUE_KEYS below). For each key:

    STEP 1  Open the issue in Jira and read its CURRENT status.
    STEP 2  Is the current status "Backlog"?
                no  -> skip, go to the next issue
                yes -> continue
    STEP 3  Confirm the migration really got the status wrong:
              the issue's LAST / FINAL status in the payload (issues.json,
              field "jira_status") must be "Done".
              (Optional, --check-changelog: also confirm nobody has changed
               the status in Jira since creation, i.e. it is still in Backlog
               only because the migration never applied the status.)
                no  -> skip, go to the next issue
                yes -> continue
    STEP 4  Transition the issue Backlog -> Done, then re-read the status to
            confirm it changed.

SAFETY: the script is a DRY RUN by default — it does all the checks and writes
the report, but changes NOTHING in Jira. Add --apply to really transition.

Jira calls (GET / POST) use the SAME credentials as your PUT script — the ones in
.env (JIRA_BASE_URL + JIRA_EMAIL/JIRA_API_TOKEN, or JIRA_PAT) — NOT the accounts
inside bulk_load_to_jira. Every transition is sent with notifyUsers=false.

Payload lookup (step 3) only READS data through bulk_load_to_jira (no Jira calls):
    jira_key -> source_id   via core.id_mapping["issues"]
    source_id -> record     via payload/issues.json ("source_issue_id")

Report (xlsx + json) is written to:
    {project_data_dir}/{project_key}_issue_status/status_update/
        status_update_report[_dry_run].xlsx / .json

Usage:
    python update_backlog_to_done.py                    # dry run
    python update_backlog_to_done.py --apply            # really change statuses
    python update_backlog_to_done.py --apply --check-changelog
"""

import os
import sys
import json
import time
import argparse
from collections import Counter

import requests
from dotenv import load_dotenv
import pandas as pd

import bulk_load_to_jira as core  # used ONLY to read id_mapping / MAPPING_DIR (no Jira calls through it)

load_dotenv(override=True)

# =============================================================
# CONFIG — Jira (same credentials as the PUT script)
# =============================================================
JIRA_BASE_URL  = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PAT       = os.getenv("JIRA_PAT")

JIRA_SESSION = requests.Session()
if JIRA_EMAIL and JIRA_API_TOKEN:
    JIRA_SESSION.auth = (JIRA_EMAIL, JIRA_API_TOKEN)   # Basic auth (Jira Cloud)
elif JIRA_PAT:
    JIRA_SESSION.headers.update({"Authorization": f"Bearer {JIRA_PAT}"})
JIRA_SESSION.headers.update({"Content-Type": "application/json"})

# ---------------------------------------------------------------------------
# PUT YOUR ISSUE KEYS HERE
# ---------------------------------------------------------------------------
ISSUE_KEYS = [
    # "ABC-101",
    # "ABC-102",
    # "ABC-103",
]

CURRENT_STATUS = "Backlog"            # step 2: only touch issues currently in this status
PAYLOAD_REQUIRED_STATUS = "Done"      # step 3: payload's final status must be this
TARGET_STATUS = "Done"                # step 4: transition the issue to this status

# Used only with --check-changelog: status changes made by these accounts are
# treated as "the migration", every other author is treated as "a user".
MIGRATION_ACCOUNT_IDS = [
    "712020:d01481c0-4b37-4216-8059-c31531b8ec4e",
    "712020:3782473e-fdca-40d2-822c-306a0cd8054f",
]


# ---------------------------------------------------------------------------
# Path helpers (same conventions as check_issue_status.py)
# ---------------------------------------------------------------------------
def load_json_file(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_issues_path(explicit_path=None):
    if explicit_path:
        return explicit_path
    for attr in ("ISSUES_JSON_PATH", "PAYLOAD_ISSUES_PATH"):
        candidate = getattr(core, attr, None)
        if candidate:
            return candidate
    payload_dir = getattr(core, "PAYLOAD_DIR", None)
    if payload_dir:
        return os.path.join(payload_dir, "issues.json")
    project_dir = os.path.dirname(os.path.normpath(core.MAPPING_DIR))
    return os.path.join(project_dir, "payload", "issues.json")


def resolve_project_key():
    for attr in ("PROJECT_KEY", "JIRA_PROJECT_KEY", "TARGET_PROJECT_KEY", "project_key"):
        val = getattr(core, attr, None)
        if val and isinstance(val, str):
            return val
    project_dir_name = os.path.basename(os.path.dirname(os.path.normpath(core.MAPPING_DIR)))
    if project_dir_name.endswith("_data"):
        return project_dir_name[: -len("_data")]
    return project_dir_name


def resolve_output_dir(explicit_path=None):
    if explicit_path:
        return explicit_path
    project_dir = os.path.dirname(os.path.normpath(core.MAPPING_DIR))
    return os.path.join(project_dir, f"{resolve_project_key()}_issue_status", "status_update")


# ---------------------------------------------------------------------------
# Lookups (only for the keys we were given, so memory stays small)
# ---------------------------------------------------------------------------
def build_key_to_source(keys):
    """jira_key -> source_id, restricted to the given keys."""
    wanted = set(keys)
    out = {}
    for source_id, info in core.id_mapping.get("issues", {}).items():
        jira_key = (info or {}).get("jira_key")
        if jira_key in wanted:
            out[jira_key] = source_id
    return out


def build_payload_lookup(issues_path, wanted_source_ids):
    """source_issue_id (str) -> record from issues.json, restricted to wanted ids."""
    wanted = {str(s) for s in wanted_source_ids}
    lookup = {}
    for rec in load_json_file(issues_path):
        sid = str(rec.get("source_issue_id"))
        if sid in wanted:
            lookup[sid] = rec
    print(f"  ✔ Found {len(lookup)}/{len(wanted)} payload record(s) in {issues_path}")
    return lookup


# ---------------------------------------------------------------------------
# Jira helpers
# ---------------------------------------------------------------------------
def get_current_status(key):
    """Returns (status_name or None, http_status)."""
    data, code = jira_get(f"api/3/issue/{key}?fields=status")
    if code != 200 or not data:
        return None, code
    return (data.get("fields", {}).get("status") or {}).get("name", "") or "", code


def find_user_status_changes(key, migration_ids):
    """
    Status changes in the issue history made by anyone NOT in migration_ids.
    Returns a list of "Author: From -> To" strings, or None if history couldn't be read.
    """
    changes = []
    start = 0
    while True:
        data, code = jira_get(f"api/3/issue/{key}/changelog?startAt={start}&maxResults=100")
        if code != 200 or not data:
            return None
        values = data.get("values", []) or []
        for hist in values:
            author = hist.get("author") or {}
            if author.get("accountId") in migration_ids:
                continue
            for item in hist.get("items", []) or []:
                if item.get("field") == "status":
                    changes.append(
                        f"{author.get('displayName', 'unknown')}: "
                        f"{item.get('fromString')} -> {item.get('toString')}"
                    )
        if not values or data.get("isLast", True):
            break
        start += len(values)
    return changes


def _request(method, path, **kwargs):
    """Sends the request with the session above; retries a few times on HTTP 429."""
    url = f"{JIRA_BASE_URL}/rest/{path}"
    for _ in range(4):
        resp = JIRA_SESSION.request(method, url, timeout=30, **kwargs)
        if resp.status_code != 429:
            break
        time.sleep(int(resp.headers.get("Retry-After", 5)))
    try:
        data = resp.json() if resp.content else None
    except ValueError:
        data = resp.text
    return data, resp.status_code


def jira_get(path):
    return _request("GET", path)


def jira_post(path, payload):
    # notifyUsers=false, same as the PUT script. Jira documents this parameter for
    # "Edit issue"; it may be ignored on transitions — test with a few keys first.
    return _request("POST", path, json=payload, params={"notifyUsers": "false"})


def build_transition_body(key):
    """
    Finds the transition that leads to TARGET_STATUS for this issue.
    Returns (body, error). body is the POST payload, error is a string if it can't be done.
    """
    data, code = jira_get(f"api/3/issue/{key}/transitions?expand=transitions.fields")
    if code != 200 or not data:
        return None, f"could not read transitions (HTTP {code})"

    transitions = data.get("transitions", []) or []
    match = next(
        (t for t in transitions
         if ((t.get("to") or {}).get("name", "") or "").strip().lower() == TARGET_STATUS.lower()),
        None,
    )
    if not match:
        available = ", ".join(
            f"{t.get('name')} -> {(t.get('to') or {}).get('name')}" for t in transitions
        ) or "none"
        return None, f"no direct transition to '{TARGET_STATUS}' (available: {available})"

    body = {"transition": {"id": match["id"]}}
    fields_meta = match.get("fields") or {}

    # Resolution is commonly required when moving to Done
    res = fields_meta.get("resolution")
    if res and res.get("required"):
        allowed = [a.get("name") for a in res.get("allowedValues", []) or []]
        pick = next((a for a in allowed if a and a.lower() == "done"), None)
        if not pick:
            return None, f"transition requires a resolution but no 'Done' option (allowed: {allowed})"
        body["fields"] = {"resolution": {"name": pick}}

    # Any other required field with no default can't be guessed
    missing = [
        name for name, meta in fields_meta.items()
        if name != "resolution" and meta.get("required") and not meta.get("hasDefaultValue")
    ]
    if missing:
        return None, f"transition requires field(s) this script can't fill: {', '.join(missing)}"

    return body, None


# ---------------------------------------------------------------------------
# Per-issue logic
# ---------------------------------------------------------------------------
def process_issue(key, key_to_source, payload_lookup, apply, check_changelog):
    row = {
        "jira_key": key, "source_id": "", "status_before": "",
        "payload_status": "", "action": "", "reason": "", "status_after": "",
    }

    # STEP 1: open the issue
    current, code = get_current_status(key)
    if current is None:
        row.update(action="SKIPPED", reason=f"could not read issue (HTTP {code})")
        return row
    row["status_before"] = current
    row["status_after"] = current

    # STEP 2: must be Backlog
    if current.strip().lower() != CURRENT_STATUS.lower():
        row.update(action="SKIPPED", reason=f"current status is '{current}', not '{CURRENT_STATUS}'")
        return row

    # STEP 3: payload's final status must be Done
    source_id = key_to_source.get(key)
    if not source_id:
        row.update(action="SKIPPED", reason="key not found in id_mapping — can't confirm payload status")
        return row
    row["source_id"] = source_id

    rec = payload_lookup.get(str(source_id))
    if not rec:
        row.update(action="SKIPPED", reason="no record in issues.json — can't confirm payload status")
        return row

    payload_status = rec.get("jira_status", "") or ""
    row["payload_status"] = payload_status
    if payload_status.strip().lower() != PAYLOAD_REQUIRED_STATUS.lower():
        row.update(action="SKIPPED",
                   reason=f"payload status is '{payload_status}', not '{PAYLOAD_REQUIRED_STATUS}'")
        return row

    if check_changelog:
        user_changes = find_user_status_changes(key, set(MIGRATION_ACCOUNT_IDS))
        if user_changes is None:
            row.update(action="SKIPPED", reason="could not read issue history")
            return row
        if user_changes:
            row.update(action="SKIPPED",
                       reason="status was changed by a user in Jira: " + "; ".join(user_changes[:3]))
            return row

    # STEP 4: Backlog -> Done
    body, err = build_transition_body(key)
    if err:
        row.update(action="FAILED", reason=err)
        return row

    if not apply:
        row.update(action="WOULD_TRANSITION",
                   reason=f"dry run — would move '{CURRENT_STATUS}' -> '{TARGET_STATUS}'")
        return row

    _, post_code = jira_post(f"api/3/issue/{key}/transitions", body)
    if post_code not in (200, 204):
        row.update(action="FAILED", reason=f"transition request failed (HTTP {post_code})")
        return row

    after, _ = get_current_status(key)
    row["status_after"] = after if after is not None else "UNKNOWN"
    if after is not None and after.strip().lower() == TARGET_STATUS.lower():
        row.update(action="TRANSITIONED", reason=f"'{CURRENT_STATUS}' -> '{TARGET_STATUS}'")
    else:
        row.update(action="FAILED", reason=f"transition sent but status is now '{after}'")
    return row


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def save_report(results, out_dir, apply):
    os.makedirs(out_dir, exist_ok=True)
    suffix = "" if apply else "_dry_run"

    json_path = os.path.join(out_dir, f"status_update_report{suffix}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\n   ✔ JSON report saved -> {json_path}")

    rows = [{
        "Jira Key": r["jira_key"],
        "Source ID": r["source_id"],
        "Status Before": r["status_before"],
        "Payload Status": r["payload_status"],
        "Action": r["action"],
        "Reason": r["reason"],
        "Status After": r["status_after"],
    } for r in results]

    xlsx_path = os.path.join(out_dir, f"status_update_report{suffix}.xlsx")
    pd.DataFrame(rows).to_excel(xlsx_path, index=False)
    print(f"   ✔ Excel report saved -> {xlsx_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Move wrongly-migrated Backlog issues (payload status Done) to Done, for a static list of keys"
    )
    parser.add_argument("--apply", action="store_true",
                         help="Actually transition issues in Jira. Without this flag it is a DRY RUN.")
    parser.add_argument("--check-changelog", action="store_true",
                         help="Also skip issues whose status was changed by a non-migration user in Jira.")
    parser.add_argument("--issues-path", default=None, help="Override path to issues.json")
    parser.add_argument("--output-dir", default=None,
                         help="Override output folder (default: {project_data_dir}/{project_key}_issue_status/status_update)")
    args = parser.parse_args()

    # de-duplicate, keep order
    keys = list(dict.fromkeys(k.strip().upper() for k in ISSUE_KEYS if k and k.strip()))
    if not keys:
        print("ISSUE_KEYS is empty — add your Jira issue keys at the top of this script.")
        return 1

    issues_path = resolve_issues_path(args.issues_path)
    out_dir = resolve_output_dir(args.output_dir)

    print("=" * 70)
    print(f"  UPDATE {CURRENT_STATUS.upper()} -> {TARGET_STATUS.upper()}  "
          f"({'APPLY — WILL CHANGE JIRA' if args.apply else 'DRY RUN — no changes'})")
    print("=" * 70)
    print(f"  Issue keys:      {len(keys)}")
    print(f"  issues.json:     {issues_path}")
    print(f"  Output dir:      {out_dir}")
    print(f"  Check changelog: {'yes' if args.check_changelog else 'no'}")
    print("  Notifications:   notifyUsers=false sent with every transition")
    print("=" * 70)

    if not JIRA_BASE_URL or not ((JIRA_EMAIL and JIRA_API_TOKEN) or JIRA_PAT):
        print("ERROR: Jira credentials missing. Set JIRA_BASE_URL and either "
              "(JIRA_EMAIL + JIRA_API_TOKEN) or JIRA_PAT in .env")
        return 1

    me, me_code = jira_get("api/3/myself")
    if me_code != 200 or not me:
        print(f"ERROR: could not authenticate to Jira (HTTP {me_code}): {me}")
        return 1
    print(f"  ✔ Authenticated as: {me.get('displayName')} ({me.get('emailAddress', me.get('accountId'))})")

    key_to_source = build_key_to_source(keys)
    print(f"  ✔ {len(key_to_source)}/{len(keys)} key(s) found in id_mapping")
    payload_lookup = build_payload_lookup(issues_path, key_to_source.values())

    results = []
    for i, key in enumerate(keys, start=1):
        try:
            row = process_issue(key, key_to_source, payload_lookup, args.apply, args.check_changelog)
        except Exception as e:
            row = {"jira_key": key, "source_id": "", "status_before": "", "payload_status": "",
                   "action": "FAILED", "reason": f"unexpected error: {e}", "status_after": ""}
        results.append(row)
        print(f"  [{i}/{len(keys)}] {key}: {row['action']} — {row['reason']}")
    save_report(results, out_dir, args.apply)

    print("\n  Summary:")
    for action, count in Counter(r["action"] for r in results).most_common():
        print(f"    {action:18s}: {count}")

    print("\n" + "=" * 70)
    print("  DONE")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())