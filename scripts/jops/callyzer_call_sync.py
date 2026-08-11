#!/usr/bin/env python3
"""Idempotent Callyzer v2.2 -> Twenty Communication sync.

Modes:
  --live       write only Callyzer IDs not already in CRM messageId
  --dry-run    fetch and compare only; no CRM writes
  --daily      reconcile the previous IST calendar day
Default live window is now minus 20 minutes through now.
"""
import argparse, datetime as dt, json, re, subprocess, sys, time, urllib.error, urllib.request, uuid
from collections import Counter

SCHEMA = "workspace_1l3urgumjmspnjxohclmfz6fx"
API = "https://api1.callyzer.co/api/v2.2/call-log/history"
TOKEN_SOURCE = "/opt/jops/callyzer_recording_sync.py"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
PAGE_SIZE = 99

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--daily", action="store_true")
ap.add_argument("--from-epoch", type=int)
ap.add_argument("--to-epoch", type=int)
args = ap.parse_args()

def get_token():
    text = open(TOKEN_SOURCE).read()
    m = re.search(r'CALYZER_TOKEN\s*=\s*["\']([^"\']+)', text)
    if not m or not m.group(1) or "***" in m.group(1):
        raise RuntimeError("Callyzer token not available from configured source")
    return m.group(1)

def psql(sql, timeout=120):
    r = subprocess.run(["docker", "exec", "-i", "twenty-db-1", "psql", "-U", "twenty", "-d", "default", "-t", "-A"], input=sql, text=True, capture_output=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:1000])
    return r.stdout

def sqlstr(v):
    if v is None: return "NULL"
    s = str(v).replace("\\", "\\\\").replace("'", "''").replace("\n", "\\n")
    return "E'" + s + "'"

def digits(v):
    return "".join(c for c in str(v or "") if c.isdigit())

def fetch(method, start, end, token):
    result = []
    page = 1
    while True:
        body = {"synced_from": start, "synced_to": end, "page_no": page, "page_size": PAGE_SIZE, "call_method": method, "call_mode": "Voice"}
        last = None
        for attempt in range(6):
            if page > 1 or attempt > 0: time.sleep(2.2)
            req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    status = resp.status; payload = json.load(resp); last = None; break
            except urllib.error.HTTPError as e:
                status = e.code
                try: payload = json.load(e)
                except Exception: payload = {}
                last = f"HTTP {status}: {payload.get('message', '')}"
                if status not in (429, 500, 502, 503, 504): raise RuntimeError(last)
            except Exception as e:
                last = repr(e); status = 599; payload = {}
            time.sleep(3 + attempt * 2)
        if last is not None: raise RuntimeError(f"{method} page {page}: {last}")
        rows = payload.get("result") or []
        if not isinstance(rows, list): raise RuntimeError(f"{method} page {page}: invalid result")
        result.extend(rows)
        total = int(payload.get("total_records") or 0)
        if len(rows) < PAGE_SIZE or len(result) >= total: break
        page += 1
    return result

def window():
    now = dt.datetime.now(IST)
    if args.from_epoch is not None: start = args.from_epoch
    elif args.daily:
        yesterday = now.date() - dt.timedelta(days=1)
        start = int(dt.datetime.combine(yesterday, dt.time.min, IST).timestamp())
    else: start = int((now - dt.timedelta(minutes=20)).timestamp())
    if args.to_epoch is not None: end = args.to_epoch
    elif args.daily:
        yesterday = now.date() - dt.timedelta(days=1)
        end = int(dt.datetime.combine(yesterday + dt.timedelta(days=1), dt.time.min, IST).timestamp())
    else: end = int(now.timestamp())
    return start, end

def main():
    start, end = window(); token = get_token()
    by_id = {}
    for method in ("PhoneCall", "WhatsAppCall"):
        for row in fetch(method, start, end, token):
            cid = row.get("id")
            if cid:
                row = dict(row); row["call_method"] = "PHONE" if method == "PhoneCall" else "WHATSAPP"; by_id[cid] = row
    ids = list(by_id)
    existing = set()
    if ids:
        vals = ",".join(sqlstr(x) for x in ids)
        out = psql(f'''SELECT "messageId" FROM "{SCHEMA}"."_communication" WHERE "communicationType"='CALL' AND "deletedAt" IS NULL AND "messageId" IN ({vals});''')
        existing = {x.strip() for x in out.splitlines() if x.strip()}
    missing = [by_id[x] for x in ids if x not in existing]
    print(json.dumps({"mode": "dry-run" if args.dry_run else ("daily" if args.daily else "incremental"), "from_ist": dt.datetime.fromtimestamp(start, IST).isoformat(), "to_ist": dt.datetime.fromtimestamp(end, IST).isoformat(), "source_unique": len(ids), "existing": len(existing), "new": len(missing), "methods": dict(Counter(x.get("call_method") for x in by_id.values())), "no_writes": args.dry_run}, separators=(",", ":")))
    if args.dry_run or not missing: return
    phones = sorted({digits(x.get("client_number")) for x in missing if digits(x.get("client_number"))})
    emps = sorted({digits(x.get("emp_number")) for x in missing if digits(x.get("emp_number"))})
    def inlist(xs): return ",".join(sqlstr(x) for x in xs) or "NULL"
    persons = {}
    for line in psql(f'''SELECT id,"phonesPrimaryPhoneNumber" FROM "{SCHEMA}"."person" WHERE "deletedAt" IS NULL AND "phonesPrimaryPhoneNumber" IN ({inlist(phones)});''').splitlines():
        p=line.split("|",1)
        if len(p)==2: persons[digits(p[1])] = p[0]
    members = {}
    for line in psql(f'''SELECT id,"officePhonePrimaryPhoneNumber" FROM "{SCHEMA}"."workspaceMember" WHERE "deletedAt" IS NULL AND "officePhonePrimaryPhoneNumber" IN ({inlist(emps)});''').splitlines():
        p=line.split("|",1)
        if len(p)==2: members[digits(p[1])] = p[0]

    # Create a single placeholder Person for each unmatched client phone.
    # The unique index covers phone + country code + calling code. The
    # NOT EXISTS guard also prevents duplicate placeholders on later runs.
    missing_phones = sorted({digits(x.get("client_number")) for x in missing if digits(x.get("client_number")) and digits(x.get("client_number")) not in persons})
    if missing_phones:
        person_sql = []
        for phone in missing_phones:
            person_sql.append(f'''INSERT INTO "{SCHEMA}"."person" ("id","createdAt","updatedAt","deletedAt","nameFirstName","nameLastName","phonesPrimaryPhoneNumber","phonesPrimaryPhoneCountryCode","phonesPrimaryPhoneCallingCode") SELECT {sqlstr(str(uuid.uuid4()))},NOW(),NOW(),NULL,'undefined',NULL,{sqlstr(phone)},'IN','+91' WHERE NOT EXISTS (SELECT 1 FROM "{SCHEMA}"."person" WHERE "deletedAt" IS NULL AND "phonesPrimaryPhoneNumber"={sqlstr(phone)});''')
        psql("BEGIN;\n"+"\n".join(person_sql)+"\nCOMMIT;", timeout=180)
        # Refresh the map so every new Communication links to its Person.
        for line in psql(f'''SELECT id,"phonesPrimaryPhoneNumber" FROM "{SCHEMA}"."person" WHERE "deletedAt" IS NULL AND "phonesPrimaryPhoneNumber" IN ({inlist(missing_phones)});''').splitlines():
            p=line.split("|",1)
            if len(p)==2: persons[digits(p[1])] = p[0]
    statements=[]
    for c in missing:
        raw_type=(c.get("call_type") or "").upper()
        direction={"INCOMING":"INBOUND","OUTGOING":"OUTBOUND","MISSED":"MISSED","REJECTED":"REJECTED"}.get(raw_type, raw_type or "OUTBOUND")
        try: duration=int(float(c.get("duration") or 0))
        except Exception: duration=0
        timestamp=None
        if c.get("call_date") and c.get("call_time"):
            try: timestamp=dt.datetime.strptime(c["call_date"]+" "+c["call_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).astimezone(dt.timezone.utc).isoformat().replace("+00:00","Z")
            except Exception: pass
        if not timestamp: continue
        name=f"📞{(c.get('client_name') or 'Unknown').strip() or 'Unknown'} x {(c.get('emp_name') or 'Unknown').strip() or 'Unknown'} - {c.get('call_time') or ''}"
        url=(c.get("call_recording_url") or "").strip()
        summary=f"Call recording: {url}" if url else "No call recording available for this log."
        cid=c["id"]
        statements.append(f'''INSERT INTO "{SCHEMA}"."_communication" ("id","createdAt","updatedAt","deletedAt","communicationType",duration,"timestamp",summary,name,"createdBySource","createdByName","updatedBySource","updatedByName","personId",direction,"callLinkPrimaryLinkUrl","callLinkPrimaryLinkLabel","messageId","assignedagentId","callMethod",position) SELECT {sqlstr(str(uuid.uuid4()))},NOW(),NOW(),NULL,'CALL',{duration},{sqlstr(timestamp)},{sqlstr(summary)},{sqlstr(name)},'WORKFLOW','Callyzer VPS sync','WORKFLOW','Callyzer VPS sync',{sqlstr(persons.get(digits(c.get('client_number'))))},'{direction}',{sqlstr(url)},'Call Recording',{sqlstr(cid)},{sqlstr(members.get(digits(c.get('emp_number'))))},'{c['call_method']}',0 WHERE NOT EXISTS (SELECT 1 FROM "{SCHEMA}"."_communication" WHERE "communicationType"='CALL' AND "deletedAt" IS NULL AND "messageId"={sqlstr(cid)});''')
    for i in range(0, len(statements), 100):
        psql("BEGIN;\n"+"\n".join(statements[i:i+100])+"\nCOMMIT;", timeout=180)
    print(json.dumps({"inserted_attempted":len(statements),"person_matches":len(persons),"placeholder_persons_created":len(missing_phones),"agent_matches":len(members)},separators=(",", ":")))

if __name__ == "__main__":
    try: main()
    except Exception as e:
        print("FAILED: "+str(e), file=sys.stderr); sys.exit(1)
