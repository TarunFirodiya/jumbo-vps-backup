#!/usr/bin/env python3
"""
Phase 2b: improved backfill linking June-2..23 Drive recordings to NULL CRM
_communication rows, using BOTH client-phone (filename -> person) and
timestamp+direction matching. Safe: only writes callLinkPrimaryLinkUrl where
it is NULL and a confident single match is found.

Matching priority per Drive recording (filename emp_client_YYYYMMDD_HHMMSS.mp3):
  1. direction = OUTBOUND if emp in AGENT_EMP set, else INBOUND
  2. client -> person_id via phone map
  3. candidate NULL comm rows:
       (a) personId == person_id  AND direction matches AND |ts - rec| <= 120s
       (b) fallback: direction matches AND |ts - rec| <= 60s  (no phone)
     -> if exactly 1 candidate overall -> link
"""
import os, sys, json, subprocess, datetime, re
from pathlib import Path

DRIVE_ROOT = "1Dq75THYRdy9IjFfVM-yutImwbSGO0VuI"
BRIDGE = "/root/.hermes/profiles/operator/skills/productivity/google-workspace/scripts/gws_bridge.py"
GWS = "/root/.hermes/node/lib/node_modules/@googleworkspace/cli/bin/gws"
SCHEMA = "workspace_1l3urgumjmspnjxohclmfz6fx"
STAGE = "/tmp/callyzer_rec"
os.makedirs(STAGE, exist_ok=True)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
UTC = datetime.timezone.utc

# Agent emp numbers observed in the manual Drive tree (our outgoing agents)
AGENT_EMP = {"7349744482","7259146738","7349744484","7760071771","7349744487","7349744480","7338693076"}

import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--limit", type=int, default=None)
ap.add_argument("--since", default="2026-06-02")
ap.add_argument("--until", default="2026-06-24")
args = ap.parse_args()

def log(m):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)

# ---------- Drive listing ----------
def gdrive_list(q, fields="files(id,name,mimeType,webViewLink)"):
    out = subprocess.run(["python3", BRIDGE, "drive", "files", "list", "--params",
                          json.dumps({"q": q, "fields": fields, "pageSize": 200})],
                         capture_output=True, text=True, timeout=60)
    try:
        return json.loads(out.stdout).get("files", [])
    except Exception:
        return []

def walk_mp3s(folder_id, prefix=""):
    results = []
    items = gdrive_list(f"'{folder_id}' in parents")
    for it in items:
        if it.get("mimeType") == "application/vnd.google-apps.folder":
            results.extend(walk_mp3s(it["id"], f"{prefix}/{it['name']}"))
        elif it.get("name", "").lower().endswith(".mp3"):
            results.append((it["name"], it.get("webViewLink", ""), prefix))
    return results

FN_RE = re.compile(r"^(\d+)_(\d+)_(\d{8})_(\d{6})\.mp3$")
def parse_name(name):
    m = FN_RE.match(name)
    if not m:
        return None
    emp, client, ymd, hms = m.groups()
    dt = datetime.datetime.strptime(f"{ymd} {hms}", "%Y%m%d %H%M%S").replace(tzinfo=IST)
    if not (args.since <= ymd <= args.until.replace("-","")):
        return None
    direction = "OUTBOUND" if emp in AGENT_EMP else "INBOUND"
    return {"emp": emp, "client": client, "dt_utc": dt.astimezone(UTC), "direction": direction}

# ---------- CRM bulk load ----------
def psql_file(sql, outfile):
    with open(outfile, "w") as f:
        f.write(sql)
    subprocess.run(["docker", "cp", outfile, "twenty-db-1:/tmp/bf.sql"], capture_output=True, timeout=10)
    r = subprocess.run(["docker", "exec", "twenty-db-1", "psql", "-U", "twenty", "-d", "default",
                        "-t", "-A", "-F", "\t", "-f", "/tmp/bf.sql"], capture_output=True, text=True, timeout=90)
    return r.stdout

def load_phone_map():
    """client phone -> person_id"""
    sql = f'''SELECT "phonesPrimaryPhoneNumber", id FROM "{SCHEMA}"."person" WHERE "phonesPrimaryPhoneNumber" IS NOT NULL;'''
    out = psql_file(sql, "/tmp/_pm.sql")
    m = {}
    for line in out.strip().splitlines():
        if "\t" not in line:
            continue
        ph, pid = line.split("\t")
        m[ph] = pid
    return m

def load_crm_rows():
    sql = f'''
      SELECT id, "personId", direction, "timestamp"
      FROM "{SCHEMA}"."_communication"
      WHERE "timestamp" >= '{args.since}'
        AND "timestamp" < '{args.until}'
        AND "callLinkPrimaryLinkUrl" IS NULL;'''
    out = psql_file(sql, "/tmp/_cr.sql")
    rows = []
    for line in out.strip().splitlines():
        if "\t" not in line:
            continue
        p = line.split("\t")
        if len(p) < 4:
            continue
        try:
            ts = datetime.datetime.strptime(p[3][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except Exception:
            continue
        rows.append({"id": p[0], "personId": p[1] if p[1] != "\\N" else None,
                     "direction": p[2], "ts": ts})
    return rows

def find_candidates(phone_map, crm_rows, rec):
    """Return all candidate CRM rows for a recording (no claiming)."""
    person_id = phone_map.get(rec["client"])
    cands = []
    for r in crm_rows:
        if r["direction"] != rec["direction"]:
            continue
        dt = abs((r["ts"] - rec["dt_utc"]).total_seconds())
        if person_id and r["personId"] == person_id and dt <= 120:
            cands.append((r, dt, "phone"))
        elif dt <= 60:
            cands.append((r, dt, "time"))
    return cands

def update_crm(row_id, link):
    sql = f'''
      UPDATE "{SCHEMA}"."_communication"
      SET "callLinkPrimaryLinkLabel" = 'Call Recording',
          "callLinkPrimaryLinkUrl" = '{link}',
          "updatedAt" = NOW()
      WHERE id = '{row_id}';'''
    psql_file(sql, "/tmp/_upd.sql")

# ---------- main ----------
def main():
    june_folder = "1wjVKKHC8bBAkBk8XueNwsOBrMlXbc7oA"
    july_folder = "1KXXRmMJEIo5ssAu6ZLf85zL5By5UbqIZ"
    all_mp3 = []
    for fid in (june_folder, july_folder):
        log(f"Listing mp3s in {fid}...")
        all_mp3.extend(walk_mp3s(fid))
    log(f"Found {len(all_mp3)} mp3 files in Drive")

    parsed = []
    for name, link, _prefix in all_mp3:
        p = parse_name(name)
        if p:
            p["link"] = link
            parsed.append(p)
    log(f"Parsed {len(parsed)} June-style recordings in window {args.since}..{args.until}")

    phone_map = load_phone_map()
    log(f"Phone map: {len(phone_map)} client->person entries")
    crm_rows = load_crm_rows()
    log(f"NULL comm rows in window: {len(crm_rows)}")

    # Build proposed matches, then resolve conflicts.
    proposals = []  # (rec, row, how)
    for rec in parsed:
        cands = find_candidates(phone_map, crm_rows, rec)
        if not cands:
            continue
        if len(cands) == 1:
            proposals.append((rec, cands[0][0], cands[0][2]))
        else:
            phones = [c for c in cands if c[2] == "phone"]
            if len(phones) == 1:
                proposals.append((rec, phones[0][0], "phone"))

    # Conflict resolution: a CRM row claimed by 2+ recordings is ambiguous -> drop all.
    from collections import defaultdict
    row_to_recs = defaultdict(list)
    for rec, row, how in proposals:
        row_to_recs[row["id"]].append(rec)
    conflicts = {rid for rid, rs in row_to_recs.items() if len(rs) > 1}

    final = [(rec, row, how) for rec, row, how in proposals if row["id"] not in conflicts]
    log(f"Proposed: {len(proposals)} | conflicts dropped: {len(conflicts)} | final: {len(final)}")

    linked = 0
    for rec, row, how in final:
        if args.limit and linked >= args.limit:
            break
        if args.dry_run:
            log(f"[dry-run] {rec['client']} {rec['dt_utc']} {rec['direction']} -> CRM {row['id']} ({how})")
        else:
            update_crm(row["id"], rec["link"])
            linked += 1
            log(f"OK {row['id']} ({how}) <- {rec['link'][:60]}")

    log(f"Done. linked={linked} conflicts={len(conflicts)} unmatched={len(parsed)-len(final)}")

if __name__ == "__main__":
    main()
