"""
export_customfield_report.py

Standalone script (no dependency on bulk_load_to_jira.py / upload_attachments.py).

For every project in the TRANSFER_KEY env var, this:
  1. Reads {project}_data/mapping/id_mapping.json  -> the "issues" dict
     (source_id -> {jira_key, jira_id, source_id, issue_type, title, created_at})
  2. Reads {project}_data/payload/issues.json      -> the POST payload that was
     built for each issue (keyed by source_id, or a list -- both are handled)
  3. For every source_id present in id_mapping["issues"], looks up that same
     source_id's payload and pulls out one custom field's value
     (default: customfield_13038)
  4. Writes one Excel file with exactly 3 columns:
        jira_key | issue_type | customfield_13038

Usage:
    # TRANSFER_KEY can be a JSON array or a comma-separated list
    export TRANSFER_KEY='["CX","EX","AB"]'
    python export_customfield_report.py

    # Overrides:
    python export_customfield_report.py --custom-field customfield_13038 \
        --output customfield_report.xlsx --base-dir /path/to/projects

    # Or just pass project keys directly, ignoring the env var:
    python export_customfield_report.py --projects CX EX AB
"""

import argparse
import json
import os
import sys

import pandas as pd
from dotenv import load_dotenv

load_dotenv()


DEFAULT_CUSTOM_FIELD  = "customfield_13038"
DEFAULT_COLUMN_NAME   = "FR_key"
DEFAULT_OUTPUT_FOLDER = "transfer_summary"


# =============================================================================
# HELPERS
# =============================================================================

def parse_transfer_keys(raw: str) -> list[str]:
    """
    TRANSFER_KEY can be given as:
      - a JSON array:            '["CX","EX","AB"]'
      - a comma-separated list:  'CX,EX,AB'
      - a space-separated list:  'CX EX AB'
    Returns a clean list of project keys, de-duplicated, order preserved.
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    keys = []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            keys = [str(k).strip() for k in parsed if str(k).strip()]
        except json.JSONDecodeError:
            keys = []

    if not keys:
        # fall back to comma / whitespace separated
        parts = raw.replace(",", " ").split()
        keys = [p.strip() for p in parts if p.strip()]

    seen = set()
    ordered = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            ordered.append(k)
    return ordered


def load_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_payload_lookup(payload_data) -> dict:
    """
    Normalize payload/issues.json into {source_id: payload_entry}, whatever
    shape it was saved in:
      - dict already keyed by source_id           -> used as-is
      - list of dicts with a source_id-like key    -> re-keyed
    """
    lookup = {}

    if payload_data is None:
        return lookup

    if isinstance(payload_data, dict):
        # Could be {source_id: entry, ...} directly, or wrapped like
        # {"issues": {source_id: entry, ...}} -- handle both.
        if "issues" in payload_data and isinstance(payload_data["issues"], dict):
            payload_data = payload_data["issues"]
        for k, v in payload_data.items():
            lookup[str(k)] = v
        return lookup

    if isinstance(payload_data, list):
        id_keys = ("source_id", "source_issue_id", "id", "issue_id", "issueId")
        for entry in payload_data:
            if not isinstance(entry, dict):
                continue
            sid = None
            for k in id_keys:
                if k in entry and entry[k] not in (None, ""):
                    sid = str(entry[k])
                    break
            if sid:
                lookup[sid] = entry

    return lookup


def find_field_value(obj, field_name: str):
    """
    Recursively search a payload entry for `field_name` (e.g.
    'customfield_13038') and return its value. Handles the common shapes:
      - {"fields": {"customfield_13038": "CH-10811", ...}, ...}
      - {"customfield_13038": "CH-10811", ...}            (flat)
      - anything more deeply nested
    Returns None if not found.
    """
    if isinstance(obj, dict):
        if field_name in obj:
            return obj[field_name]
        for v in obj.values():
            found = find_field_value(v, field_name)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_field_value(item, field_name)
            if found is not None:
                return found
    return None


def process_project(project_key: str, base_dir: str, custom_field: str, column_name: str) -> list[dict]:
    """Returns a list of row-dicts for this project: jira_key / issue_type / <column_name>."""
    rows = []

    id_mapping_path = os.path.join(base_dir, f"{project_key}_data", "mapping", "id_mapping.json")
    payload_path     = os.path.join(base_dir, f"{project_key}_data", "payload", "issues.json")

    id_mapping_data = load_json(id_mapping_path)
    if id_mapping_data is None:
        print(f"  ⚠ [{project_key}] id_mapping.json not found at {id_mapping_path} -- skipping project")
        return rows

    issues_map = id_mapping_data.get("issues", id_mapping_data)  # tolerate a bare {source_id: {...}} file too
    if not isinstance(issues_map, dict):
        print(f"  ⚠ [{project_key}] id_mapping.json has no usable 'issues' dict -- skipping project")
        return rows

    payload_data = load_json(payload_path)
    if payload_data is None:
        print(f"  ⚠ [{project_key}] payload/issues.json not found at {payload_path} "
              f"-- {custom_field} will be blank for every issue in this project")
    payload_lookup = build_payload_lookup(payload_data)

    missing_payload = 0
    missing_field    = 0

    for source_id, entry in issues_map.items():
        if not isinstance(entry, dict):
            continue

        jira_key   = entry.get("jira_key", "")
        issue_type = entry.get("issue_type", "")

        payload_entry = payload_lookup.get(str(source_id))
        if payload_entry is None:
            missing_payload += 1
            field_value = None
        else:
            field_value = find_field_value(payload_entry, custom_field)
            if field_value is None:
                missing_field += 1

        rows.append({
            "jira_key":   jira_key,
            "issue_type": issue_type,
            column_name:  field_value if field_value is not None else "",
        })

    print(f"  ✔ [{project_key}] {len(rows)} issue(s) processed "
          f"({missing_payload} with no payload entry, {missing_field} with payload but no {custom_field})")

    return rows


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Export jira_key/issue_type/custom-field to Excel across all TRANSFER_KEY projects.")
    parser.add_argument("--projects", nargs="+", default=None,
                         help="Project keys to process. If omitted, reads the TRANSFER_KEY env var.")
    parser.add_argument("--custom-field", default=DEFAULT_CUSTOM_FIELD,
                         help=f"Custom field id to extract from each payload (default: {DEFAULT_CUSTOM_FIELD})")
    parser.add_argument("--column-name", default=DEFAULT_COLUMN_NAME,
                         help=f"Header name for that field's column in the Excel output (default: {DEFAULT_COLUMN_NAME})")
    parser.add_argument("--output-folder", default=DEFAULT_OUTPUT_FOLDER,
                         help=f"Folder to write one Excel per project into (default: {DEFAULT_OUTPUT_FOLDER})")
    parser.add_argument("--base-dir", default=".",
                         help="Directory containing the {project}_data folders (default: current directory)")
    args = parser.parse_args()

    if args.projects:
        project_keys = args.projects
    else:
        project_keys = parse_transfer_keys(os.environ.get("TRANSFER_KEY", ""))

    if not project_keys:
        print("✖ No project keys found. Set the TRANSFER_KEY env var "
              "(e.g. TRANSFER_KEY='[\"CX\",\"EX\"]') or pass --projects CX EX ...")
        sys.exit(1)

    print(f"Projects to process: {project_keys}")
    print(f"Custom field:        {args.custom_field}")
    print(f"Base dir:            {os.path.abspath(args.base_dir)}")
    print(f"Output folder:       {os.path.abspath(args.output_folder)}")
    print()

    os.makedirs(args.output_folder, exist_ok=True)

    any_written = False
    for project_key in project_keys:
        rows = process_project(project_key, args.base_dir, args.custom_field, args.column_name)

        if not rows:
            print(f"  ⏭ [{project_key}] no rows -- no Excel written for this project")
            continue

        df = pd.DataFrame(rows, columns=["jira_key", "issue_type", args.column_name])
        out_path = os.path.join(args.output_folder, f"{project_key}_export_summary.xlsx")
        df.to_excel(out_path, index=False)
        any_written = True
        print(f"  ✔ Wrote {len(df)} row(s) to {out_path}")

    if not any_written:
        print("\n✖ No rows collected across any project -- nothing was written.")
        sys.exit(1)

    print(f"\n✔ Done. Check the '{args.output_folder}' folder for one Excel file per project.")


if __name__ == "__main__":
    main()