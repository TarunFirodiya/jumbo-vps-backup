#!/usr/bin/env python3
"""Token-Paid task backfill generator for Jumbo Homes Twenty CRM.

Rebuilt 2026-07-16 from the proven skill `token_paid_backfill.py` content.
Creates + links + assigns the standardized TAT task set
(RESALE=25, ASSIGNMENT=21) on in-scope offers.

Rules (Tarun, 2026-07-10, confirmed 2026-07-16):
  - Anchor due dates on offer.bookingDate (NOT createdAt).
  - Saturdays ARE working days; only Sunday (weekday 6) skipped.
  - Due time 18:00 IST (+05:30).
  - POCs: Harish, Puja, Ramswaroop, Rohith.

Idempotent + resumable + rate-limit safe. Two-step link (createTaskTarget
empty -> updateTaskTarget with bare scalar IDs). assigneeId set on createTask.

Run:
  python3 token_paid_backfill.py                  # all in-scope, resumes via state
  python3 token_paid_backfill.py --dry            # list targets, no writes
  python3 token_paid_backfill.py --only <id> ...  # specific offers
"""
import json, time, urllib.request, datetime, os, sys, argparse

API_KEY = open('/root/.twenty/api_key.txt').read().strip()
URL = 'http://localhost:3000/graphql'
HDR = {'Content-Type': 'application/json', 'Authorization': f'Bearer {API_KEY}'}
STATE_FILE = '/tmp/token_paid_backfill_state.json'

POC = {
    'Harish':     'c744aa41-ef42-4d2c-81af-bda0d71aeeca',
    'Puja':       '5cd4520c-52d8-4c98-9cc0-232ae767192b',
    'Ramswaroop': '8e1fdcfe-8db6-4e50-a7ab-d820f9b95c96',
    'Rohith':     'e897ff71-fcc8-4a10-ba07-9c02080a6a80',
}

RESALE = [
    (1,  'Token Payment +Whatsapp Grp Creation', 'Harish'),
    (2,  'E- khata to be checked if its open', 'Rohith'),
    (2,  'Deal Term Sheet Drafting & Signing', 'Puja'),
    (2,  'Safebuy Payment Collection', 'Puja'),
    (3,  'Legal Due Diligence to be initiated', 'Puja'),
    (5,  'Bank to be selected', 'Puja'),
    (9,  'Legal Due Diligence to be completed', 'Puja'),
    (10, 'Token Release', 'Puja'),
    (10, 'AFS Draft to be shared', 'Ramswaroop'),
    (11, 'Penny test to be completed', 'Puja'),
    (13, 'Estamp Procurement', 'Rohith'),
    (13, 'Stamp duty + Estamp charges collection', 'Puja'),
    (15, 'AFS Signing', 'Ramswaroop'),
    (15, 'Jumbo Fee Collection from the Seller', 'Puja'),
    (16, 'Sale deed to be shared on group', 'Ramswaroop'),
    (21, 'Bank Legal Evaluation', 'Rohith'),
    (25, 'Bank Technical Evaluation', 'Rohith'),
    (30, 'ODV', 'Rohith'),
    (35, 'Docket signing', 'Rohith'),
    (36, 'TDS Payment', 'Harish'),
    (37, 'SRO Slot booking', 'Rohith'),
    (40, 'Sale deed Registration', 'Rohith'),
    (55, 'E Khata Transfer', 'Rohith'),
    (56, 'Property Tax Name Change', 'Rohith'),
    (70, 'BESCOM', 'Rohith'),
]
ASSIGNMENT = [
    (1,  'Token Payment +Whatsapp Grp Creation', 'Harish'),
    (2,  'Deal Term Sheet Drafting & Signing', 'Puja'),
    (2,  'Safebuy Payment Collection', 'Puja'),
    (3,  'Legal Due Diligence to be initiated', 'Puja'),
    (5,  'Bank to be selected', 'Puja'),
    (9,  'Legal Due Diligence to be completed', 'Puja'),
    (10, 'Token Release', 'Puja'),
    (10, 'MOU draft to be shared on group', 'Ramswaroop'),
    (11, 'MOU Draft to be confirmed with the builder', 'Ramswaroop'),
    (13, 'Penny test to be completed', 'Puja'),
    (15, 'Stamp duty + Estamp charges collection', 'Puja'),
    (15, 'Estamp Procurement', 'Rohith'),
    (15, 'MOU Signing', 'Ramswaroop'),
    (16, 'Jumbo Fee Collection from the Seller', 'Puja'),
    (21, 'Assignment Agrement Initiation with the builder', 'Ramswaroop'),
    (25, 'Bank Legal Evaluation', 'Rohith'),
    (27, 'Bank Technical Evaluation', 'Rohith'),
    (35, 'Docket signing', 'Rohith'),
    (36, 'TDS Payment', 'Harish'),
    (37, 'Assignment Agrement to be signed', 'Ramswaroop'),
    (42, 'Loan Disbursement', 'Rohith'),
]

def gql(query, variables, label):
    body = json.dumps({'query': query, 'variables': variables}).encode()
    req = urllib.request.Request(URL, data=body, headers=HDR, method='POST')
    lr = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode())
        except Exception:
            time.sleep(5); continue
        if resp.get('errors'):
            msg = str(resp['errors'])
            if 'LIMIT_REACHED' in msg:
                lr += 1
                if lr > 30: raise RuntimeError(f"{label}: rate-limit exceeded")
                print(f"  [rate-limit] pause 65s ({label})"); time.sleep(65); continue
            raise RuntimeError(f"{label} errors: {resp['errors']}")
        if resp.get('data') is None:
            time.sleep(5); continue
        return resp['data']

def working_day(start, n):
    d = start; add = n - 1
    while add > 0:
        d += datetime.timedelta(days=1)
        if d.weekday() != 6:  # Sunday only skipped; Sat works
            add -= 1
    return d

def linked_info(oid):
    q = """query($oid:ID!){ opportunities(filter:{id:{eq:$oid}}){ edges{ node{ taskTargets{ edges{ node{ task{ id title assignee{ id } } } } } } } } }"""
    d = gql(q, {'oid': oid}, 'linked_info')
    out = {}
    try:
        for e in d['opportunities']['edges'][0]['node']['taskTargets']['edges']:
            t = e['node']['task']
            if t: out[t['title']] = {'id': t['id'], 'assignee': t.get('assignee')}
    except Exception:
        pass
    return out

def offer_meta(oid):
    q = """query($oid:ID!){ opportunities(filter:{id:{eq:$oid}}){ edges{ node{ bookingDate } } } }"""
    return gql(q, {'oid': oid}, 'meta')['opportunities']['edges'][0]['node'].get('bookingDate')

def task_exists(tid):
    return gql("""query($id:ID!){ tasks(filter:{id:{eq:$id}}){ totalCount } }""",
               {'id': tid}, 'exists')['tasks']['totalCount'] > 0

def create_linked(oid, title, due_iso, assignee_id, verify=True):
    tid = gql("""mutation($d:TaskCreateInput!){ createTask(data:$d){ id } }""",
              {'d': {'title': title, 'status': 'TODO', 'dueAt': due_iso, 'assigneeId': assignee_id}},
              f'create:{title}')['createTask']['id']
    if verify and not task_exists(tid):
        raise RuntimeError(f'task {title} not persisted ({tid})')
    tgt = gql("""mutation{ createTaskTarget(data:{}){ id } }""", {}, f'target:{title}')['createTaskTarget']['id']
    gql("""mutation($id:ID!,$d:TaskTargetUpdateInput!){ updateTaskTarget(id:$id,data:$d){ id } }""",
        {'id': tgt, 'd': {'taskId': tid, 'targetOpportunityId': oid}}, f'link:{title}')
    return tid

def run_offer(oid, ttype):
    tpl = RESALE if ttype == 'RESALE' else ASSIGNMENT
    bd = offer_meta(oid)
    if not bd:
        return {'status': 'SKIP_NULL_BOOKING', 'missing': [t for _, t, _ in tpl]}
    anchor = datetime.date.fromisoformat(bd)
    have = linked_info(oid)
    daymap = {t: d for d, t, _ in tpl}; pocmap = {t: p for _, t, p in tpl}
    created = []; updated = []; failed = []
    for title in daymap:
        due = working_day(anchor, daymap[title]).strftime('%Y-%m-%d') + 'T18:00:00+05:30'
        aid = POC[pocmap[title]]
        if title in have:
            gql("""mutation($id:ID!,$d:TaskUpdateInput!){ updateTask(id:$id,data:$d){ id } }""",
                {'id': have[title]['id'], 'd': {'dueAt': due, 'assigneeId': aid}}, f'update:{title}')
            updated.append(title); time.sleep(1.2); continue
        ok = False
        for attempt in range(1, 4):
            try:
                create_linked(oid, title, due, aid, verify=False); ok = True; break
            except Exception:
                if attempt == 3: failed.append(title); break
                time.sleep(3 * attempt)
        if ok: created.append(title)
        time.sleep(2.0)
    for _ in range(3):
        final = linked_info(oid)
        miss = [t for t in daymap if t not in final]
        if not miss: break
        for title in miss:
            due = working_day(anchor, daymap[title]).strftime('%Y-%m-%d') + 'T18:00:00+05:30'
            try:
                create_linked(oid, title, due, POC[pocmap[title]], verify=True); created.append(title)
            except Exception:
                pass
            time.sleep(2.0)
    final = linked_info(oid)
    missing = [t for t in daymap if t not in final]
    return {'status': 'OK' if not missing else 'PARTIAL', 'bookingDate': bd,
            'created': created, 'updated': updated, 'failed': failed,
            'missing': missing, 'expected': len(tpl)}

def list_targets():
    q = """query{ opportunities(filter:{and:[{or:[{transactionType:{eq:RESALE}},{transactionType:{eq:ASSIGNMENT}}]},{or:[{stage:{eq:TOKEN_PAID}},{stage:{eq:TERM_SHEET_SIGNED}},{stage:{eq:AFS_MOU_SIGNED}}]}]}){ edges{ node{ id transactionType name bookingDate } } } }"""
    return [(e['node']['id'], e['node']['transactionType'], e['node']['name'])
            for e in gql(q, {}, 'list')['opportunities']['edges']]

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--only', nargs='+', default=[])
    args = ap.parse_args()
    deals = list_targets()
    if args.only:
        deals = [d for d in deals if d[0] in args.only]
    if args.dry:
        for oid, tt, name in deals:
            print(f"{name} ({tt}) {oid}")
        print(f"\nDRY: {len(deals)} targets, no writes."); sys.exit(0)
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    print(f"TOTAL IN-SCOPE: {len(deals)} | already OK: {sum(1 for d in deals if state.get(d[0])=='OK')}")
    for oid, ttype, label in deals:
        if state.get(oid) == 'OK':
            print(f"SKIP(done) {label}"); continue
        r = run_offer(oid, ttype); r['label'] = label; state[oid] = r['status']
        json.dump(state, open(STATE_FILE, 'w'), indent=2)
        bd = r.get('bookingDate', 'NULL')
        print(f"{label} ({ttype}): {r['status']} booking={bd} "
              f"created={len(r.get('created',[]))} updated={len(r.get('updated',[]))} "
              f"failed={len(r.get('failed',[]))} missing={r.get('missing',[])}")
    done = sum(1 for d in deals if state.get(d[0]) == 'OK')
    part = [d[2] for d in deals if state.get(d[0]) == 'PARTIAL']
    print(f"\n=== DONE: {done}/{len(deals)} OK | partial={len(part)} ===")
    if part: print("PARTIAL:", part)
