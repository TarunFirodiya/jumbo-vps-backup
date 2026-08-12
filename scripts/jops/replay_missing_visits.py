#!/usr/bin/env python3
import json, os, subprocess, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
from dotenv import dotenv_values

CFG = dotenv_values('/opt/jumbo-webhook-proxy/.env')
BASE = CFG['SUPABASE_URL'] + '/rest/v1'
KEY = CFG['SUPABASE_SERVICE_KEY']
HEADERS = {'apikey': KEY, 'Authorization': 'Bearer ' + KEY}
IST = timezone(timedelta(hours=5, minutes=30))

def supa(table, filters, select='*'):
    parts = [('select', select)] + list(filters.items())
    url = BASE + '/' + table + '?' + '&'.join(
        urllib.parse.quote(k) + '=' + urllib.parse.quote(str(v), safe='.,:')
        for k, v in parts
    )
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def sql(query):
    r = subprocess.run(
        ['docker', 'exec', 'twenty-db-1', 'psql', '-U', 'twenty', '-d', 'default', '-t', '-A', '-F', '|', '-c', query],
        capture_output=True, text=True, timeout=60, check=True)
    return r.stdout.splitlines()

def digits(value):
    return ''.join(c for c in (value or '') if c.isdigit())[-10:]

def post(record):
    body = json.dumps({'type': 'INSERT', 'record': record}).encode()
    req = urllib.request.Request(
        'http://127.0.0.1:3001/api/supabase/visit',
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read().decode()

# Read all source visits since 1 July, then keep only rows still lacking write-back.
source = []
for offset in range(0, 10000, 1000):
    batch = supa('visit', {
        'created_at': 'gte.2026-07-01T00:00:00+05:30',
        'order': 'created_at.asc', 'limit': 1000, 'offset': offset,
    })
    source.extend(x for x in batch if not x.get('internal_id'))
    if len(batch) < 1000:
        break

# Resolve source phone and listing -> CRM property.
people = {}
for row in source:
    table = 'user' if row.get('user_id') else 'external_user'
    uid = row.get('user_id') or row.get('external_user_id')
    if uid and uid not in people:
        found = supa(table, {'id': 'eq.' + uid}, 'id,phone_number')
        people[uid] = found[0] if found else {}
listing_ids = list({r['listing_id'] for r in source if r.get('listing_id')})
listings = {}
for i in range(0, len(listing_ids), 50):
    for row in supa('listing', {'id': 'in.(' + ','.join(listing_ids[i:i+50]) + ')'}, 'id,internal_id'):
        listings[row['id']] = row

# Read current CRM visits with buyer phone and property.
crm_rows = sql('''SELECT v.id, v."scheduledAt", v."propertyId", p."phonesPrimaryPhoneNumber"
FROM "workspace_1l3urgumjmspnjxohclmfz6fx"._visit v
LEFT JOIN "workspace_1l3urgumjmspnjxohclmfz6fx"._buyer b ON b.id=v."buyerProfileId"
LEFT JOIN "workspace_1l3urgumjmspnjxohclmfz6fx".person p ON p.id=b."personId"
WHERE v."deletedAt" IS NULL AND v."createdAt">='2026-07-01';''')
crm = []
for line in crm_rows:
    fields = line.split('|')
    if len(fields) != 4:
        continue
    try:
        when = datetime.fromisoformat(fields[1].replace(' ', 'T')).astimezone(timezone.utc).timestamp()
    except ValueError:
        continue
    crm.append({'id': fields[0], 'when': when, 'property': fields[2], 'phone': digits(fields[3])})

candidates = []
for row in source:
    uid = row.get('user_id') or row.get('external_user_id')
    source_phone = digits(people.get(uid, {}).get('phone_number'))
    property_id = listings.get(row.get('listing_id'), {}).get('internal_id')
    scheduled = datetime.fromisoformat(row['scheduled_at']).replace(tzinfo=IST).astimezone(timezone.utc).timestamp() if row.get('scheduled_at') else None
    matches = [x for x in crm if x['phone'] == source_phone and x['property'] == property_id and scheduled is not None and abs(x['when'] - scheduled) < 5]
    if not matches:
        candidates.append(row)

manifest = '/opt/jops/missing-visits-replay-manifest.json'
with open(manifest, 'w') as f:
    json.dump({'created_at': datetime.now(IST).isoformat(), 'count': len(candidates), 'source_ids': [r['id'] for r in candidates]}, f, indent=2)

if len(candidates) != 15:
    raise SystemExit(f'ABORT: expected exactly 15 unmatched visits, found {len(candidates)}; no replay sent')

results = []
for row in candidates:
    try:
        status, body = post(row)
        results.append({'source_id': row['id'], 'http_status': status, 'response': body[:200]})
    except Exception as exc:
        results.append({'source_id': row['id'], 'error': str(exc)})
print(json.dumps({'candidate_count': len(candidates), 'results': results, 'manifest': manifest}, indent=2))
