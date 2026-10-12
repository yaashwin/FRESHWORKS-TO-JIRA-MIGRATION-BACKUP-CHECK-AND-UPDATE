import os
import sys
import json
import time
import argparse
import requests
import pandas as pd
from requests.adapters import HTTPAdapter, Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# =============================================================
# .ENV LOADER
# =============================================================
# Loads a .env file (KEY="VALUE" per line) if present, WITHOUT
# overriding anything already exported in the real shell env.
# This means: just having JIRA_URL="..." etc. in a .env file next
# to this script is now enough - you don't have to `export` each
# var by hand every session.
def _load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)

_load_dotenv()

# =============================================================
# CONFIG
# =============================================================
# Data is scoped per-project: {PROJECT_KEY}_data/mapping (excel inputs)
# and {PROJECT_KEY}_data/payload (source-system json exports: issue,
# comment, versions, sprints, links, etc.)
PROJECT_KEY = (os.getenv("PROJECT_KEY") or os.getenv("project_key") or "").strip()
if not PROJECT_KEY:
    print('ERROR: PROJECT_KEY (or project_key) must be set, e.g. project_key="FS"')
    sys.exit(1)

PROJECT_DATA_DIR = f"{PROJECT_KEY}_data"
MAPPING_DIR = os.path.join(PROJECT_DATA_DIR, "mapping")
PAYLOAD_DIR = os.path.join(PROJECT_DATA_DIR, "payload")

EXCEL_PATH = os.path.join(MAPPING_DIR, "test.xlsx")
USER_MAPPING_FILE = os.path.join(MAPPING_DIR, "mappings.xlsx")

JIRA_URL = (os.getenv("JIRA_URL") or "").rstrip("/")

# Auth: Service Account (OAuth) OR API Token
JIRA_ACCESS_TOKEN = os.getenv("JIRA_ACCESS_TOKEN")
JIRA_CLIENT_ID = os.getenv("JIRA_CLIENT_ID")
JIRA_CLIENT_SECRET = os.getenv("JIRA_CLIENT_SECRET")

JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

MAX_WORKERS = 20
REQUEST_DELAY_SEC = 0.05

print_lock = Lock()
cache_lock = Lock()
token_lock = Lock()

version_cache = {}
sprint_cache = {}
oauth_token_cache = {"token": None, "expires_at": 0}

try:
    from mappings import SOURCE_KEY_CUSTOM_FIELD
except Exception:
    SOURCE_KEY_CUSTOM_FIELD = None


# =============================================================
# OAUTH TOKEN
# =============================================================
def get_oauth_token():
    with token_lock:
        if oauth_token_cache["token"] and time.time() < oauth_token_cache["expires_at"]:
            return oauth_token_cache["token"]
        
        if JIRA_ACCESS_TOKEN:
            oauth_token_cache["token"] = JIRA_ACCESS_TOKEN
            oauth_token_cache["expires_at"] = time.time() + 3600
            return JIRA_ACCESS_TOKEN
        
        if not (JIRA_CLIENT_ID and JIRA_CLIENT_SECRET):
            raise ValueError("Need JIRA_ACCESS_TOKEN or (JIRA_CLIENT_ID + JIRA_CLIENT_SECRET)")
        
        resp = requests.post(
            "https://api.atlassian.com/oauth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": JIRA_CLIENT_ID,
                "client_secret": JIRA_CLIENT_SECRET,
                "audience": "api.atlassian.com"
            },
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        expires = data.get("expires_in", 3600)
        oauth_token_cache["token"] = token
        oauth_token_cache["expires_at"] = time.time() + expires - 60
        return token


# =============================================================
# THREAD-LOCAL SESSION
# =============================================================
from threading import local
thread_local = local()

def build_session():
    if not JIRA_URL:
        print("ERROR: JIRA_URL must be set. Example: https://api.atlassian.com/ex/jira/xxxxx")
        sys.exit(1)
    
    session = requests.Session()
    
    if JIRA_ACCESS_TOKEN or JIRA_CLIENT_ID:
        token = get_oauth_token()
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        })
    elif JIRA_EMAIL and JIRA_API_TOKEN:
        session.auth = (JIRA_EMAIL, JIRA_API_TOKEN)
        session.headers.update({"Accept": "application/json"})
    elif JIRA_EMAIL and not JIRA_API_TOKEN:
        print("ERROR: JIRA_EMAIL is set but JIRA_API_TOKEN is empty.")
        print("   Generate one at https://id.atlassian.com/manage-profile/security/api-tokens")
        print('   and set JIRA_API_TOKEN="..." (it cannot be blank).')
        sys.exit(1)
    else:
        print("ERROR: Set JIRA_ACCESS_TOKEN or JIRA_CLIENT_ID+JIRA_CLIENT_SECRET (OAuth)")
        print("   OR set JIRA_EMAIL + JIRA_API_TOKEN (Basic Auth)")
        sys.exit(1)
    
    retries = Retry(total=5, backoff_factor=1.5, status_forcelist=[429,500,502,503,504], allowed_methods=["GET"])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))
    return session

def get_session():
    if not hasattr(thread_local, "session"):
        thread_local.session = build_session()
    return thread_local.session

def jira_get(path, params=None):
    session = get_session()
    if JIRA_ACCESS_TOKEN or JIRA_CLIENT_ID:
        session.headers["Authorization"] = f"Bearer {get_oauth_token()}"
    
    url = f"{JIRA_URL}{path}"
    resp = session.get(url, params=params, timeout=30)
    time.sleep(REQUEST_DELAY_SEC)
    
    if resp.status_code == 404:
        return None
    if resp.status_code == 401:
        raise Exception(f"Auth failed at {url}: {resp.text}")
    resp.raise_for_status()
    return resp.json()

def safe_print(msg):
    with print_lock:
        print(msg)

def extract_project_key(key): 
    return key.split("-")[0] if key and "-" in key else None


# =============================================================
# LOADERS
# =============================================================
def load_json(filename, default=None):
    path = os.path.join(PAYLOAD_DIR, filename)
    if not os.path.exists(path):
        safe_print(f"WARNING: {path} missing")
        return default or []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_excel_rows(path):
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        sys.exit(1)
    df = pd.read_excel(path, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    required = ["Source ID", "Jira Key"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"ERROR: Missing columns: {missing}")
        sys.exit(1)
    
    rows = []
    for _, r in df.iterrows():
        sid = str(r.get("Source ID", "")).strip()
        jkey = str(r.get("Jira Key", "")).strip()
        if not sid or not jkey or sid.lower() == "nan" or jkey.lower() == "nan":
            continue
        pk = extract_project_key(jkey)
        if not pk:
            continue
        rows.append({
            "source_id": sid,
            "jira_key": jkey,
            "project_key": pk,
            "issue_type": str(r.get("Issue Type", "")).strip(),
            "title": str(r.get("Title", "")).strip(),
            "sprint": str(r.get("Sprint", "")).strip(),
        })
    safe_print(f"Loaded {len(rows)} rows from {path}")
    return rows

def load_sprint_board_map():
    if not os.path.exists(USER_MAPPING_FILE):
        return {}
    try:
        df = pd.read_excel(USER_MAPPING_FILE, sheet_name="sprint")
    except:
        return {}
    mapping = {}
    for _, r in df.iterrows():
        k = str(r.get("jira_project_key", "")).strip()
        b = r.get("jira_board_id")
        if k and pd.notna(b):
            try:
                mapping[k] = int(float(b))
            except:
                pass
    return mapping


# =============================================================
# ISSUES
# =============================================================
def fetch_issue(key):
    fields = "summary,issuetype,created"
    if SOURCE_KEY_CUSTOM_FIELD:
        fields += f",{SOURCE_KEY_CUSTOM_FIELD}"
    data = jira_get(f"/rest/api/3/issue/{key}", params={"fields": fields})
    if not data:
        return None
    f = data.get("fields", {})
    return {
        "jira_id": data.get("id"),
        "jira_key": data.get("key"),
        "issue_type": (f.get("issuetype") or {}).get("name", ""),
        "title": f.get("summary", ""),
        "created_at": f.get("created", "")
    }

def process_issue(row, i, total):
    sid = row["source_id"]
    jkey = row["jira_key"]
    try:
        d = fetch_issue(jkey)
        if d is None:
            safe_print(f"  [{i}/{total}] {jkey} -> NOT FOUND")
            return None
        result = {
            "jira_key": d["jira_key"],
            "jira_id": d["jira_id"],
            "source_id": sid,
            "issue_type": d["issue_type"] or row["issue_type"],
            "title": d["title"] or row["title"],
            "created_at": d["created_at"]
        }
        if i % 25 == 0:
            safe_print(f"  [{i}/{total}] resolved...")
        return sid, (result, d["jira_id"])
    except Exception as e:
        safe_print(f"  ERROR {jkey}: {e}")
        return None

def build_issues_mapping(rows):
    safe_print("\nResolving issues...")
    issues = {}
    key_id_map = {}
    total = len(rows)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(process_issue, r, i, total): r for i, r in enumerate(rows, 1)}
        for f in as_completed(futures):
            res = f.result()
            if res:
                sid, (data, jid) = res
                issues[sid] = data
                key_id_map[data["jira_key"]] = jid
    safe_print(f"  Resolved {len(issues)}/{total}")
    return issues, key_id_map


# =============================================================
# COMMENTS
# =============================================================
def fetch_comments(key):
    all_c = []
    start = 0
    while True:
        d = jira_get(f"/rest/api/3/issue/{key}/comment", params={
            "startAt": start, "maxResults": 100, "orderBy": "created"
        })
        if not d: break
        batch = d.get("comments", [])
        all_c.extend(batch)
        total_c = d.get("total", len(all_c))
        start += len(batch)
        if start >= total_c or not batch:
            break
    return all_c

def process_comments(sid, jkey, local_list, i, total):
    try:
        jc = fetch_comments(jkey)
        if len(jc) != len(local_list):
            safe_print(f"  [WARN] {jkey}: local={len(local_list)} jira={len(jc)}")
        out = {}
        for lc, j in zip(local_list, jc):
            cid = lc.get("source_comment_id")
            if cid:
                out[cid] = {"jira_comment_id": j.get("id"), "jira_issue_key": jkey}
        if i % 25 == 0:
            safe_print(f"  [{i}/{total}] comments done...")
        return out
    except Exception as e:
        safe_print(f"  ERROR comments {jkey}: {e}")
        return {}

def build_comments_mapping(rows):
    safe_print("\nResolving comments...")
    source_c = load_json("comments.json", default=[])
    by_issue = {}
    for c in source_c:
        iid = c.get("source_issue_id")
        by_issue.setdefault(iid, []).append(c)
    
    sid_to_key = {r["source_id"]: r["jira_key"] for r in rows}
    issues = [sid for sid in by_issue if sid in sid_to_key]
    out = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(process_comments, sid, sid_to_key[sid], by_issue[sid], i, len(issues)): sid
            for i, sid in enumerate(issues, 1)
        }
        for f in as_completed(futures):
            out.update(f.result())
    safe_print(f"  Resolved {len(out)} comments")
    return out


# =============================================================
# VERSIONS
# =============================================================
def fetch_versions(pk):
    with cache_lock:
        if pk in version_cache:
            return version_cache[pk]
    d = jira_get(f"/rest/api/3/project/{pk}/version", params={"maxResults": 200})
    vers = d.get("values", []) if d else []
    with cache_lock:
        version_cache[pk] = vers
    return vers

def process_version(v, i, total):
    sid = v.get("source_release_id")
    pk = v.get("jira_project_key")
    name = (v.get("payload") or {}).get("name")
    if not (sid and pk and name):
        return None
    try:
        all_v = fetch_versions(pk)
        match = next((x for x in all_v if x.get("name") == name), None)
        if not match:
            safe_print(f"  [WARN] version '{name}' not in {pk}")
            return None
        return sid, {"jira_version_id": match.get("id"), "jira_version_name": name, "source_release_id": sid}
    except Exception as e:
        safe_print(f"  ERROR version: {e}")
        return None

def build_versions_mapping():
    safe_print("\nResolving versions...")
    vs = load_json("versions.json", default=[])
    out = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(process_version, v, i, len(vs)): v for i, v in enumerate(vs, 1)}
        for f in as_completed(futures):
            res = f.result()
            if res:
                out[res[0]] = res[1]
    safe_print(f"  Resolved {len(out)}/{len(vs)}")
    return out


# =============================================================
# SPRINTS
# =============================================================
def fetch_sprints(board_id):
    with cache_lock:
        if board_id in sprint_cache:
            return sprint_cache[board_id]
    sprints = []
    start = 0
    while True:
        d = jira_get(f"/rest/agile/1.0/board/{board_id}/sprint", params={"startAt": start, "maxResults": 50})
        if not d: break
        batch = d.get("values", [])
        sprints.extend(batch)
        if d.get("isLast", True) or not batch:
            break
        start += len(batch)
    with cache_lock:
        sprint_cache[board_id] = sprints
    return sprints

def process_sprint(s, board_map, i, total):
    sid = s.get("source_sprint_id")
    pk = s.get("jira_project_key")
    name = (s.get("payload") or {}).get("name")
    if not sid or not name:
        return None
    board_id = board_map.get(pk) or (s.get("payload") or {}).get("originBoardId")
    if not board_id:
        safe_print(f"  [WARN] no board for {pk} sprint '{name}'")
        return None
    try:
        sprints = fetch_sprints(board_id)
        match = next((sp for sp in sprints if sp.get("name") == name), None)
        if not match:
            safe_print(f"  [WARN] sprint '{name}' not on board {board_id}")
            return None
        return sid, {
            "jira_sprint_id": match.get("id"),
            "jira_sprint_name": name,
            "jira_board_id": board_id,
            "jira_board_name": match.get("name", "")
        }
    except Exception as e:
        safe_print(f"  ERROR sprint: {e}")
        return None

def build_sprints_mapping():
    safe_print("\nResolving sprints...")
    sprints_json = load_json("sprints.json", default=[])
    board_map = load_sprint_board_map()
    out = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(process_sprint, s, board_map, i, len(sprints_json)): s
            for i, s in enumerate(sprints_json, 1)
        }
        for f in as_completed(futures):
            res = f.result()
            if res:
                out[res[0]] = res[1]
    safe_print(f"  Resolved {len(out)}/{len(sprints_json)}")
    return out


# =============================================================
# LINKS
# =============================================================
def process_link(link, link_cache, lock_local):
    src = link.get("jira_source_key")
    tgt = link.get("jira_target_key")
    ltype = link.get("jira_link_type")
    sid = link.get("source_issue_id")
    tid = link.get("target_issue_id")
    if not src or not tgt:
        return None
    
    try:
        with lock_local:
            cached = link_cache.get(src)
        if cached is None:
            d = jira_get(f"/rest/api/3/issue/{src}", params={"fields": "issuelinks"})
            cached = (d or {}).get("fields", {}).get("issuelinks", [])
            with lock_local:
                link_cache[src] = cached
        
        link_id = None
        for il in cached:
            lt = (il.get("type") or {}).get("name")
            other = il.get("outwardIssue") or il.get("inwardIssue") or {}
            if lt == ltype and other.get("key") == tgt:
                link_id = il.get("id")
                break
        
        if link_id:
            k = f"{sid}->{tid}"
            return k, {
                "jira_link_id": link_id,
                "jira_source_key": src,
                "jira_target_key": tgt,
                "jira_link_type": ltype
            }
        return None
    except Exception as e:
        safe_print(f"  ERROR link {src}->{tgt}: {e}")
        return None

def build_links_mapping(key_id_map):
    safe_print("\nResolving links...")
    links_json = load_json("links.json", default=[])
    if not links_json:
        safe_print("  No links")
        return {}
    cache = {}
    out = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(process_link, l, cache, Lock()): l  # Each gets its own lock? Better use shared
            for l in links_json
        }
        # Actually use one shared cache but thread-safe via Lock
        shared_cache = {}
        shared_lock = Lock()
        futures = {ex.submit(process_link, l, shared_cache, shared_lock): l for l in links_json}
        for f in as_completed(futures):
            res = f.result()
            if res:
                out[res[0]] = res[1]
    safe_print(f"  Resolved {len(out)}/{len(links_json)}")
    return out


# =============================================================
# MAIN
# =============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--excel", default=EXCEL_PATH, help="Path to test.xlsx")
    parser.add_argument("--skip-comments", action="store_true")
    parser.add_argument("--skip-links", action="store_true")
    parser.add_argument("--skip-versions", action="store_true")
    parser.add_argument("--skip-sprints", action="store_true")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()
    
    global MAX_WORKERS
    MAX_WORKERS = args.workers
    
    print("="*60)
    print(f"Project:  {PROJECT_KEY}")
    print(f"JIRA_URL: {JIRA_URL}")
    print(f"Mapping:  {MAPPING_DIR}")
    print(f"Payload:  {PAYLOAD_DIR}")
    print(f"Workers:  {MAX_WORKERS}")
    print(f"Excel:    {args.excel}")
    print("="*60)
    
    rows = load_excel_rows(args.excel)
    issues, key_map = build_issues_mapping(rows)
    comments = {} if args.skip_comments else build_comments_mapping(rows)
    versions = {} if args.skip_versions else build_versions_mapping()
    sprints = {} if args.skip_sprints else build_sprints_mapping()
    links = {} if args.skip_links else build_links_mapping(key_map)
    
    result = {"issues": issues, "comments": comments, "versions": versions, "sprints": sprints, "links": links}
    
    out_path = os.path.join(MAPPING_DIR, "id_mapping1.json")
    os.makedirs(MAPPING_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    
    print("\n" + "="*60)
    print("DONE")
    print(f"Issues:   {len(issues)}")
    print(f"Comments: {len(comments)}")
    print(f"Versions: {len(versions)}")
    print(f"Sprints:  {len(sprints)}")
    print(f"Links:    {len(links)}")
    print(f"Output:   {out_path}")
    print("="*60)

if __name__ == "__main__":
    main()