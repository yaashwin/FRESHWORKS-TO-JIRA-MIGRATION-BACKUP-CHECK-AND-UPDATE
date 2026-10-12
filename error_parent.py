#!/usr/bin/env python3
"""
failed_issues_parent_check.py

For every ISSUE that failed to load into Jira (per errors.json), this script:
  1. Looks up that issue's source_id in the payload file (issues.json) to find
     its source_parent_id / source_epic_id / source_project_key.
  2. Checks whether that parent id was itself successfully created in Jira
     (i.e. exists in id_mapping.json -> "issues").
  3. Writes a full report to Excel (and CSV) with one row per failed issue,
     explaining exactly why it's missing a parent link.

Usage:
    python3 failed_issues_parent_check.py \
        --errors //home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FSdata/mapping/errors.json \
        --id-mapping //home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FSdata/mapping/id_mapping.json \
        --payload //home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FSdata/payload/issues.json \
        --out //home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FSdata/mapping/failed_issues_parent_report.xlsx

All paths have sensible defaults matching your directory layout below, so you
can also just run it with no arguments from that final_draft_code directory.
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("This script needs pandas + openpyxl. Install with:\n"
              "    pip install pandas openpyxl")


DEFAULT_ERRORS = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data/mapping/errors.json"
DEFAULT_ID_MAPPING = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data/mapping/id_mapping_copy_2.json"
DEFAULT_PAYLOAD = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data/payload/issues.json"
DEFAULT_OUT = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data/mapping/failed_issues_parent_report.xlsx"


def load_json(path):
    path = Path(path)
    if not path.exists():
        sys.exit(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_payload_index(payload_data):
    """
    Normalizes issues.json into a dict keyed by source_issue_id (string),
    regardless of whether the file is:
      - a list of issue dicts:            [ {...}, {...} ]
      - a dict keyed by source_issue_id:  { "1952604": {...}, ... }
      - wrapped under a top-level key:    { "issues": [ {...} ] } or
                                           { "issues": { "1952604": {...} } }
    """
    # unwrap a top-level "issues" key if present
    if isinstance(payload_data, dict) and "issues" in payload_data and (
        isinstance(payload_data["issues"], (list, dict))
    ):
        payload_data = payload_data["issues"]

    index = {}

    if isinstance(payload_data, list):
        for rec in payload_data:
            if not isinstance(rec, dict):
                continue
            sid = str(rec.get("source_issue_id") or rec.get("source_id") or "").strip()
            if sid:
                index[sid] = rec

    elif isinstance(payload_data, dict):
        for key, rec in payload_data.items():
            if not isinstance(rec, dict):
                continue
            sid = str(rec.get("source_issue_id") or rec.get("source_id") or key).strip()
            index[sid] = rec

    else:
        sys.exit("Unrecognized structure in issues.json payload file.")

    return index


def build_id_mapping_index(id_mapping_data):
    """
    id_mapping.json -> {"issues": {<key>: {jira_key, jira_id, source_id, issue_type, title, ...}}}

    We key the returned index off the *"source_id" field inside each record*
    (not the outer dict key), since that's the field we're matching
    source_parent_id against. Falls back to the outer key only if a record
    is missing "source_id" for some reason.
    """
    issues = id_mapping_data.get("issues", id_mapping_data)
    index = {}
    for k, rec in issues.items():
        if not isinstance(rec, dict):
            continue
        sid = str(rec.get("source_id") or k).strip()
        index[sid] = rec
    return index


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--errors", default=DEFAULT_ERRORS, help="Path to errors.json")
    ap.add_argument("--id-mapping", default=DEFAULT_ID_MAPPING, help="Path to id_mapping.json")
    ap.add_argument("--payload", default=DEFAULT_PAYLOAD, help="Path to issues.json (payload)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Output .xlsx report path")
    args = ap.parse_args()

    print(f"Loading errors:      {args.errors}")
    errors_data = load_json(args.errors)
    print(f"Loading id_mapping:  {args.id_mapping}")
    id_mapping_data = load_json(args.id_mapping)
    print(f"Loading payload:     {args.payload}")
    payload_data = load_json(args.payload)

    failed_issues = errors_data.get("by_type", {}).get("issue", [])
    print(f"Failed issue entries in errors.json: {len(failed_issues)}")

    payload_index = build_payload_index(payload_data)
    print(f"Payload issues indexed: {len(payload_index)}")

    mapping_index = build_id_mapping_index(id_mapping_data)
    print(f"Successfully mapped issues in id_mapping.json: {len(mapping_index)}")

    rows = []
    for err in failed_issues:
        source_id = str(err.get("source_id", "")).strip()
        error_msg = err.get("error", "")
        timestamp = err.get("timestamp", "")

        payload_rec = payload_index.get(source_id)

        if payload_rec is None:
            rows.append({
                "source_issue_id": source_id,
                "error": error_msg,
                "timestamp": timestamp,
                "found_in_payload": False,
                "source_project_key": "",
                "source_parent_id": "",
                "source_epic_id": "",
                "parent_id_used": "",
                "parent_found_in_id_mapping": "",
                "parent_jira_key": "",
                "parent_jira_id": "",
                "parent_issue_type": "",
                "root_cause": "source_issue_id not found in issues.json payload file",
            })
            continue

        parent_id = str(payload_rec.get("source_parent_id") or "").strip()
        epic_id = str(payload_rec.get("source_epic_id") or "").strip()
        project_key = payload_rec.get("source_project_key", "")

        # For sub-tasks, the relevant "parent" is source_parent_id.
        # If that's blank, fall back to source_epic_id (in case the failure
        # is epic-link related rather than sub-task related).
        parent_to_check = parent_id or epic_id

        parent_mapping = mapping_index.get(parent_to_check) if parent_to_check else None

        if not parent_to_check:
            root_cause = "No source_parent_id or source_epic_id in payload — parent link missing at source"
            parent_found = ""
        elif parent_mapping is not None:
            root_cause = "Parent WAS created in Jira — issue itself likely failed for a different/unrelated reason"
            parent_found = True
        elif parent_to_check in [str(e.get("source_id")) for e in failed_issues]:
            root_cause = "Parent also FAILED to load — this is a cascading sub-task/parent failure"
            parent_found = False
        else:
            root_cause = "Parent not found in id_mapping AND not in errors.json — parent likely never attempted (skipped/missing in extraction)"
            parent_found = False

        rows.append({
            "source_issue_id": source_id,
            "error": error_msg,
            "timestamp": timestamp,
            "found_in_payload": True,
            "source_project_key": project_key,
            "source_parent_id": parent_id,
            "source_epic_id": epic_id,
            "parent_id_used": parent_to_check,
            "parent_found_in_id_mapping": parent_found,
            "parent_jira_key": parent_mapping.get("jira_key", "") if parent_mapping else "",
            "parent_jira_id": parent_mapping.get("jira_id", "") if parent_mapping else "",
            "parent_issue_type": parent_mapping.get("issue_type", "") if parent_mapping else "",
            "root_cause": root_cause,
        })

    df = pd.DataFrame(rows)

    # Summary by root cause
    summary = df["root_cause"].value_counts().reset_index()
    summary.columns = ["Root Cause", "Count"]
    summary.loc[len(summary)] = ["TOTAL", summary["Count"].sum()]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        df.to_excel(writer, sheet_name="Failed Issues - Parent Check", index=False)

    # Light formatting pass
    from openpyxl import load_workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = load_workbook(out_path)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(name="Arial", bold=True, color="FFFFFF")

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        for row in ws.iter_rows():
            for cell in row:
                cell.font = Font(name="Arial")
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.freeze_panes = "A2"
        for col_cells in ws.columns:
            length = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells)
            col_letter = get_column_letter(col_cells[0].column)
            ws.column_dimensions[col_letter].width = min(max(length + 2, 12), 60)

    wb.save(out_path)

    # Also drop a CSV next to it for quick grepping
    csv_path = out_path.with_suffix(".csv")
    df.to_csv(csv_path, index=False)

    print("\n=== Summary ===")
    print(summary.to_string(index=False))
    print(f"\nReport written to: {out_path}")
    print(f"CSV also written to: {csv_path}")


if __name__ == "__main__":
    main()