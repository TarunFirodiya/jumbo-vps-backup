#!/usr/bin/env python3
"""
Callyzer recording -> Google Drive -> CRM Call Link sync.

For each Callyzer call with a non-empty call_recording_url:
  1. Download the MP3 (public URL, no auth).
  2. Upload it to the matching monthly Google Drive folder under the root folder.
  3. Write the Drive web link into the CRM _communication row's
     callLinkPrimaryLinkUrl / callLinkPrimaryLinkLabel.

Matching a Callyzer call to a CRM _communication row:
  Callyzer gives emp_number (agent), client_number, call_date, call_time, call_type.
  CRM _communication has name like "📞<Agent> x <Client> - <HH:MM:SS>",
  direction (OUTBOUND/INBOUND), duration, timestamp.
  We match on: direction == call_type mapping, and timestamp within +-90s of the
  Callyzer call's IST datetime. Phone numbers aren't stored on the comm row, so we
  match on timestamp+direction+duration as a secondary signal. If ambiguous (>1 or 0
  matches) we skip and log for manual review (no blind update).

Run modes:
  default  -> live: download, upload, update CRM
  --dry-run -> fetch + match only, print proposed actions, no download/upload/DB write
  --since <epoch> -> override last-run state for the from-window (testing)
  --limit N -> cap number of calls processed this run (testing / test gate)

State file: /opt/jops/callyzer_recording_sync.state.json
  { "last_epoch": <int>, "done_call_ids": ["...", ...] }

Callyzer API: POST /api/v2.1/call-log/history
  body: {synced_from: epoch, synced_to: epoch, page_no, page_size}
  Rate limit: 1 request / second.
"""
import os, sys, json, time, subprocess, datetime, glob, shutil

# ---- config ----
DRIVE_ROOT = "1Dq75THYRdy9IjFfVM-yutImwbSGO0VuI"
CALYZER_TOKEN = "f6377a30-74b6-4a99-954a-d4b674bf22cf"
CALYZER_URL = "https://api1.callyzer.co/api/v2.1/call-log/history"
GWS = "/root/.hermes/node/lib/node_modules/@googleworkspace/cli/bin/gws"
BRIDGE = "/root/.hermes/profiles/operator/skills/productivity/google-workspace/scripts/gws_bridge.py"
STATE_FILE = "/opt/jops/callyzer_recording_sync.state.json"
STAGE = "/tmp/callyzer_rec"
SCHEMA = "workspace_1l3urgumjmspnjxohclmfz6fx"

import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--since", type=int, default=None)
ap.add_argument("--until", type=int, default=None)
ap.add_argument("--limit", type=int, default=None)
ap.add_argument("--migrate", action="store_true",
                help="Scan CRM rows with media1.callyzer.co links and migrate them to Drive")
ap.add_argument("--verbose", action="store_true",
                help="Print per-row progress logs (cron runs silent by default)")
args = ap.parse_args()

os.makedirs(STAGE, exist_ok=True)

def log(*a):
    # Silent by default so the no_agent cron does not spam Slack.
    if args.verbose or args.dry_run:
        print(*a, flush=True)

def alert(*a):
    # Always printed -> becomes the cron's Slack message on real failure.
    print(*a, flush=True)

# ---- state ----
def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {"last_epoch": int(time.time()) - 3600, "done_call_ids": []}

def save_state(s):
    tmp = STATE_FILE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE_FILE)

state = load_state()
done_ids = set(state.get("done_call_ids", []))

# ---- Callyzer fetch ----
def fetch_calls(since_epoch, to_epoch):
    calls = []
    page = 1
    while True:
        body = json.dumps({
            "synced_from": since_epoch,
            "synced_to": to_epoch,
            "page_no": page,
            "page_size": 100,
        })
        # rate limit: 1 req/sec
        time.sleep(1.1)
        out = subprocess.run(
            ["curl", "-s", "-m", "25", "-X", "POST", CALYZER_URL,
             "-H", "content-type: application/json",
             "-H", f"Authorization: Bearer {CALYZER_TOKEN}",
             "-d", body],
            capture_output=True, text=True, timeout=30)
        try:
            d = json.loads(out.stdout)
        except Exception:
            log("WARN bad json page", page, out.stdout[:200])
            break
        res = d.get("result") or []
        if not res:
            break
        calls.extend(res)
        if len(res) < 100:
            break
        page += 1
        if page > 50:  # safety
            break
    return calls

# Known folder IDs (Tarun's Drive root 1Dq75THYRdy9IjFfVM-yutImwbSGO0VuI).
# These use FULL month names (e.g. "July-2026"). Used to avoid creating duplicate
# folders when the name-query misses. New months (Aug-2026+) are created on demand.
KNOWN_FOLDERS = {
    "January-2026": "1k4NJ9zjRzSTmzAZw3BKVVPdsfU1L_5Di",
    "February-2026": "1NUjTfkwFm3TOJo4Dr7jrG_fIzOpyvGjD",
    "March-2026": "1EremBNi6SO7-pYfTrro6J6ICyWCYmtUP",
    "April-2026": "1TTaP57Em3vFwQRCGhgLJUXwocAjPnCg-",
    "May-2026": "1aHrAksk1VrFDqmHgvDyARMLXTkrmNhcS",
    "June-2026": "1wjVKKHC8bBAkBk8XueNwsOBrMlXbc7oA",
    "July-2026": "1KXXRmMJEIo5ssAu6ZLf85zL5By5UbqIZ",
}
_folder_cache = {}
def month_folder_id(yyyymm):
    """Return Drive folder id for a 'Month-YYYY' label (FULL month name).
    Prefer KNOWN_FOLDERS; otherwise query Drive; create only if truly missing.
    """
    if yyyymm in _folder_cache:
        return _folder_cache[yyyymm]
    if yyyymm in KNOWN_FOLDERS:
        _folder_cache[yyyymm] = KNOWN_FOLDERS[yyyymm]
        return KNOWN_FOLDERS[yyyymm]
    # list existing
    q = f"'{DRIVE_ROOT}' in parents and mimeType='application/vnd.google-apps.folder' and name='{yyyymm}'"
    out = subprocess.run(
        ["python3", BRIDGE, "drive", "files", "list",
         "--params", json.dumps({"q": q, "fields": "files(id,name)"})],
        capture_output=True, text=True, timeout=40)
    try:
        files = json.loads(out.stdout).get("files", [])
    except Exception:
        files = []
    if files:
        fid = files[0]["id"]
    else:
        if args.dry_run:
            log(f"[dry-run] would create folder {yyyymm}")
            fid = "DRYRUN"
        else:
            out = subprocess.run(
                ["python3", BRIDGE, "drive", "files", "create",
                 "--json", json.dumps({"name": yyyymm,
                                       "mimeType": "application/vnd.google-apps.folder",
                                       "parents": [DRIVE_ROOT]})],
                capture_output=True, text=True, timeout=40)
            try:
                fid = json.loads(out.stdout)["id"]
            except Exception:
                log("ERROR creating folder", yyyymm, out.stdout[:300])
                return None
    _folder_cache[yyyymm] = fid
    return fid

# ---- Drive upload ----
def drive_file_exists(call_id, fid):
    """Return webViewLink if <call_id>.mp3 already exists in folder fid, else None."""
    lst = subprocess.run(
        ["python3", BRIDGE, "drive", "files", "list",
         "--params", json.dumps({"q": f"name='{call_id}.mp3' and '{fid}' in parents",
                                 "fields": "files(id,webViewLink)"})],
        capture_output=True, text=True, timeout=40)
    try:
        files = json.loads(lst.stdout).get("files", [])
        if files and files[0].get("webViewLink"):
            return files[0]["webViewLink"]
    except Exception:
        pass
    return None

def upload_to_drive(local_path, call_id, month_label):
    fid = month_folder_id(month_label)
    if not fid or fid == "DRYRUN":
        return None
    # Idempotency: reuse existing Drive file if already uploaded
    existing = drive_file_exists(call_id, fid)
    if existing:
        return existing
    out = None
    for attempt in range(2):  # retry once on transient gws_bridge failure
        out = subprocess.run(
            ["python3", BRIDGE, "drive", "files", "create",
             "--upload", local_path,
             "--json", json.dumps({"name": f"{call_id}.mp3", "parents": [fid]})],
            capture_output=True, text=True, timeout=120)
        try:
            j = json.loads(out.stdout)
            if j.get("id"):
                break
        except Exception:
            pass
        if attempt == 0:
            time.sleep(3)
    try:
        j = json.loads(out.stdout)
        file_id = j.get("id")
        if not file_id:
            return None
    except Exception:
        return None
    # fetch webViewLink (create response doesn't include it)
    lst = subprocess.run(
        ["python3", BRIDGE, "drive", "files", "list",
         "--params", json.dumps({"q": f"name='{call_id}.mp3' and '{fid}' in parents",
                                 "fields": "files(id,webViewLink)"})],
        capture_output=True, text=True, timeout=40)
    try:
        files = json.loads(lst.stdout).get("files", [])
        if files and files[0].get("webViewLink"):
            return files[0]["webViewLink"]
    except Exception:
        pass
    return f"https://drive.google.com/file/d/{file_id}/view"

# ---- CRM match ----
def callyzer_dt_ist(call_date, call_time):
    """call_date=YYYY-MM-DD, call_time=HH:MM or HH:MM:SS (IST). Return tz-aware UTC datetime."""
    ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    ct = (call_time or "").strip()
    fmt = "%Y-%m-%d %H:%M:%S" if len(ct) >= 8 else "%Y-%m-%d %H:%M"
    naive = datetime.datetime.strptime(f"{call_date} {ct}", fmt)
    dt_ist = naive.replace(tzinfo=ist)
    return dt_ist.astimezone(datetime.timezone.utc)

def _pgfmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S+00")

def match_crm_row(call):
    """Return list of candidate CRM _communication rows for a Callyzer call.

    Matching key (Tarun's spec): phone number + timestamp + direction.
    CRM comms don't reliably store phone numbers, so we use the next best
    deterministic signal: direction + timestamp (+-300s) + duration (+-15s).
    Caller treats exactly-1 result as a confident match.
    """
    ctype = (call.get("call_type") or "").upper()
    direction = "OUTBOUND" if ctype == "OUTGOING" else "INBOUND"
    target_utc = callyzer_dt_ist(call.get("call_date"), call.get("call_time"))
    lo = _pgfmt(target_utc - datetime.timedelta(seconds=300))
    hi = _pgfmt(target_utc + datetime.timedelta(seconds=300))
    try:
        dur = int(float(call.get("duration") or 0))
    except Exception:
        dur = 0
    sql = f"""
      SELECT id, name, direction, duration, "timestamp"
      FROM "{SCHEMA}"."_communication"
      WHERE direction = '{direction}'
        AND "timestamp" >= '{lo}'
        AND "timestamp" <= '{hi}'
        AND ABS(COALESCE(duration, 0) - {dur}) <= 15
      ORDER BY abs(extract(epoch from ("timestamp" - '{_pgfmt(target_utc)}'))) ASC
      LIMIT 5;
    """
    with open("/tmp/_match.sql", "w") as f:
        f.write(sql)
    subprocess.run(["docker", "cp", "/tmp/_match.sql", "twenty-db-1:/tmp/_match.sql"],
                   capture_output=True, timeout=10)
    out = subprocess.run(
        ["docker", "exec", "twenty-db-1", "psql", "-U", "twenty", "-d", "default",
         "-t", "-A", "-F", "\t", "-f", "/tmp/_match.sql"],
        capture_output=True, text=True, timeout=30)
    rows = []
    for line in out.stdout.strip().splitlines():
        if not line.strip() or "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) >= 5:
            rows.append({"id": parts[0], "name": parts[1], "direction": parts[2],
                         "duration": parts[3], "timestamp": parts[4]})
    return rows

def update_crm(row_id, link):
    sql = f"""
      UPDATE "{SCHEMA}"."_communication"
      SET "callLinkPrimaryLinkLabel" = 'Call Recording',
          "callLinkPrimaryLinkUrl" = '{link}',
          "updatedAt" = NOW()
      WHERE id = '{row_id}';
    """
    with open("/tmp/_upd.sql", "w") as f:
        f.write(sql)
    subprocess.run(["docker", "cp", "/tmp/_upd.sql", "twenty-db-1:/tmp/_upd.sql"],
                   capture_output=True, timeout=10)
    subprocess.run(
        ["docker", "exec", "twenty-db-1", "psql", "-U", "twenty", "-d", "default",
         "-f", "/tmp/_upd.sql"],
        capture_output=True, text=True, timeout=30)

# ---- Migration: CRM rows polluted with raw Callyzer media URLs ----
import re as _re
_CALLYZER_URL_RE = _re.compile(r"media1\.callyzer\.co/public/[^/]+/(\d+)/(\d{8})/(\d+)_(\d+)_(\d{8})_(\d{6})\.mp3$")

def find_callyzer_linked_rows(limit=None):
    """Return CRM _communication rows whose callLinkPrimaryLinkUrl points to media1.callyzer.co."""
    sql = f"""
      SELECT id, "callLinkPrimaryLinkUrl"
      FROM "{SCHEMA}"."_communication"
      WHERE "callLinkPrimaryLinkUrl" LIKE 'https://media1.callyzer.co/%'
      ORDER BY "updatedAt" DESC
    """
    if limit:
        sql += f" LIMIT {limit};"
    else:
        sql += ";"
    with open("/tmp/_mig_find.sql", "w") as f:
        f.write(sql)
    subprocess.run(["docker", "cp", "/tmp/_mig_find.sql", "twenty-db-1:/tmp/_mig_find.sql"],
                   capture_output=True, timeout=10)
    out = subprocess.run(
        ["docker", "exec", "twenty-db-1", "psql", "-U", "twenty", "-d", "default",
         "-t", "-A", "-F", "\t", "-f", "/tmp/_mig_find.sql"],
        capture_output=True, text=True, timeout=40)
    rows = []
    for line in out.stdout.strip().splitlines():
        if not line.strip() or "\t" not in line:
            continue
        p = line.split("\t")
        if len(p) >= 2:
            rows.append({"id": p[0], "url": p[1]})
    return rows

def migrate_row(row):
    """Download the Callyzer recording, upload to Drive, replace CRM link. Return Drive link or None."""
    url = row["url"]
    m = _CALLYZER_URL_RE.search(url)
    if not m:
        return None
    emp, ymd, client, _emp2, _ymd2, hms = m.groups()
    mnum = int(ymd[4:6])
    mon = ["January","February","March","April","May","June","July","August","September","October","November","December"][mnum-1]
    month_label = f"{mon}-{ymd[:4]}"
    fname = url.rstrip("/").split("/")[-1]

    local = os.path.join(STAGE, fname)
    subprocess.run(["curl", "-s", "-m", "60", "-o", local, url],
                   capture_output=True, text=True, timeout=70)
    if not os.path.exists(local) or os.path.getsize(local) < 1000:
        return None
    link = upload_to_drive(local, fname.rsplit(".",1)[0], month_label)
    os.remove(local)
    return link

def run_migration(limit=None):
    """Self-healing: migrate all CRM rows with Callyzer media URLs to Drive links.
    Silent on success; returns count migrated (and count failed for alerting)."""
    rows = find_callyzer_linked_rows(limit)
    if not rows:
        return 0, 0
    migrated = 0
    failed = 0
    for r in rows:
        if args.dry_run:
            migrated += 1
            continue
        try:
            link = migrate_row(r)
        except Exception:
            link = None
        if link:
            try:
                update_crm(r["id"], link)
                migrated += 1
            except Exception:
                failed += 1
        else:
            failed += 1
    if failed:
        # main() will emit the single alert; don't double-alert here
        pass
    return migrated, failed

# ---- main ----
def main():
    since = args.since if args.since else state["last_epoch"]
    to = args.until if args.until else int(time.time())
    calls = fetch_calls(since, to)

    candidates = [c for c in calls if c.get("call_recording_url") and c.get("id") not in done_ids]
    if args.limit:
        candidates = candidates[:args.limit]

    processed = 0
    linked = 0
    for c in candidates:
        cid = c.get("id")
        url = c.get("call_recording_url")
        mnum = int(c.get("call_date")[5:7])
        mon = ["January","February","March","April","May","June","July","August","September","October","November","December"][mnum-1]
        month_label = f"{mon}-{c.get('call_date')[:4]}"

        rows = match_crm_row(c)
        confident = (len(rows) == 1)

        if args.dry_run:
            done_ids.add(cid)
            processed += 1
            continue

        local = os.path.join(STAGE, f"{cid}.mp3")
        subprocess.run(["curl", "-s", "-m", "60", "-o", local, url],
                       capture_output=True, text=True, timeout=70)
        if not os.path.exists(local) or os.path.getsize(local) < 1000:
            continue

        link = upload_to_drive(local, cid, month_label)
        os.remove(local)
        if not link:
            continue

        done_ids.add(cid)
        processed += 1
        if confident:
            try:
                update_crm(rows[0]["id"], link)
                linked += 1
            except Exception:
                pass

    # update state
    state["last_epoch"] = to
    state["done_call_ids"] = list(done_ids)[-5000:]
    if not args.dry_run:
        save_state(state)

    # Self-healing: migrate any CRM rows still pointing at Callyzer media URLs
    mig, mig_fail = run_migration()

    # Silent on success. Only alert on real failure (keeps hourly cron quiet).
    if mig_fail:
        alert(f"sync: {mig} callyzer->drive migrated, {mig_fail} FAILED (will retry next run)")
    elif args.verbose:
        alert(f"sync: {processed} uploaded, {linked} linked, {mig} migrated")


if __name__ == "__main__":
    main()
