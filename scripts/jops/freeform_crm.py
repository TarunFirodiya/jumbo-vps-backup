#!/usr/bin/env python3
"""
Free-form lead CRM write layer (JUM-700 Phase 6) — dry-run first.

Reads parsed free-form leads (from freeform_parser.parse_message) and plans
Person → Seller → Property creation via the Twenty GraphQL API.

DRY-RUN BY DEFAULT. No writes unless --live is passed.

Rules (agreed with Rushabh, Sep 22 2026):
- Building: EXACT (case/whitespace-normalized) match only. No match -> property
  still created with all captured fields, buildingId empty, flagged in receipt.
- Only certain fields written; unknown/missing reported in the receipt.
- Seller dedup: same person + same matched building + same BHK -> reuse seller.
  NULL-URL existing sellers are never dedup matches.
- Identity fields (name/phone) never auto-overwritten.
"""
import json, re, sys, subprocess, urllib.request
from pathlib import Path

sys.path.insert(0, '/opt/jops')
from freeform_parser import parse_message

CRM = "http://127.0.0.1:3000/graphql"
API_KEY = Path("/root/.twenty/api_key.txt").read_text().strip()

BHK_CONFIG = {1: 'OPT1_BHK', 2: 'OPT2_BHK', 3: 'OPT3_BHK', 4: 'OPT4_BHK', 4.5: 'OPT4_5_BHK', 5: 'OPT5_BHK'}


def gql(query, variables=None):
    body = json.dumps({'query': query, 'variables': variables or {}}).encode()
    req = urllib.request.Request(CRM, data=body, headers={
        'Authorization': 'Bearer ' + API_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if out.get('errors'):
        raise RuntimeError(str(out['errors'])[:500])
    return out['data']


def norm_name(s):
    return re.sub(r'\s+', ' ', (s or '')).strip().lower()


def find_building_exact(guess):
    """Exact (case/space-normalized) match via targeted query — no bulk-load cap."""
    first_word = re.escape((guess or '').split()[0]) if guess else ''
    data = gql(
        'query($w: String!) { buildings(first: 200, filter: { deletedAt: { is: NULL }, '
        'name: { ilike: $w } }) { edges { node { id name } } } }',
        {'w': '%' + first_word + '%'})
    hits = [e['node'] for e in data['buildings']['edges']
            if norm_name(e['node']['name']) == norm_name(guess)]
    if not hits:
        return None
    if len(hits) > 1:
        return ('DUP', [h['name'] for h in hits])
    return (hits[0]['id'], hits[0]['name'])


def find_person_by_phone(phone10):
    data = gql(
        'query($p: String!) { people(first: 3, filter: { deletedAt: { is: NULL }, '
        'phones: { primaryPhoneNumber: { eq: $p } } }) { edges { node { id '
        'name { firstName lastName } } } } }', {'p': phone10})
    edges = data['people']['edges']
    return edges[0]['node'] if edges else None


def find_sellers_for_person(person_id):
    data = gql(
        'query($id: UUID!) { sellers(first: 50, filter: { deletedAt: { is: NULL }, '
        'personId: { eq: $id } }) { edges { node { id name onboardingStatus '
        'sourceUrlPrimaryLinkUrl } } } }', {'id': person_id})
    return [e['node'] for e in data['sellers']['edges']]


def plan_lead(text):
    """Pure dry-run plan for one message. Returns a plan dict; no writes."""
    parsed = parse_message(text)
    if parsed.get('routed') == 'url_track':
        return {'routed': 'url_track'}
    cap, unknown, missing = parsed['captured'], parsed['unknown'], list(parsed['missing'])
    plan = {'routed': 'freeform', 'captured': cap, 'unknown': unknown,
            'missing': missing, 'steps': [], 'warnings': []}

    # building exact match
    building_id = None
    if 'building' in cap:
        m = find_building_exact(cap['building'])
        if m is None:
            plan['warnings'].append(f"Building '{cap['building']}' — no exact match in CRM; property would be created WITHOUT buildingId, flagged for confirmation")
            plan['building_status'] = 'NO_MATCH'
        elif isinstance(m, tuple) and m[0] == 'DUP':
            plan['warnings'].append(f"Building '{cap['building']}' — multiple exact matches {m[1]}; needs human pick")
            plan['building_status'] = 'DUP'
        else:
            building_id, building_name = m
            plan['building_status'] = 'MATCHED'
            plan['building_crm'] = building_name
            plan['building_id'] = building_id
    else:
        plan['building_status'] = 'ABSENT'
        plan['warnings'].append('No building identified in message')

    # person lookup
    person = find_person_by_phone(cap['phone']) if 'phone' in cap else None
    if person:
        crm_name = (person['name']['firstName'] + ' ' + (person['name']['lastName'] or '')).strip()
        plan['steps'].append(f"PERSON: reuse existing {person['id']} ({crm_name})")
        plan['person_id'] = person['id']
        if cap.get('name') and norm_name(cap['name']) != norm_name(crm_name):
            plan['warnings'].append(f"Name mismatch: lead says '{cap['name']}', CRM person is '{crm_name}' — possible recycled number; flag for review, do NOT overwrite")
    else:
        plan['steps'].append(f"PERSON: create {cap.get('name','?')} / {cap.get('phone','?')}")

    # seller dedup: same person + matched building + same bhk
    if person and building_id:
        sellers = find_sellers_for_person(person['id'])
        plan['existing_sellers'] = [(s['id'], s['onboardingStatus']) for s in sellers]
        plan['steps'].append(
            f"SELLER: dedup check vs {len(sellers)} existing seller(s) for this person "
            "(match = same building + same BHK); would create new seller if no match")
    else:
        plan['steps'].append("SELLER: create (onboardingStatus=IDENTIFIED, stage=NEW_ENQUIRY)")

    # property plan
    if building_id and ('bhk' in cap or 'area_sqft' in cap or 'price_rupees' in cap):
        fields = []
        if 'bhk' in cap: fields.append(f"config={BHK_CONFIG.get(cap['bhk'])}")
        if 'area_sqft' in cap: fields.append(f"sqft={cap['area_sqft']}")
        if 'carpet_sqft' in cap: fields.append(f"carpet={cap['carpet_sqft']}")
        if 'floor' in cap: fields.append(f"floor={cap['floor']}")
        if 'furnishing' in cap: fields.append(f"furnishing={cap['furnishing']}")
        if 'facing' in cap: fields.append(f"facing={cap['facing']}")
        if 'unit_no' in cap: fields.append(f"flat={cap['unit_no']}")
        if 'price_rupees' in cap: fields.append(f"price=₹{cap['price_rupees']:,}")
        plan['steps'].append("PROPERTY: create DRAFT with " + ', '.join(fields))
    else:
        reason = 'no matched building' if not building_id else 'no property fields'
        plan['steps'].append(f"PROPERTY: NOT created ({reason}) — seller + note only")

    plan['steps'].append("NOTE: raw message stored on seller")
    return plan


def render_receipt(plan):
    """The Slack thread reply preview."""
    cap = plan['captured']
    lines = [f"✅ *Seller:* {cap.get('name','?')} ({cap.get('phone','?')})"]
    if plan['building_status'] == 'MATCHED':
        lines.append(f"✅ *Building:* {plan['building_crm']} (exact match)")
    prop_bits = []
    if 'bhk' in cap: prop_bits.append(f"{cap['bhk']} BHK")
    if 'floor' in cap: prop_bits.append(f"floor {cap['floor']}")
    if 'area_sqft' in cap: prop_bits.append(f"{cap['area_sqft']} sqft")
    if 'carpet_sqft' in cap: prop_bits.append(f"carpet {cap['carpet_sqft']}")
    if 'facing' in cap: prop_bits.append(f"{cap['facing'].title()} facing")
    if 'furnishing' in cap: prop_bits.append(cap['furnishing'].replace('_', ' ').title())
    if 'unit_no' in cap: prop_bits.append(f"unit {cap['unit_no']}")
    if 'price_rupees' in cap: prop_bits.append(f"₹{cap['price_rupees']/10000000:.2f} Cr")
    lines.append("✅ *Property:* " + (' · '.join(prop_bits) if prop_bits else 'nothing captured'))
    for w in plan['warnings']:
        lines.append(f"⚠️ {w}")
    for label, raw in plan['unknown']:
        lines.append(f"⚠️ *Couldn't place:* `{raw}` ({label})")
    if plan['missing']:
        lines.append(f"❌ *Missing:* {', '.join(plan['missing'])}")
    return '\n'.join(lines)


if __name__ == '__main__':
    msgs = json.load(open(sys.argv[1]))
    for m in msgs:
        txt = m.get('text', '')
        if 'has joined the channel' in txt:
            continue
        plan = plan_lead(txt)
        print('=' * 66)
        print('ts', m['ts'])
        if plan['routed'] == 'url_track':
            print('-> URL track'); continue
        print('--- PLAN (dry-run, no writes) ---')
        for s in plan['steps']:
            print(' •', s)
        print('--- THREAD REPLY PREVIEW ---')
        print(render_receipt(plan))
        print()
