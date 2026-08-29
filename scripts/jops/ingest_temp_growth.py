#!/usr/bin/env python3
"""
JUM-700: Ingest seller leads from Slack #temp-growth into Twenty CRM.

Flow: read channel history since checkpoint -> parse lead messages ->
scrape portal URL -> upsert Person -> create Seller -> create Listing (_property)
-> react to Slack message + thread confirmation.

Modes:
  dry-run (default)  — parse + scrape only, print what WOULD be written. No CRM writes.
  live               — perform CRM writes.

Usage:
  python3 ingest_temp_growth.py                 # dry-run, last 50 messages
  python3 ingest_temp_growth.py --live          # live mode
  python3 ingest_temp_growth.py --limit 100     # inspect more history
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

SLACK_CHANNEL_ID = "C0A10M2L2SW"  # #temp-growth
BABLU_ENV = "/root/.hermes/profiles/bablu/.env"
STATE_FILE = "/opt/jops/temp_growth_state.json"
LOG_FILE = "/opt/jops/temp_growth_ingest.log"

# --- Slack helpers -----------------------------------------------------------

def load_slack_token():
    with open(BABLU_ENV) as f:
        for line in f:
            if line.startswith("SLACK_BOT_TOKEN="):
                return line.rstrip("\n").split("=", 1)[1]
    raise SystemExit("SLACK_BOT_TOKEN missing in " + BABLU_ENV)

def slack_api(method, params, token):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        "https://slack.com/api/" + method, data=data,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def fetch_history(token, oldest=None, limit=200):
    """Return list of {ts, text, user, permalink} for non-bot root messages."""
    out = []
    cursor = None
    while True:
        params = {"channel": SLACK_CHANNEL_ID, "limit": str(min(limit, 200))}
        if oldest:
            params["oldest"] = oldest
        if cursor:
            params["cursor"] = cursor
        resp = slack_api("conversations.history", params, token)
        if not resp.get("ok"):
            raise SystemExit("history failed: " + repr(resp))
        for m in resp.get("messages", []):
            if m.get("subtype"):          # joins, edits, etc.
                continue
            if m.get("bot_id"):           # our own confirmations
                continue
            out.append({"ts": m["ts"], "text": m.get("text", ""),
                        "user": m.get("user", "")})
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor or len(out) >= limit:
            break
    return out

# --- Lead parsing ------------------------------------------------------------

PHONE_RE = re.compile(r"tel:\+?(\d{10,13})\|[^>]*|(?<!\d)(\+?91[-\s]?)?([6-9]\d{9})(?!\d)")

def parse_lead(msg_text):
    """Extract phone, name, url from a lead message.

    Known formats:
      <tel:+919821754308|+91-9821754308> Bhawana | - <https://...|label>
      +91-9821754308 Bhawana | https://...
    Returns dict or None if no phone+url found.
    """
    text = msg_text
    # Strip slack markup to plain pieces
    phone = None
    m = re.search(r"tel:\+?(\d{10,13})\|", text)
    if m:
        phone = m.group(1)
    else:
        m2 = re.search(r"(?<!\d)\+?91[-\s]?([6-9]\d{9})(?!\d)", text)
        if m2:
            phone = m2.group(1)
        else:
            m3 = re.search(r"(?<!\d)([6-9]\d{9})(?!\d)", text)
            if m3:
                phone = m3.group(1)
    if phone and len(phone) == 12 and phone.startswith("91"):
        phone = phone[2:]  # tel:+919821754308 -> bare 10-digit (CRM format)
    if not phone or len(phone) != 10:
        return None

    urls = re.findall(r"<(https?://[^|\s>]+)(?:\|[^>]*)?>", text)
    if not urls:
        urls = re.findall(r"https?://[^\s|>]+", text)
    if not urls:
        return None
    url = urls[0].strip()

    # Name: text between phone marker and the '|' separator
    plain = re.sub(r"<[^>]+>", " ", text)  # drop <> markup
    plain = plain.replace("+91-" + phone, " ").replace("+91" + phone, " ")
    plain = plain.replace(phone, " ")
    name_part = plain.split("|")[0]
    tokens = [t for t in re.split(r"[\s\-–—]+", name_part) if t.strip()]
    # drop tokens that are urls or pure punctuation
    tokens = [t for t in tokens if not t.lower().startswith(("http", "www."))]
    name = " ".join(tokens[:4]).strip(" -–—|,")
    return {"phone": phone, "name": name or None, "url": url}

def source_from_url(url):
    u = url.lower()
    if "99acres" in u:
        return "NINETYNINE_ACRES"
    if "housing" in u:
        return "HOUSING"
    if "magicbricks" in u or "makaan" in u:
        return "MAGICBRICKS"
    return None

def parse_url_slug(url):
    """Extract hints from 99acres-style URL slugs.

    e.g. /3-bhk-bedroom-apartment-flat-for-sale-in-brigade-cornerstone-utopia-varthur-bangalore-east-1538-sqft-spid-H93574616
    -> {bhk: 3, building_guess: 'Brigade Cornerstone Utopia', sqft: 1538, spid: H93574616}
    """
    out = {}
    m = re.search(r"spid-([A-Za-z0-9]+)", url)
    if m:
        out["spid"] = m.group(1)
    m = re.search(r"/(\d)-bhk", url)
    if m:
        out["bhk"] = int(m.group(1))
    m = re.search(r"(\d{3,5})-sq(?:ft)?", url)
    if m:
        out["sqft"] = int(m.group(1))
    m = re.search(r"for-sale-in-(.+?)-bangalore", url)
    if m:
        slug = m.group(1)
        out["building_slug"] = slug
        out["building_guess"] = slug.replace("-", " ").title()
    return out

# --- Scrape ------------------------------------------------------------------

SCRAPE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    "Referer": "https://www.google.com/",
}

def _fetch(url, timeout=30):
    """Fetch via Google Translate proxy (Akamai bypass), direct fallback.

    99acres: translate.goog works (Aug 28, 2026); direct is 403/417 from our IP.
    Other domains: try direct first, translate proxy as fallback.
    """
    import subprocess
    import urllib.parse

    def _curl(u):
        r = subprocess.run(
            ["curl", "-s", "--compressed", "-L", "--max-time", str(timeout),
             "-A", SCRAPE_HEADERS["User-Agent"],
             "-H", "Accept: " + SCRAPE_HEADERS["Accept"],
             "-H", "Accept-Language: " + SCRAPE_HEADERS["Accept-Language"],
             "-e", SCRAPE_HEADERS["Referer"], u],
            capture_output=True)
        return r.stdout.decode("utf-8", errors="ignore")

    host = urllib.parse.urlparse(url).netloc
    if "99acres" in host:
        # translate.goog primary (hard-rate-limit at 1-2 req/s upstream)
        path = urllib.parse.urlparse(url).path
        proxied = (f"https://{host.replace('.', '-')}.translate.goog{path}"
                   f"?_x_tr_sl=en&_x_tr_tl=hi&_x_tr_hl=en")
        html = _curl(proxied)
        if len(html) > 5000 and "Access Denied" not in html:
            return html
        raise RuntimeError("translate.goog fetch failed for 99acres")
    html = _curl(url)
    if len(html) > 5000 and "Access Denied" not in html:
        return html
    # fallback via translate proxy
    path = urllib.parse.urlparse(url).path
    proxied = (f"https://{host.replace('.', '-')}.translate.goog{path}"
               f"?_x_tr_sl=en&_x_tr_tl=hi&_x_tr_hl=en")
    html = _curl(proxied)
    if len(html) > 5000:
        return html
    raise RuntimeError(f"fetch failed (direct + proxy) for {url}")

def _price_to_micros(text):
    """'₹1.91 Cr' / '₹95 Lac' -> micros (rupees x 1e6)."""
    t = text.replace(",", "").replace("₹", "").strip()
    m = re.match(r"([\d.]+)\s*(Cr|Crore|L|Lac|Lakh|K)?", t, re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    mult = {"cr": 1e7, "crore": 1e7, "l": 1e5, "lac": 1e5, "lakh": 1e5, "k": 1e3}.get(unit, 1)
    return int(val * mult * 1_000_000)

CONFIG_MAP = {1: "OPT1_BHK", 2: "OPT2_BHK", 3: "OPT3_BHK", 4: "OPT4_BHK", 5: "OPT5_BHK"}
FURNISH_MAP = {"unfurnished": "UNFURNISHED", "semi-furnished": "SEMI_FURNISHED",
               "semifurnished": "SEMI_FURNISHED", "furnished": "FULLY_FURNISHED"}
FACE_MAP = {"north": "NORTH", "south": "SOUTH", "east": "EAST", "west": "WEST",
            "north-east": "NORTH_EAST", "north east": "NORTH_EAST",
            "south-east": "SOUTH_EAST", "south east": "SOUTH_EAST",
            "north-west": "NORTH_WEST", "north west": "NORTH_WEST",
            "south-west": "SOUTH_WEST", "south west": "SOUTH_WEST"}

def scrape_listing(url):
    """Fetch a 99acres listing page and extract CRM property fields."""
    if "99acres" not in url:
        return {"error": "unsupported portal (only 99acres implemented)"}
    try:
        html = _fetch(url)
    except Exception as e:
        return {"error": f"fetch failed: {e}"}

    out = {}

    # JSON-LD Apartment block (most reliable)
    ld = None
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(block)
        except Exception:
            continue
        if d.get("@type") == "Apartment":
            ld = d
            break
    if ld:
        out["bedrooms"] = int(ld.get("numberOfRooms") or 0) or None
        out["bathrooms"] = int(ld.get("numberOfBathroomsTotal") or 0) or None
        fs = ld.get("floorSize", "")
        m = re.search(r"([\d.]+)", str(fs))
        out["squareFeet"] = float(m.group(1)) if m else None
        out["floor"] = int(ld["floorLevel"]) if str(ld.get("floorLevel", "")).isdigit() else None
        out["description"] = (ld.get("description") or "").strip() or None
        addr = ld.get("address") or {}
        out["building_guess"] = addr.get("name")
        out["locality"] = addr.get("streetAddress")

    # Price (display value, e.g. ₹1.91 Cr) — id="pdPrice" or whitespace-padded span
    m = re.search(r'id="pdPrice">\s*([^<]+?)\s*<', html)
    if not m:
        m = re.search(r'pdPrice[^>]*>\s*(₹[^<]+?)\s*<', html)
    if m and m.group(1).strip():
        out["price_micros"] = _price_to_micros(m.group(1))
        out["price_display"] = m.group(1).strip()
    else:
        # fallback: ₹X.XX Cr near the fact table
        m = re.search(r'₹\s*([\d.]+\s*(?:Cr|Crore|Lac|Lakh|L|K))', html)
        if m:
            out["price_micros"] = _price_to_micros(m.group(1))
            out["price_display"] = m.group(1).strip()

    # Carpet area
    m = re.search(r'carpetArea_span">([\d,]+)', html)
    if m:
        out["carpetArea"] = float(m.group(1).replace(",", ""))

    # Total floors
    m = re.search(r'out of (\d+)\)', html)
    if m:
        out["totalFloors"] = int(m.group(1))

    # Facing / furnishing (embedded JSON attrs)
    m = re.search(r'"Facing_Label":"([^"]*)"', html)
    if m:
        out["facing"] = FACE_MAP.get(m.group(1).lower().strip())
    m = re.search(r'"furnishStatusLabel":"([^"]*)"', html)
    if m:
        out["furnishing"] = FURNISH_MAP.get(m.group(1).lower().strip().replace(" ", ""))
    m = re.search(r'"propertyType":\{"label":"([^"]*)"', html)
    if m:
        pt = m.group(1).lower()
        out["propertyType"] = ("VILLA" if "villa" in pt else
                               "PENTHOUSE" if "penthouse" in pt else
                               "STUDIO" if "studio" in pt else
                               "COMMERCIAL" if "commercial" in pt else "APARTMENT")

    # Balconies / parking
    m = re.search(r'"balconyNum">\s*(\d+)\s*Balcon', html)
    if m:
        out["balcony"] = int(m.group(1))
    m = re.search(r'Parking\s*:\s*(\d+)\s*Covered', html)
    if m:
        out["parking"] = int(m.group(1))

    # Occupancy: posted-by + possession heuristic
    if re.search(r'Owner of Property', html):
        out["occupancy"] = "SELF"
    elif re.search(r'(Dealer|Agent)', html):
        out["occupancy"] = None  # dealer could be tenant-occupied; leave for review
    m = re.search(r'Possession:\s*(Immediate|Ready to move|Under Construction)', html)
    if m:
        out["possession"] = m.group(1)

    # Configuration from bedrooms
    if out.get("bedrooms"):
        out["configuration"] = CONFIG_MAP.get(out["bedrooms"])

    # Images (dedupe, keep _med/_O variants only)
    imgs = re.findall(r'"link":"(https://newprojects\.99acres\.com[^"]+?_(?:med|O)\.jpg)"', html)
    if not imgs:
        imgs = re.findall(r'(https://newprojects\.99acres\.com[^"\s]+?\.jpg)', html)
    seen, files = set(), []
    for u in imgs:
        base = u.rsplit("_", 1)[0]
        if base in seen:
            continue
        seen.add(base)
        files.append({"tag": "Listing", "url": u, "source": "99acres"})
    out["files"] = files[:10]

    out.setdefault("building_guess", None)
    return out

def react_and_reply(token, ts, text):
    """React + thread confirmation on the processed message."""
    slack_api("reactions.add", {"channel": SLACK_CHANNEL_ID,
                                "timestamp": ts, "name": "white_check_mark"}, token)
    slack_api("chat.postMessage", {"channel": SLACK_CHANNEL_ID,
                                   "thread_ts": ts, "text": text}, token)

def flag_failure(ts, detail):
    """Post failure flag to #alerts (C0BBGL93R1V)."""
    try:
        slack_api("chat.postMessage", {
            "channel": "C0BBGL93R1V",
            "text": f":warning: *temp-growth ingest failure* (ts {ts})\n{detail}"},
            token)
    except Exception:
        pass

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_ts": None}

def save_state(last_ts):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_ts": last_ts}, f)

# --- Main --------------------------------------------------------------------

def main():
    live = "--live" in sys.argv
    limit = 50
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    token = load_slack_token()
    state = load_state()
    checkpoint = state.get("last_ts")
    msgs = fetch_history(token, limit=limit)

    leads = []
    for m in msgs:
        if checkpoint and float(m["ts"]) <= float(checkpoint):
            continue
        lead = parse_lead(m["text"])
        if lead:
            lead["ts"] = m["ts"]
            lead["source"] = source_from_url(lead["url"])
            lead["url_hints"] = parse_url_slug(lead["url"])
            leads.append(lead)

    if not leads:
        print("no new leads")
        return

    print(f"{len(leads)} new leads, mode {'LIVE' if live else 'DRY-RUN'}")
    results = []
    failures = 0
    for lead in sorted(leads, key=lambda x: float(x["ts"])):
        scrape = None
        if live:
            try:
                scrape = scrape_listing(lead["url"])
            except Exception as e:
                scrape = {"error": str(e)}
        try:
            from temp_growth_crm import process_lead
            res = process_lead(lead, scrape, live=live)
            results.append(res)
            if live:
                # Build confirmation text
                parts = [f":white_check_mark: Lead ingested — *{lead['name']}*"]
                if res.get("building"):
                    parts.append(f"Building: {res['building'][1]}")
                elif scrape and not scrape.get("error"):
                    parts.append("Building: no CRM match — needs manual linking")
                if scrape and not scrape.get("error") and scrape.get("price_display"):
                    parts.append(f"Price: ₹{scrape['price_display']}")
                if scrape and scrape.get("error"):
                    parts.append(":warning: listing details not scraped — will need manual completion")
                react_and_reply(token, lead["ts"], "\n".join(parts))
                state["last_ts"] = lead["ts"]
            else:
                state["last_ts"] = lead["ts"]  # dry-run advances too
        except Exception as e:
            failures += 1
            if live:
                flag_failure(lead["ts"], f"{lead['name']} {lead['url']}: {e}")
            print(f"ERROR ts={lead['ts']}: {e}", file=sys.stderr)
    save_state(state["last_ts"])
    print(json.dumps(results, indent=1, default=str))
    if failures:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
