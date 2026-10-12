#!/usr/bin/env python3
"""
payload_vs_id_mapping_check.py

Cross-checks EVERY issue in the payload file (issues.json) against
id_mapping.json to see whether it was successfully created in Jira or not.

For each record in issues.json:
    "source_issue_id": "1952604",
    "source_project_key": "CXBU",
    ...

...it looks up "source_issue_id" against the "source_id" field of every
record inside id_mapping.json["issues"], e.g.:
    "1440472": {
        "jira_key": "CX-152577",
        "jira_id": "441728",
        "source_id": "1440472",
        ...
    }

and reports, for every payload issue: whether it was mapped, and if so its
Jira key/id/type/title/created_at.

This script is READ-ONLY on issues.json and id_mapping.json. It only ever
writes the new output report file(s) you point --out at.

Usage:
    python3 payload_vs_id_mapping_check.py \
        --payload //home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FSdata/payload/issues.json \
        --id-mapping //home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FSdata/mapping/id_mapping.json \
        --out //home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FSdata/mapping/payload_vs_id_mapping_report.xlsx

Defaults below match your usual directory layout, so it also runs with no
arguments.
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("This script needs pandas + openpyxl. Install with:\n"
              "    pip install pandas openpyxl")

# Control characters (other than tab/newline/CR) that openpyxl/Excel refuse
# to store in a cell. Source data sometimes contains these (e.g. pasted from
# terminals/logs); strip them so the report doesn't crash on write.
_ILLEGAL_CHARS_RE = re.compile(
    r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]"
)


def clean_str(value):
    if isinstance(value, str):
        return _ILLEGAL_CHARS_RE.sub("", value)
    return value


DEFAULT_PAYLOAD = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data/payload/issues.json"
DEFAULT_ID_MAPPING = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data/mapping/id_mapping_copy_2.json"
DEFAULT_OUT = "/home/sudharshnalakshmi.s/fs_code/for_fs/final_draft_code/final_draft_code/FS_data/mapping/payload_vs_id_mapping_report.xlsx"


def load_json(path):
    path = Path(path)
    if not path.exists():
        sys.exit(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def iter_payload_records(payload_data):
    """
    Yields (source_issue_id, record_dict) for every issue in issues.json,
    regardless of whether the file is:
      - a list of dicts:               [ {...}, {...} ]
      - a dict keyed by source id:      { "1952604": {...}, ... }
      - wrapped in a top-level key:     { "issues": [ {...} ] } or
                                         { "issues": { "1952604": {...} } }
    """
    if isinstance(payload_data, dict) and "issues" in payload_data and isinstance(
        payload_data["issues"], (list, dict)
    ):
        payload_data = payload_data["issues"]

    if isinstance(payload_data, list):
        for rec in payload_data:
            if not isinstance(rec, dict):
                continue
            sid = str(rec.get("source_issue_id") or rec.get("source_id") or "").strip()
            if sid:
                yield sid, rec

    elif isinstance(payload_data, dict):
        for key, rec in payload_data.items():
            if not isinstance(rec, dict):
                continue
            sid = str(rec.get("source_issue_id") or rec.get("source_id") or key).strip()
            yield sid, rec

    else:
        sys.exit("Unrecognized structure in issues.json payload file.")


def build_id_mapping_index(id_mapping_data):
    """
    Returns dict keyed by the *"source_id" field value* inside each
    id_mapping.json["issues"] record (falls back to the outer dict key only
    if "source_id" is missing).
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
    ap.add_argument("--payload", default=DEFAULT_PAYLOAD, help="Path to issues.json (payload)")
    ap.add_argument("--id-mapping", default=DEFAULT_ID_MAPPING, help="Path to id_mapping.json")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Output .xlsx report path")
    args = ap.parse_args()

    print(f"Loading payload:    {args.payload}")
    payload_data = load_json(args.payload)
    print(f"Loading id_mapping: {args.id_mapping}")
    id_mapping_data = load_json(args.id_mapping)

    mapping_index = build_id_mapping_index(id_mapping_data)
    print(f"Issues in id_mapping.json: {len(mapping_index)}")

    rows = []
    seen = set()
    dupes = 0

    for source_issue_id, rec in iter_payload_records(payload_data):
        if source_issue_id in seen:
            dupes += 1
        seen.add(source_issue_id)

        mapped = mapping_index.get(source_issue_id)

        rows.append({
            "source_issue_id": source_issue_id,
            "source_project_key": rec.get("source_project_key", ""),
            "source_parent_id": rec.get("source_parent_id", ""),
            "source_epic_id": rec.get("source_epic_id", ""),
            "mapped_to_jira": bool(mapped),
            "jira_key": mapped.get("jira_key", "") if mapped else "",
            "jira_id": mapped.get("jira_id", "") if mapped else "",
            "issue_type": mapped.get("issue_type", "") if mapped else "",
            "title": mapped.get("title", "") if mapped else "",
            "created_at": mapped.get("created_at", "") if mapped else "",
        })

    df = pd.DataFrame(rows)
    # Strip any characters Excel/openpyxl can't store (control chars etc.).
    # Applied to every column unconditionally: clean_str() is a no-op on
    # non-strings, and newer pandas can report string columns as dtype
    # "str" rather than "object", so a dtype filter here would miss them.
    for col in df.columns:
        df[col] = df[col].map(clean_str)

    total_payload = len(df)
    total_mapped = int(df["mapped_to_jira"].sum())
    total_unmapped = total_payload - total_mapped

    summary = pd.DataFrame([
        {"Metric": "Total issues in payload (issues.json)", "Count": total_payload},
        {"Metric": "Duplicate source_issue_id entries in payload", "Count": dupes},
        {"Metric": "Mapped to Jira (found in id_mapping.json)", "Count": total_mapped},
        {"Metric": "NOT mapped to Jira (missing from id_mapping.json)", "Count": total_unmapped},
        {"Metric": "Total issues in id_mapping.json", "Count": len(mapping_index)},
    ])

    # breakdown of mapped issues by issue_type, for context
    if total_mapped:
        type_breakdown = (
            df[df["mapped_to_jira"]]["issue_type"]
            .value_counts()
            .reset_index()
        )
        type_breakdown.columns = ["Issue Type (mapped)", "Count"]
    else:
        type_breakdown = pd.DataFrame(columns=["Issue Type (mapped)", "Count"])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        type_breakdown.to_excel(writer, sheet_name="Summary", index=False, startcol=4)
        df.to_excel(writer, sheet_name="Payload vs Id Mapping", index=False)
        df[~df["mapped_to_jira"]].drop(columns=["mapped_to_jira"]).to_excel(
            writer, sheet_name="Unmapped Only", index=False
        )

    # formatting pass
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

    csv_path = out_path.with_suffix(".csv")
    df.to_csv(csv_path, index=False)

    print("\n=== Summary ===")
    print(summary.to_string(index=False))
    print(f"\nReport written to: {out_path}")
    print(f"CSV also written to: {csv_path}")


if __name__ == "__main__":
    main()