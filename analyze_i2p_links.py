"""
analyze_i2p_links.py  (STANDALONE — no dependency on any other script)

For each Jira key in JIRA_KEYS (below) or passed on the command line:

    1. GET the Jira issue, read customfield_13038 (the "FR Key" —
       source-system key, e.g. "FD-197277"). The prefix before the
       dash (e.g. "FD") is the project key on the backend side.
    2. Derive the backend project key from the FR key itself — it's the
       prefix before the dash (e.g. "FRESHCALL-52496" -> "FRESHCALL",
       "FD-197277" -> "FD"). Different issues can point at different
       backend projects, so this is computed per-issue, never fixed.
    3. Call the backend:  {BACKEND_BASE_URL}/{fr_project_key}/issues/{fr_key}
       and pull "cf_i2p_document" out of the returned JSON. This value
       is an HTML snippet containing zero or more <a href="..."> tags.
    4. Parse that HTML and count the <a> tags that have an href
       attribute, EXCLUDING any <a> tag that also has target="_blank".
    5. For every counted link (i.e. not excluded), check whether its
       href value is longer than 250 characters and flag it.
    6. Compute character counts:
         - total_characters          = len(raw cf_i2p_document HTML)
         - target_blank_characters   = sum of len(str(tag)) for every
                                        <a target="_blank"> tag (the
                                        full "<a ...>...</a>" markup)
         - net_characters            = total_characters - target_blank_characters
    7. Write one row per Jira key into an Excel report.

    All 596+ (or however many) issues are processed CONCURRENTLY using a
    thread pool (15 workers by default — override with --workers), since
    this is almost entirely waiting on network I/O to Jira and the backend.

-----------------------------------------------------------------------
.env variables needed
-----------------------------------------------------------------------
JIRA_BASE_URL=https://yourcompany.atlassian.net
JIRA_EMAIL=you@yourcompany.com
JIRA_API_TOKEN=xxxxxxxxxxxxxxxx
# JIRA_PAT=xxxxxxxxxxxxxxxx          # alternative to email+token

BACKEND_BASE_URL=https://backend.example.com
BACKEND_AUTH_TOKEN=xxxxxxxxxxxxxxxx
# BACKEND_PROJECT_KEY is NOT needed — the project key is derived per-issue
# from the FR key's prefix (the part before the dash).
-----------------------------------------------------------------------

Usage:
    # Bulk run — just paste your keys into JIRA_KEYS below, then:
    python analyze_i2p_links.py

    # Ad-hoc / spot-check run — CLI keys override JIRA_KEYS:
    python analyze_i2p_links.py PLG-4129 PLG-4130
    python analyze_i2p_links.py --keys-file keys.txt
    python analyze_i2p_links.py --limit 20          # only process first 20 keys (testing)
"""

import os
import sys
import json
import argparse
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

load_dotenv(override=True)

# =============================================================
# >>> PASTE YOUR 1000+ JIRA KEYS HERE <<<
# =============================================================
JIRA_KEYS = [
    "PLG-4129", "PLG-4128", "PLG-4124", "PLG-4079", "PLG-2802", "PLG-2798", "PLG-2796", "PLG-2795", "PLG-2794", "PLG-2793", "PLG-2792", "PLG-2791", "PLG-2790", "PLG-673", "PLG-669", "PLG-668", "PLG-667", "PLG-663", "PLG-654", "PLG-645", "PLG-643", "PLG-622", "PLG-621", "PLG-608", "PLG-600", "PLG-592", "PLG-589", "PLG-578", "PLG-565", "PLAT-165324", "PLAT-165318", "PLAT-165317", "PLAT-165310", "PLAT-165304", "PLAT-165303", "PLAT-165302", "PLAT-131872", "PLAT-131866", "PLAT-131861", "PLAT-131855", "PLAT-131837", "PLAT-131792", "PLAT-131787", "PLAT-131780", "PLAT-131771", "PLAT-131752", "PLAT-131717", "PLAT-131712", "PLAT-131645", "PLAT-131599", "PLAT-131597", "PLAT-131550", "PLAT-131497", "PLAT-131483", "PLAT-131469", "PLAT-131384", "PLAT-131294", "PLAT-131293", "PLAT-131281", "PLAT-131277", "PLAT-131242", "PLAT-131228", "CX-335383", "CX-335368", "CX-335354", "CX-335350", "CX-335344", "CX-335334", "CX-335332", "CX-335327", "CX-335324", "CX-335320", "CX-335316", "CX-335310", "CX-335302", "CX-335294", "CX-335292", "CX-335290", "CX-335282", "CX-335267", "CX-335250", "CX-335247", "CX-335242", "CX-335239", "CX-335231", "CX-335230", "CX-335222", "CX-335206", "CX-335198", "CX-335187", "CX-335175", "CX-335174", "CX-335171", "CX-335170", "CX-335168", "CX-335164", "CX-335157", "CX-335155", "CX-335115", "CX-335104", "CX-335089", "CX-335084", "CX-335077", "CX-335075", "CX-335054", "CX-335046", "CX-335043", "CX-335024", "CX-335018", "CX-335011", "CX-335001", "CX-334999", "CX-334983", "CX-334981", "CX-334977", "CX-334976", "CX-334974", "CX-334973", "CX-334969", "CX-334968", "CX-334966", "CX-334965", "CX-334962", "CX-334960", "CX-334952", "CX-334943", "CX-334937", "CX-334931", "CX-334930", "CX-334929", "CX-334928", "CX-334926", "CX-334924", "CX-334911", "CX-334910", "CX-334901", "CX-334900", "CX-334895", "CX-334890", "CX-334884", "CX-334878", "CX-334860", "CX-334839", "CX-334810", "CX-334797", "CX-334796", "CX-334795", "CX-334792", "CX-334787", "CX-334781", "CX-334764", "CX-334754", "CX-334752", "CX-334748", "CX-334743", "CX-334742", "CX-334740", "CX-334725", "CX-334721", "CX-334720", "CX-334717", "CX-334715", "CX-334714", "CX-332967", "CX-332964", "CX-332959", "CX-329315", "CX-329307", "CX-329299", "CX-329293", "CX-329283", "CX-329270", "CX-329249", "CX-329245", "CX-329216", "CX-329214", "CX-329209", "CX-323562", "CX-323560", "CX-323554", "CX-323518", "CX-323502", "CX-323501", "CX-323483", "CX-323481", "CX-323480", "CX-323456", "CX-323438", "CX-323410", "CX-323386", "CX-323382", "CX-323373", "CX-260907", "CX-260902", "CX-260901", "CX-260897", "CX-260892", "CX-260887", "CX-260878", "CX-260875", "CX-260868", "CX-260861", "CX-260823", "CX-260811", "CX-260804", "CX-260802", "CX-260800", "CX-260793", "CX-260771", "CX-260766", "CX-260760", "CX-260745", "CX-260732", "CX-260713", "CX-260702", "CX-260692", "CX-260691", "CX-260690", "CX-260626", "CX-260584", "CX-260555", "CX-260528", "CX-260526", "CX-260523", "CX-260514", "CX-260513", "CX-260510", "CX-260501", "CX-260493", "CX-260491", "CX-260490", "CX-260479", "CX-260476", "CX-260467", "CX-260464", "CX-260456", "CX-260445", "CX-260442", "CX-260415", "CX-260410", "CX-260370", "CX-260348", "CX-260342", "CX-260319", "CX-260292", "CX-260256", "CX-260251", "CX-260241", "CX-260196", "CX-260180", "CX-260178", "CX-260172", "CX-260167", "CX-260166", "CX-260164", "CX-260157", "CX-260140", "CX-260130", "CX-260125", "CX-260113", "CX-260099", "CX-260093", "CX-260091", "CX-260088", "CX-260086", "CX-260075", "CX-260056", "CX-260051", "CX-260043", "CX-260040", "CX-259986", "CX-259977", "CX-259967", "CX-259959", "CX-259942", "CX-259927", "CX-259914", "CX-259892", "CX-259877", "CX-259870", "CX-259848", "CX-259830", "CX-259821", "CX-259818", "CX-259810", "CX-259786", "CX-259785", "CX-259784", "CX-259765", "CX-259761", "CX-259742", "CX-259687", "CX-259665", "CX-259661", "CX-259659", "CX-259647", "CX-259615", "CX-259609", "CX-259604", "CX-259560", "CX-259429", "CX-259428", "CX-259408", "CX-259369", "CX-259355", "CX-249320", "CX-249306", "CX-249297", "CX-249271", "CX-249256", "CX-249243", "CX-249209", "CX-249186", "CX-249173", "CX-249140", "CX-249133", "CX-249103", "CX-249096", "CX-249077", "CX-249059", "CX-249031", "CX-192279", "CX-192266", "CX-192263", "CX-192256", "CX-192252", "CX-192250", "CX-192238", "CX-192237", "CX-192234", "CX-192233", "CX-192226", "CX-192221", "CX-192220", "CX-192216", "CX-192211", "CX-192208", "CX-192188", "CX-192186", "CX-192179", "CX-192176", "CX-192175", "CX-192174", "CX-192172", "CX-192171", "CX-192168", "CX-192151", "CX-192140", "CX-192138", "CX-192133", "CX-192118", "CX-192111", "CX-192110", "CX-192106", "CX-192097", "CX-192095", "CX-192082", "CX-192080", "CX-192069", "CX-192066", "CX-192059", "CX-192054", "CX-192048", "CX-192044", "CX-192037", "CX-192028", "CX-191999", "CX-191996", "CX-191992", "CX-191988", "CX-191940", "CX-191925", "CX-191918", "CX-191915", "CX-191852", "CX-191850", "CX-191836", "CX-191832", "CX-191809", "CX-191801", "CX-191795", "CX-191774", "CX-191773", "CX-191771", "CX-191756", "CX-191751", "CX-191747", "CX-191714", "CX-191701", "CX-191699", "CX-191692", "CX-191641", "CX-191631", "CX-191628", "CX-191621", "CX-191605", "CX-191600", "CX-191598", "CX-191590", "CX-191584", "CX-191571", "CX-191570", "CX-191559", "CX-191547", "CX-191545", "CX-191538", "CX-191535", "CX-191526", "CX-191525", "CX-191519", "CX-191503", "CX-191494", "CX-191465", "CX-191460", "CX-191454", "CX-191443", "CX-191437", "CX-191434", "CX-191431", "CX-191426", "CX-191421", "CX-191409", "CX-191389", "CX-191361", "CX-191356", "CX-191355", "CX-191343", "CX-191333", "CX-191316", "CX-191313", "CX-191312", "CX-191310", "CX-191300", "CX-191299", "CX-191297", "CX-191286", "CX-191276", "CX-191274", "CX-191263", "CX-191262", "CX-191261", "CX-191259", "CX-191258", "CX-191255", "CX-191253", "CX-191251", "CX-191249", "CX-191246", "CX-191243", "CX-191239", "CX-191230", "CX-191224", "CX-191218", "CX-191215", "CX-191211", "CX-191202", "CX-191200", "CX-191189", "CX-191188", "CX-191183", "CX-191180", "CX-191178", "CX-191174", "CX-191158", "CX-191154", "CX-191152", "CX-191141", "CX-191133", "CX-191132", "CX-191129", "CX-191127", "CX-191125", "CX-191115", "CX-191114", "CX-191111", "CX-191106", "CX-191100", "CX-191079", "CX-191072", "CX-191070", "CX-191064", "CX-191045", "CX-191042", "CX-191041", "CX-191024", "CX-191023", "CX-191022", "CX-191012", "CX-190912", "CX-190719", "CX-109688", "CX-109542", "CX-109533", "CX-109507", "CX-109487", "CX-109400", "CX-109378", "AI-77586", "AI-77584", "AI-77570", "AI-77558", "AI-77555", "AI-77550", "AI-77547", "AI-77536", "AI-77533", "AI-77518", "AI-77516", "AI-77510", "AI-77503", "AI-77488", "AI-77481", "AI-77479", "AI-77430", "AI-77428", "AI-77418", "AI-77413", "AI-77393", "AI-77382", "AI-77378", "AI-77377", "AI-77364", "AI-77363", "AI-77356", "AI-948", "AI-940", "AI-936", "AI-930", "AI-928", "AI-926", "AI-923", "AI-922", "AI-919", "AI-916", "AI-912", "AI-907", "AI-900", "AI-898", "AI-890", "AI-888", "AI-885", "AI-842", "AI-841", "AI-818", "AI-805", "AI-803", "AI-801", "AI-797", "AI-793", "AI-783", "AI-771", "AI-770", "AI-768", "AI-762", "AI-760", "AI-752", "AI-749", "AI-745", "AI-733", "AI-732", "AI-714", "AI-712", "AI-692", "AI-650", "AI-626", "AI-620", "AI-607", "AI-578", "AI-571", "AI-562", "AI-558", "AI-552", "AI-549", "AI-540", "AI-526", "AI-521", "AI-519", "AI-489", "AI-464", "AI-430", "AI-429", "AI-408", "AI-386", "AI-373", "AI-319", "AI-282", "AI-281", "AI-276", "AI-262", "AI-260", "AI-248", "AI-235", "AI-208", "AI-185", "AI-178", "AI-177"
]

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

# =============================================================
# CONFIG — Backend (source system)
# =============================================================
BACKEND_BASE_URL    = os.getenv("BACKEND_BASE_URL", "").rstrip("/")
BACKEND_AUTH_TOKEN  = os.getenv("BACKEND_AUTH_TOKEN") or os.getenv("AUTH_TOKEN")

BACKEND_SESSION = requests.Session()
BACKEND_SESSION.headers.update({
    "Authorization": f"Token {BACKEND_AUTH_TOKEN}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
})

FR_KEY_FIELD    = "customfield_13038"   # "FR Key" field on the Jira issue
BACKEND_DOC_KEY = "cf_i2p_document"     # field inside the backend JSON we want

HREF_LENGTH_LIMIT = 250                 # links longer than this get flagged
DEFAULT_WORKERS   = 15                  # concurrent threads for bulk runs

OUTPUT_DIR  = "i2p docs"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "i2p_link_report.xlsx")

# requests.Session objects are safe to share and reuse across threads for
# simple GETs like these (connection pooling handles the concurrency).
_print_lock = threading.Lock()


def derive_project_key(fr_key):
    """
    The backend project key is the prefix of the FR key before the first
    dash: "FRESHCALL-52496" -> "FRESHCALL", "FD-197277" -> "FD".
    Different Jira issues can point at different backend projects, so this
    is always computed per-issue rather than pinned to one fixed value.
    """
    if "-" not in fr_key:
        raise RuntimeError(f"FR key '{fr_key}' has no '-' — can't derive a project key from it")
    return fr_key.split("-", 1)[0]


# =============================================================
# Jira helper (GET)
# =============================================================
def jira_get(path):
    url = f"{JIRA_BASE_URL}/rest/{path}"
    resp = JIRA_SESSION.get(url, timeout=30)
    data = resp.json() if resp.content else None
    return data, resp.status_code


def get_fr_key(jira_key):
    data, status_code = jira_get(f"api/3/issue/{jira_key}?fields={FR_KEY_FIELD}")
    if status_code != 200 or not data:
        raise RuntimeError(f"Failed to fetch {FR_KEY_FIELD} (status {status_code}): {data}")

    fr_key = data.get("fields", {}).get(FR_KEY_FIELD)
    if not fr_key:
        raise RuntimeError(f"{FR_KEY_FIELD} is empty — nothing to look up in backend")

    return fr_key


# =============================================================
# Backend helper (GET)
# =============================================================
def get_backend_i2p_document(fr_key, project_key):
    url = f"{BACKEND_BASE_URL}/{project_key}/issues/{fr_key}.json"
    resp = BACKEND_SESSION.get(url, timeout=30)

    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(
            f"Backend GET {url} returned {resp.status_code}. "
            f"Body (first 300 chars): {resp.text[:300]!r}"
        ) from e

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Backend GET {url} returned status {resp.status_code} but the body "
            f"isn't JSON (Content-Type: {resp.headers.get('Content-Type')}). "
            f"Body (first 300 chars): {resp.text[:300]!r}"
        ) from e

    issue = data.get("issue", data)
    custom_fields = issue.get("custom_field", {})

    html_value = custom_fields.get(BACKEND_DOC_KEY)
    if not html_value:
        raise RuntimeError(f"Backend has no value for {BACKEND_DOC_KEY}")

    return html_value


# =============================================================
# Analyze links + characters in the HTML
# =============================================================
def analyze_links(html):
    soup = BeautifulSoup(html, "html.parser")
    anchors = [a for a in soup.find_all("a") if a.get("href")]

    total_anchor_tags = len(anchors)
    counted_links = []
    excluded_target_blank = 0
    target_blank_characters = 0

    for a in anchors:
        target = (a.get("target") or "").strip().lower()
        href = a.get("href").strip()

        if target == "_blank":
            excluded_target_blank += 1
            target_blank_characters += len(str(a))   # full "<a ...>...</a>" markup
            continue

        counted_links.append({
            "href": href,
            "length": len(href),
            "over_250": len(href) > HREF_LENGTH_LIMIT,
        })

    over_250_links = [l for l in counted_links if l["over_250"]]
    total_characters = len(html)
    net_characters = total_characters - target_blank_characters

    return {
        "total_anchor_tags": total_anchor_tags,
        "excluded_target_blank": excluded_target_blank,
        "counted_links": counted_links,
        "counted_count": len(counted_links),
        "over_250_links": over_250_links,
        "over_250_count": len(over_250_links),
        "total_characters": total_characters,
        "target_blank_characters": target_blank_characters,
        "net_characters": net_characters,
    }


# =============================================================
# Process one Jira key end-to-end (never raises — returns a row dict)
# =============================================================
def process_key(jira_key):
    row = {
        "jira_key": jira_key,
        "fr_key": "",
        "fr_project_key": "",
        "total_anchor_tags": "",
        "excluded_target_blank": "",
        "counted_count": "",
        "over_250_count": "",
        "over_250_hrefs": "",
        "total_characters": "",
        "target_blank_characters": "",
        "net_characters": "",
        "status": "OK",
        "error": "",
    }
    try:
        fr_key = get_fr_key(jira_key)
        row["fr_key"] = fr_key

        project_key = derive_project_key(fr_key)
        row["fr_project_key"] = project_key

        html_value = get_backend_i2p_document(fr_key, project_key)
        result = analyze_links(html_value)

        row["total_anchor_tags"] = result["total_anchor_tags"]
        row["excluded_target_blank"] = result["excluded_target_blank"]
        row["counted_count"] = result["counted_count"]
        row["over_250_count"] = result["over_250_count"]
        row["over_250_hrefs"] = " | ".join(l["href"] for l in result["over_250_links"])
        row["total_characters"] = result["total_characters"]
        row["target_blank_characters"] = result["target_blank_characters"]
        row["net_characters"] = result["net_characters"]

    except Exception as e:
        row["status"] = "FAILED"
        row["error"] = str(e)

    return row


# =============================================================
# Excel writer
# =============================================================
COLUMNS = [
    ("jira_key",                 "Jira Key",                    16),
    ("fr_key",                   "FR Key",                      16),
    ("fr_project_key",           "Backend Project Key",         16),
    ("total_anchor_tags",        "Total <a href> Tags Found",   14),
    ("excluded_target_blank",    'Excluded (target="_blank")',  14),
    ("counted_count",            "Counted Links (Final)",       14),
    ("over_250_count",           "Links Over 250 Chars",        14),
    ("over_250_hrefs",           "Over-250 Href(s)",            50),
    ("total_characters",         "Total Characters",            14),
    ("target_blank_characters",  'Target="_blank" Characters',  16),
    ("net_characters",           "Net Characters (Total - Blank)", 18),
    ("status",                   "Status",                      10),
    ("error",                    "Error",                       40),
]


def write_excel(rows, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "i2p Link Report"

    header_font = Font(name="Arial", bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    body_font = Font(name="Arial")
    fail_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")

    for col_idx, (_, header, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    ws.freeze_panes = "A2"

    for r_idx, row in enumerate(rows, start=2):
        for c_idx, (key, _, _) in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=r_idx, column=c_idx, value=row[key])
            cell.font = body_font
            if row["status"] == "FAILED":
                cell.fill = fail_fill

    wb.save(path)


# =============================================================
# MAIN
# =============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Bulk-analyze links + character counts in backend cf_i2p_document "
                     "for a list of Jira keys, writing results to an Excel report."
    )
    parser.add_argument("keys", nargs="*", help="Jira issue keys (overrides JIRA_KEYS list if given)")
    parser.add_argument("--keys-file", default=None, help="Path to a text file with one Jira key per line")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N keys (for testing)")
    parser.add_argument("--sleep", type=float, default=0.0,
                         help="Seconds each worker sleeps before its request (rate limiting)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                         help=f"Number of concurrent worker threads (default: {DEFAULT_WORKERS})")
    parser.add_argument("--out", default=OUTPUT_FILE, help=f"Output .xlsx path (default: {OUTPUT_FILE})")
    args = parser.parse_args()

    jira_keys = list(args.keys)
    if args.keys_file:
        with open(args.keys_file, "r", encoding="utf-8") as f:
            jira_keys.extend(line.strip() for line in f if line.strip())
    if not jira_keys:
        jira_keys = list(JIRA_KEYS)

    if not jira_keys:
        print("No Jira keys given. Paste them into JIRA_KEYS in the script, "
              "or pass them as arguments / --keys-file.")
        return 1

    if args.limit:
        jira_keys = jira_keys[: args.limit]

    if not JIRA_BASE_URL or not ((JIRA_EMAIL and JIRA_API_TOKEN) or JIRA_PAT):
        print("ERROR: Jira credentials missing. Set JIRA_BASE_URL and either "
              "(JIRA_EMAIL + JIRA_API_TOKEN) or JIRA_PAT in .env")
        return 1

    if not BACKEND_BASE_URL or not BACKEND_AUTH_TOKEN:
        print("ERROR: Backend credentials missing. Set BACKEND_BASE_URL and "
              "BACKEND_AUTH_TOKEN in .env")
        return 1

    total = len(jira_keys)
    print(f"Processing {total} Jira key(s) with {args.workers} worker thread(s)...")

    def worker(jira_key):
        if args.sleep:
            time.sleep(args.sleep)
        return process_key(jira_key)

    rows_by_key = {}
    failures = []
    completed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_key = {executor.submit(worker, k): k for k in jira_keys}
        for future in as_completed(future_to_key):
            jira_key = future_to_key[future]
            completed += 1
            try:
                row = future.result()
            except Exception as e:
                # Should not normally happen — process_key() already catches
                # everything internally — but guard against unexpected errors.
                row = {
                    "jira_key": jira_key, "fr_key": "", "fr_project_key": "",
                    "total_anchor_tags": "", "excluded_target_blank": "",
                    "counted_count": "", "over_250_count": "", "over_250_hrefs": "",
                    "total_characters": "", "target_blank_characters": "",
                    "net_characters": "", "status": "FAILED", "error": str(e),
                }

            rows_by_key[jira_key] = row

            with _print_lock:
                if row["status"] == "FAILED":
                    failures.append(jira_key)
                    print(f"[{completed}/{total}] {jira_key}: FAILED — {row['error']}")
                else:
                    print(f"[{completed}/{total}] {jira_key}: OK "
                          f"(counted={row['counted_count']}, over250={row['over_250_count']}, "
                          f"net_chars={row['net_characters']})")

    # Preserve the original input order in the report, regardless of
    # which thread finished first.
    rows = [rows_by_key[k] for k in jira_keys]

    write_excel(rows, args.out)

    print("\n" + "=" * 60)
    print(f"Done. {total - len(failures)}/{total} succeeded.")
    if failures:
        print(f"Failed ({len(failures)}): {failures[:20]}{' ...' if len(failures) > 20 else ''}")
    print(f"Report written to: {args.out}")
    print("=" * 60)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())