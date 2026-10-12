import os
import sys
import json
import argparse

import requests
from dotenv import load_dotenv

load_dotenv(override=True)

# =============================================================
# CONFIG — Jira
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

TARGET_FIELD     = "customfield_13034"   # "Default FR Field" (ADF paragraph field)
BACKEND_DOC_KEY  = "cf_i2p_document"     # label prefix used when it was appended
LABEL_PREFIX     = f"{BACKEND_DOC_KEY} : "

# =============================================================
# STATIC LIST OF JIRA KEYS
# =============================================================
# Paste your 100+ Jira keys here (one per string, comma-separated).
# These are used automatically if you don't pass any keys on the
# command line and don't use --keys-file. You can still combine this
# with CLI args / --keys-file — all sources are merged, duplicates removed.
STATIC_JIRA_KEYS = [
"PLG-2798" 
  ]


# =============================================================
# Jira helpers (GET / PUT) — self-contained, no external module
# =============================================================
def jira_get(path):
    url = f"{JIRA_BASE_URL}/rest/{path}"
    resp = JIRA_SESSION.get(url, timeout=30)
    data = resp.json() if resp.content else None
    return data, resp.status_code


def jira_put(path, payload):
    url = f"{JIRA_BASE_URL}/rest/{path}"
    resp = JIRA_SESSION.put(url, json=payload, params={"notifyUsers": "false"}, timeout=30)
    data = resp.json() if resp.content else None
    return data, resp.status_code


# =============================================================
# STEP 1 — Jira: get current ADF value of customfield_13034
# =============================================================
def get_current_adf(jira_key):
    data, status_code = jira_get(f"api/3/issue/{jira_key}?fields={TARGET_FIELD}")
    if status_code != 200 or not data:
        raise RuntimeError(f"[{jira_key}] Failed to fetch {TARGET_FIELD} (status {status_code}): {data}")

    current = data.get("fields", {}).get(TARGET_FIELD)
    if not current:
        current = {"type": "doc", "version": 1, "content": []}
    return current


# =============================================================
# STEP 2 — find & strip out the paragraph(s) that start with
#          "cf_i2p_document : " (label included)
# =============================================================
def _paragraph_is_i2p_document(node):
    if node.get("type") != "paragraph":
        return False
    content = node.get("content") or []
    if not content:
        return False
    first = content[0]
    if first.get("type") != "text":
        return False
    return first.get("text", "").startswith(LABEL_PREFIX)


def remove_i2p_document_paragraphs(existing_doc):
    original_content = existing_doc.get("content", [])
    kept = [n for n in original_content if not _paragraph_is_i2p_document(n)]
    removed = [n for n in original_content if _paragraph_is_i2p_document(n)]

    cleaned = dict(existing_doc)  # shallow copy — don't mutate caller's dict
    cleaned["content"] = kept
    return cleaned, removed


def _paragraph_plain_text(node):
    """Flatten a paragraph node's text runs into one plain-text line for display."""
    parts = []
    for child in node.get("content", []):
        if child.get("type") == "text":
            parts.append(child.get("text", ""))
        elif child.get("type") == "hardBreak":
            parts.append("\n")
    return "".join(parts)


# =============================================================
# STEP 3 — Jira: PUT the cleaned ADF doc back
# =============================================================
def update_jira_field(jira_key, cleaned_adf_doc, removed_nodes=None, dry_run=False):
    payload = {
        "fields": {
            TARGET_FIELD: cleaned_adf_doc,
        }
    }

    if dry_run:
        removed_nodes = removed_nodes or []

        print(f"  [DRY RUN] Paragraph(s) that would be removed:")
        for i, node in enumerate(removed_nodes, 1):
            print(f"    {i}. {_paragraph_plain_text(node)}")

        print(f"  [DRY RUN] Final {TARGET_FIELD} content after removal:")
        print(json.dumps(cleaned_adf_doc, indent=2))

        preview_path = f"/tmp/{jira_key}_dry_run_payload.json"
        with open(preview_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"  [DRY RUN] Full PUT payload written to {preview_path} "
              f"({len(json.dumps(payload))} chars total)")
        print(f"  [DRY RUN] Would PUT with query param notifyUsers=false "
              f"(no email/notifications will be sent to watchers or assignees)")
        return True

    data, status_code = jira_put(f"api/3/issue/{jira_key}", payload)

    # Jira's PUT /issue endpoint normally returns 204 No Content on success
    if status_code not in (200, 204):
        raise RuntimeError(f"[{jira_key}] PUT failed (status {status_code}): {data}")

    return True


# =============================================================
# MAIN — one Jira key end-to-end
# =============================================================
def process_key(jira_key, dry_run=False):
    print(f"\n--- {jira_key} ---")

    current_adf = get_current_adf(jira_key)
    existing_block_count = len(current_adf.get("content", []))

    cleaned_adf, removed_nodes = remove_i2p_document_paragraphs(current_adf)
    removed_count = len(removed_nodes)

    if removed_count == 0:
        print(f"  (no '{LABEL_PREFIX}' paragraph found — nothing to remove, "
              f"{existing_block_count} block(s) unchanged)")
        return

    update_jira_field(jira_key, cleaned_adf, removed_nodes=removed_nodes, dry_run=dry_run)

    print(
        f"  ✔ {'[DRY RUN] Would remove' if dry_run else 'Removed'} "
        f"{removed_count} '{LABEL_PREFIX}' paragraph(s) from {TARGET_FIELD} "
        f"({existing_block_count} -> {len(cleaned_adf['content'])} block(s))"
    )


def main():
    parser = argparse.ArgumentParser(
        description=f"Remove the '{LABEL_PREFIX}' paragraph previously appended to "
                    f"Jira customfield_13034, leaving everything else untouched"
    )
    parser.add_argument("keys", nargs="*", help="Jira issue keys, e.g. PLG-4129 PLG-4130")
    parser.add_argument("--keys-file", default=None, help="Path to a text file with one Jira key per line")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually PUT — just show what would happen")
    args = parser.parse_args()

    jira_keys = list(args.keys)
    if args.keys_file:
        with open(args.keys_file, "r", encoding="utf-8") as f:
            jira_keys.extend(line.strip() for line in f if line.strip())

    # Fall back to the hardcoded STATIC_JIRA_KEYS list if nothing was
    # passed on the command line / via --keys-file. If CLI args or a
    # keys-file WERE given, they're merged with STATIC_JIRA_KEYS too,
    # with duplicates removed, so you can freely combine both.
    jira_keys = list(dict.fromkeys(jira_keys + STATIC_JIRA_KEYS))

    if not jira_keys:
        print("No Jira keys given. Pass them as arguments, via --keys-file, "
              "or paste them into STATIC_JIRA_KEYS at the top of the script.")
        return 1

    if not JIRA_BASE_URL or not ((JIRA_EMAIL and JIRA_API_TOKEN) or JIRA_PAT):
        print("ERROR: Jira credentials missing. Set JIRA_BASE_URL and either "
              "(JIRA_EMAIL + JIRA_API_TOKEN) or JIRA_PAT in .env")
        return 1

    failures = []
    for jira_key in jira_keys:
        try:
            process_key(jira_key, dry_run=args.dry_run)
        except Exception as e:
            print(f"  ✖ {jira_key} FAILED: {e}")
            failures.append(jira_key)

    print("\n" + "=" * 60)
    print(f"Done. {len(jira_keys) - len(failures)}/{len(jira_keys)} succeeded.")
    if failures:
        print(f"Failed: {failures}")
    print("=" * 60)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())