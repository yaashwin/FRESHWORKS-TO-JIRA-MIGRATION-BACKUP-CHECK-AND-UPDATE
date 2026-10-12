"""
export_versions_from_payload.py

Exports EVERY row in versions.json (the payload directory) to Excel — one
row per (source_release_id, project) pair — instead of reading from
id_mapping like the old export_versions_to_excel() in bulk_load_to_jira.py.

WHY THE OLD EXPORT LOST PLAT/CEMIG ROWS
------------------------------------------
export_versions_to_excel() reads FROM id_mapping["versions"], which
(before fix_multiproject_versions.py) was keyed ONLY by
source_release_id. When the same source_release_id existed for multiple
projects (EX / PLAT / CEMIG / abcdry all needing their own "NA"
version), only the LAST one to overwrite that key survived in
id_mapping — so PLAT/CEMIG rows never showed up in the Excel even
though the version was actually created in Jira for them.

THIS SCRIPT INSTEAD
----------------------
Reads directly from versions.json so no row is ever dropped — every
(source_release_id, project) pair that was actually sent to Jira gets
its own row. For each row it then looks up the Jira Version ID from
id_mapping, trying in order:
  1. composite key "source_release_id::project"   (fix_multiproject_versions.py format)
  2. legacy bare "source_release_id" key            (pre-fix / old runs)
If neither is found, "Jira Version ID" is left blank and the row is
counted in the "unmatched" warning at the end, so you can see exactly
what's still unposted / unmapped.

Output columns:
    Version Name | Source Release ID | Project | Jira Version ID

Usage:
    python export_versions_from_payload.py
"""

import os
import json
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

PROJECT_KEY_ENV = os.getenv("project_key")
DATA_ROOT = f"{PROJECT_KEY_ENV}_data" if PROJECT_KEY_ENV else "data"

PAYLOAD_DIR = f"{DATA_ROOT}/payload"
MAPPING_DIR = f"{DATA_ROOT}/mapping"

VERSIONS_JSON_PATH = os.path.join(PAYLOAD_DIR, "versions.json")
MAPPING_FILE_PATH = os.path.join(MAPPING_DIR, "id_mapping.json")
OUTPUT_XLSX_PATH = os.path.join(MAPPING_DIR, "versions_mapping.xlsx")


def load_json(path):
    if not os.path.exists(path):
        print(f"\u2716 File not found: {path}")
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    versions = load_json(VERSIONS_JSON_PATH)
    if not versions:
        print("  No versions.json data found — nothing to export.")
        return

    id_mapping = load_json(MAPPING_FILE_PATH) or {}
    version_map = id_mapping.get("versions", {}) if isinstance(id_mapping, dict) else {}

    rows = []
    unmatched = 0

    for item in versions:
        source_id = str(item.get("source_release_id", ""))
        payload = item.get("payload", {})
        version_name = payload.get("name", "")
        # project can live on the payload itself, or on the row metadata
        # depending on which stage of prepare_payload.py produced this row
        project = payload.get("project", "") or item.get("jira_project_key", "")

        composite_key = f"{source_id}::{project}"
        mapped = version_map.get(composite_key) or version_map.get(source_id)

        jira_version_id = mapped.get("jira_version_id", "") if mapped else ""
        if not jira_version_id:
            unmatched += 1

        rows.append({
            "Version Name": version_name,
            "Source Release ID": source_id,
            "Project": project,
            "Jira Version ID": jira_version_id,
        })

    df = pd.DataFrame(rows)
    os.makedirs(MAPPING_DIR, exist_ok=True)
    df.to_excel(OUTPUT_XLSX_PATH, index=False)

    print(f"\u2714 Exported {len(rows)} version row(s) -> {OUTPUT_XLSX_PATH}")
    if unmatched:
        print(
            f"\u26a0 {unmatched} row(s) had no matching Jira Version ID in id_mapping "
            f"(not yet posted, or posted under a key format this script didn't find) "
            f"— left blank in the sheet."
        )


if __name__ == "__main__":
    main()