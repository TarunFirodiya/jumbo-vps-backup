#!/usr/bin/env python3
"""
Phase 2 backfill: link existing Google Drive call recordings to June-18+ CRM
_communication rows. Safe: only UPDATES callLinkPrimaryLinkUrl where it is NULL
and a confident (single) match is found. Never creates CRM rows.

Drive layout handled:
  - July-2026: flat  <call_id>.mp3   (from live sync; call_id not directly usable,
                                      but we match by timestamp+direction)
  - June-2026: nested JUMxxxx/<emp>/<YYYYMMDD>/<emp>_<client>_<YYYYMMDD>_<HHMMSS>.mp3
                                      (filename gives emp/client/datetime)

Matching key: direction + timestamp(+-300s). For June files direction is derived
from whether `emp` is one of our agent numbers (folder names under JUMxxxx).
Duration is NOT in filenames, so we rely on timestamp+direction only (wider than
live sync which also had duration).

Usage:
  python3 backfill_recording_links.py [--dry-run] [--limit N]
"""
import os, sys, json, argparse, subprocess, datetime, re

SCHEMA = "workspace_1l3urgumjmspnjxohclmfz6fx"
BRIDGE = "/root/.hermes/profiles/operator/skills/productivity/google-workspace/scripts/gws_bridge.py"
# node bin dir so `gws` is on PATH
os.environ["PATH"] = "/root/.hermes/node/lib/node_modules/@googleworkspace/cli/bin:" + os.environ.get("PATH", "")

DRIVE_FOLDERS = {
    "June-2026": "1wjVKKHC8bBAkBk8XueNwsOBrMlXbc7oA",
    "July-2026": "1KXXRmMJEIo5ssAu6ZLf85zL5By5UbqIZ",
}

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
UTC = datetime.timezone.utc

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--limit", type=int, default=None)
args = ap.parse_args()

def log(m):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)

# ---------- Drive recursive listing ----------
def gdrive_list(q, fields="files(id,name,mimeType,webViewLink)"):
    out = subprocess.run(
        ["python3", BRIDGE, "drive", "files", "list",
         "--params", json.dumps({"q": q, "fields": fields, "pageSize": 200})],
        capture_output=True, text=True, timeout=60)
    try:
        return json.loads(out.stdout).get("files", [])
    except Exception:
        return []

def walk_mp3s(folder_id, prefix=""):
    """Recursively yield (name, webViewLink, path_str) for all .mp3 files under folder_id."""
    results = []
    items = gdrive_list(f"'{folder_id}' in parents")
    for it in items:
        if it.get("mimeType") == "application/vnd.google-apps.folder":
            results.extend(walk_mp3s(it["id"], f"{prefix}/{it['name']}"))
        elif it.get("name", "").lower().endswith(".mp3"):
            results.append((it["name"], it.get("webViewLink", ""), prefix))
    return results

# Parse June-style filename: emp_client_YYYYMMDD_HHMMSS.mp3
FN_RE = re.compile(r"^(\d+)_(\d+)_(\d{8})_(\d{6})\.mp3$")

# Process only recordings on/after this date (CRM has no comms before then)
MIN_DATE = datetime.date(2026, 6, 18)

def parse_june_filename(name):
    m = FN_RE.match(name)
    if not m:
        return None
    emp, client, ymd, hms = m.groups()
    dt = datetime.datetime.strptime(f"{ymd} {hms}", "%Y%m%d %H%M%S").replace(tzinfo=IST)
    if dt.date() < MIN_DATE:
        return None  # outside backfill scope
    return {"emp": emp, "client": client, "dt_ist": dt, "dt_utc": dt.astimezone(UTC)}

# ---------- CRM bulk load ----------
def load_crm_rows():
    """Load all June-18+ unlinked _communication rows once (id, direction, timestamp)."""
    sql = f"""
      SELECT id, direction, "timestamp"
      FROM "{SCHEMA}"."_communication"
      WHERE "timestamp" >= '2026-06-18'
        AND "callLinkPrimaryLinkUrl" IS NULL;
    """
    with open("/tmp/_bf_load.sql", "w") as f:
        f.write(sql)
    subprocess.run(["docker", "cp", "/tmp/_bf_load.sql", "twenty-db-1:/tmp/_bf_load.sql"],
                   capture_output=True, timeout=10)
    out = subprocess.run(
        ["docker", "exec", "twenty-db-1", "psql", "-U", "twenty", "-d", "default",
         "-t", "-A", "-F", "\t", "-f", "/tmp/_bf_load.sql"],
        capture_output=True, text=True, timeout=60)
    rows = []
    for line in out.stdout.strip().splitlines():
        if not line.strip() or "\t" not in line:
            continue
        p = line.split("\t")
        if len(p) >= 3:
            try:
                ts = datetime.datetime.strptime(p[2][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            except Exception:
                continue
            rows.append({"id": p[0], "direction": p[1], "ts": ts})
    return rows

def match_crm_bulk(dt_utc, direction, crm_rows):
    """Return list of matching CRM rows within +-60s (in-memory), sorted by closeness."""
    lo = dt_utc - datetime.timedelta(seconds=60)
    hi = dt_utc + datetime.timedelta(seconds=60)
    matches = []
    for r in crm_rows:
        if r["direction"] != direction:
            continue
        if lo <= r["ts"] <= hi:
            matches.append(r)
    matches.sort(key=lambda r: abs((r["ts"] - dt_utc).total_seconds()))
    return matches[:5]

def update_crm(row_id, link):
    sql = f"""
      UPDATE "{SCHEMA}"."_communication"
      SET "callLinkPrimaryLinkLabel" = 'Call Recording',
          "callLinkPrimaryLinkUrl" = '{link}',
          "updatedAt" = NOW()
      WHERE id = '{row_id}';
    """
    with open("/tmp/_bf_upd.sql", "w") as f:
        f.write(sql)
    subprocess.run(["docker", "cp", "/tmp/_bf_upd.sql", "twenty-db-1:/tmp/_bf_upd.sql"],
                   capture_output=True, timeout=10)
    subprocess.run(["docker", "exec", "twenty-db-1", "psql", "-U", "twenty", "-d", "default",
                    "-f", "/tmp/_bf_upd.sql"], capture_output=True, text=True, timeout=30)

# ---------- main ----------
def main():
    # Collect all mp3s from both folders
    all_mp3 = []
    for label, fid in DRIVE_FOLDERS.items():
        log(f"Listing mp3s in {label}...")
        files = walk_mp3s(fid, prefix=label)
        log(f"  {len(files)} mp3 files")
        all_mp3.extend(files)

    log(f"Total mp3 files to process: {len(all_mp3)}")

    # Bulk-load CRM rows once (fast)
    log("Loading June-18+ unlinked CRM rows...")
    crm_rows = load_crm_rows()
    log(f"  {len(crm_rows)} CRM rows loaded")

    if args.limit:
        all_mp3 = all_mp3[:args.limit]
        log(f"Limited to {len(all_mp3)} for this run")

    linked = 0
    skipped = 0
    for name, link, path in all_mp3:
        parsed = parse_june_filename(name)
        if not parsed:
            # Either pre-June-18 (out of scope) or genuinely unparseable.
            skipped += 1
            continue

        # Direction: June files are agent-initiated (OUTBOUND) since emp=our agent.
        direction = "OUTBOUND"
        dt_utc = parsed["dt_utc"]

        rows = match_crm_bulk(dt_utc, direction, crm_rows)
        if len(rows) != 1:
            log(f"SKIP (ambiguous: {len(rows)} rows) {name} | emp={parsed['emp']} client={parsed['client']} {parsed['dt_ist']}")
            skipped += 1
            continue

        row = rows[0]
        if args.dry_run:
            log(f"[dry-run] {name} -> CRM {row['id']}")
            linked += 1
            continue

        update_crm(row["id"], link)
        linked += 1
        # Remove from in-memory set so we don't double-link if a 2nd file matches
        crm_rows.remove(row)
        log(f"OK {name} -> CRM {row['id']} | {link}")

    log(f"DONE. Linked: {linked}, Skipped: {skipped}")

if __name__ == "__main__":
    main()
