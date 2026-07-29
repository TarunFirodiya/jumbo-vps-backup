#!/usr/bin/env python3
"""
Backfill Callyzer call logs as Communication records in Twenty CRM.
Date range: Jun 25 2026 → Jul 27 2026.
Fix: column names MUST be double-quoted for camelCase.
"""
import subprocess, json, time, sys, uuid
from datetime import datetime, timezone

CALYZER_TOKEN = "f6377a30-74b6-4a99-954a-d4b674bf22cf"
SCHEMA = "workspace_1l3urgumjmspnjxohclmfz6fx"
PAGE_SIZE = 50

def run(cmd, check=True, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nstderr: {r.stderr[:500]}")
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def psql(sql):
    out, err, code = run(["docker", "exec", "twenty-db-1", "psql",
                          "-U", "twenty", "-d", "default",
                          "-t", "-A", "-c", sql], check=False, timeout=60)
    return out

def esc(val):
    if val is None: return "NULL"
    s = str(val).replace("\\", "\\\\").replace("'", "''").replace("\n", "\\n")
    return f"E'{s}'"

def esc_id(val):
    return f"'{val}'" if val else "NULL"

# ── Step 1: Fetch all calls from Callyzer ──────────────
print("=== Step 1: Fetching call records from Callyzer ===")

STATE = "/tmp/_callyzer_state.json"
try:
    with open(STATE) as f:
        s = json.load(f)
        all_calls = s["calls"]
        print(f"Resuming from saved state: {len(all_calls)} calls")
except (FileNotFoundError, json.JSONDecodeError):
    all_calls = []

if not all_calls:
    start_ts = 1782325800
    end_ts = 1785176940
    chunk_dur = 3 * 86400
    chunks = []
    cs = start_ts
    while cs < end_ts:
        ce = min(cs + chunk_dur, end_ts)
        chunks.append((cs, ce))
        cs = ce
    for ci, (cf, ct) in enumerate(chunks):
        print(f"  Chunk {ci+1}/{len(chunks)}: ", end="", flush=True)
        page = 1
        cc = []
        while True:
            for rtry in range(3):
                resp = subprocess.run(
                    ["curl", "-s", "-m", "30", "-X", "POST",
                     "https://api1.callyzer.co/api/v2.1/call-log/history",
                     "-H", "content-type: application/json",
                     "-H", f"Authorization: Bearer {CALYZER_TOKEN}",
                     "-d", json.dumps({"synced_from": cf, "synced_to": ct,
                                        "page_no": page, "page_size": PAGE_SIZE})],
                    capture_output=True, text=True, timeout=35)
                try: data = json.loads(resp.stdout)
                except: data = {}
                if "result" in data and isinstance(data.get("result"), list):
                    break
                time.sleep(3)
            if "result" not in data or not isinstance(data.get("result"), list):
                break
            cc.extend(data["result"])
            if len(cc) >= data.get("total_records", 0):
                break
            page += 1
            time.sleep(1.2)
        print(f"{len(cc)} calls")
        all_calls.extend(cc)
        with open(STATE, "w") as f: json.dump({"calls": all_calls}, f)

print(f"  Total: {len(all_calls)}")

# ── Step 2: Bulk phone lookups ─────────────────────────
print("\n=== Step 2: Bulk phone→person / phone→wsm lookups ===")
client_phones = sorted(set(c.get("client_number","").strip() for c in all_calls if c.get("client_number")))
emp_phones = sorted(set(c.get("emp_number","").strip() for c in all_calls if c.get("emp_number")))
print(f"  Client numbers: {len(client_phones)}, Emp numbers: {len(emp_phones)}")

# Bulk person lookup
clause = ", ".join(f"'{p}'" for p in client_phones)
out = psql(f"""
SELECT id, "phonesPrimaryPhoneNumber" AS phone
FROM "{SCHEMA}"."person"
WHERE "deletedAt" IS NULL
AND "phonesPrimaryPhoneNumber" IN (SELECT UNNEST(ARRAY[{clause}]));
""")
person_map = {}
for line in out.split("\n"):
    line = line.strip()
    if line and "|" in line:
        parts = line.split("|", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            person_map[parts[1].strip()] = parts[0].strip()
print(f"  Person matches: {len(person_map)}/{len(client_phones)}")

# Bulk workspace member lookup
clause2 = ", ".join(f"'{p}'" for p in emp_phones)
out = psql(f"""
SELECT id, "officePhonePrimaryPhoneNumber" AS phone
FROM "{SCHEMA}"."workspaceMember"
WHERE "deletedAt" IS NULL
AND "officePhonePrimaryPhoneNumber" IN (SELECT UNNEST(ARRAY[{clause2}]));
""")
wsm_map = {}
for line in out.split("\n"):
    line = line.strip()
    if line and "|" in line:
        parts = line.split("|", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            wsm_map[parts[1].strip()] = parts[0].strip()
print(f"  WSM matches: {len(wsm_map)}/{len(emp_phones)}")

# ── Step 3: Dedup ──────────────────────────────────────
print("\n=== Step 3: Dedup ===")
out = psql(f"""
SELECT "messageId" FROM "{SCHEMA}"."_communication"
WHERE "communicationType" = 'CALL'
AND "createdAt" >= '2026-06-25' AND "createdAt" < '2026-07-28'
AND "messageId" IS NOT NULL AND "messageId" != '';
""")
existing_ids = set()
for line in out.split("\n"):
    line = line.strip()
    if line and line != "(0 rows)":
        existing_ids.add(line)
print(f"  Existing with messageId: {len(existing_ids)}")

# ── Step 4: Generate SQL (QUOTED column names!) ──
print("\n=== Step 4: Generating SQL ===")

COLUMNS = '''"id", "createdAt", "updatedAt", "deletedAt",
 "communicationType", duration, "timestamp",
 summary, name,
 "createdBySource", "createdByName",
 "updatedBySource", "updatedByName",
 "personId", direction,
 "callLinkPrimaryLinkUrl", "callLinkPrimaryLinkLabel",
 "messageId", "assignedagentId", position'''

inserts = []
skipped = 0

for c in all_calls:
    cid = c.get("id", "")
    if cid in existing_ids:
        skipped += 1
        continue

    cn = (c.get("client_number") or "").strip()
    en = (c.get("emp_number") or "").strip()
    pid = person_map.get(cn, "")
    wid = wsm_map.get(en, "")

    ct_raw = (c.get("call_type") or "").upper()
    direction = "INBOUND" if "INCOMING" in ct_raw or ct_raw == "INBOUND" else "OUTBOUND"
    dur = c.get("duration", 0)

    crm_ts = c.get("crm_timestamp") or ""
    if not crm_ts and c.get("call_date") and c.get("call_time"):
        try:
            dt = datetime.strptime(f"{c['call_date']} {c['call_time']}", "%Y-%m-%d %H:%M:%S")
            crm_ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            crm_ts = ""
    if not crm_ts:
        skipped += 1
        continue

    cname = (c.get("client_name") or "").strip() or "Unknown"
    aname = (c.get("emp_name") or "").strip() or "Unknown"
    ctime = (c.get("call_time") or "").strip()
    name = f"📞{cname} x {aname} - {ctime}" if ctime else f"📞{cname} x {aname}"

    rec_url = (c.get("call_recording_url") or "").strip()
    summary = f"Call recording: {rec_url}" if rec_url else "No call recording available for this log."
    rid = str(uuid.uuid4())

    sql = f"""
    INSERT INTO "{SCHEMA}"."_communication"
    ({COLUMNS})
    VALUES
    ('{rid}', NOW(), NOW(), NULL,
     'CALL', {dur}, E'{crm_ts}'::timestamptz,
     {esc(summary)}, {esc(name)},
     'WORKFLOW', 'Operator (backfill)',
     'WORKFLOW', 'Operator (backfill)',
     {esc_id(pid)}, '{direction}',
     {esc(rec_url)}, '',
     {esc(cid)},
     {esc_id(wid)}, 0);
    """
    inserts.append(sql)

print(f"  To insert: {len(inserts)}")
print(f"  Skipped: {skipped}")

if not inserts:
    print("\nNothing to do.")
    sys.exit(0)

# ── Step 5: Execute ────────────────────────────────────
print("\n=== Step 5: Executing SQL inserts ===")

BATCH = 200
total_ok = 0

for bi in range(0, len(inserts), BATCH):
    batch = inserts[bi:bi+BATCH]
    sql_content = "\n".join(batch)
    with open("/tmp/_cb.sql", "w") as f:
        f.write(sql_content)
    run(["docker", "cp", "/tmp/_cb.sql", "twenty-db-1:/tmp/_cb.sql"])
    out, err, code = run(
        ["docker", "exec", "twenty-db-1", "psql",
         "-U", "twenty", "-d", "default", "-f", "/tmp/_cb.sql"],
        check=False, timeout=90)
    if code != 0:
        print(f"  Batch {bi//BATCH+1} FAILED:\n{err[:500]}")
        break
    total_ok += len(batch)
    print(f"  Batch {bi//BATCH+1}/{(len(inserts)-1)//BATCH+1}: {len(batch)} (total: {total_ok})")
    run(["docker", "exec", "twenty-db-1", "rm", "-f", "/tmp/_cb.sql"], check=False, timeout=5)

print(f"\n=== DONE ===")
print(f"Fetched: {len(all_calls)} | Inserted: {total_ok} | Skipped: {skipped}")
print(f"Person matches: {len(person_map)}/{len(client_phones)}")
print(f"WSM matches: {len(wsm_map)}/{len(emp_phones)}")
