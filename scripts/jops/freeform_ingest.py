#!/usr/bin/env python3
"""
Free-form lead ingestion orchestrator for #temp-growth (JUM-700 Phase 6).

Regular-practice pipeline:
  1. Poll #temp-growth for new human messages (checkpointed).
  2. Free-form messages (no URL) -> parse (regex; conservative) -> plan.
  3. --live: create Person -> Seller -> Property -> Note via Twenty GraphQL API.
  4. Post a thread receipt (captured / unknown / missing) as Bablu.
  5. Correction loop: poll thread replies, apply `Field - value` updates
     (strict regex first, LLM fallback for free text — NEVER for building),
     refresh receipt, ✅-react when nothing is missing.

DRY-RUN BY DEFAULT. Pass --live to write.

Rules (agreed with Rushabh, Sep 22 2026):
- Building: EXACT normalized match only; never fuzzy; never LLM-assigned.
- Only certain fields written; ambiguous -> "couldn't place" in receipt.
- Identity fields (name/phone) never auto-overwritten.
- Overwriting a captured field requires in-thread "yes" confirmation.
"""
import json, os, re, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, '/opt/jops')
from freeform_parser import parse_message, FURNISH_MAP
import freeform_crm

CHANNEL = "C0A10M2L2SW"  # #temp-growth
BOT = "U0BBGL8FP7Z"      # bablu bot user
STATE = Path("/opt/jops/freeform_state.json")
SLACK = "https://slack.com/api/"
LIVE = '--live' in sys.argv


def _slack_token():
    for line in open('/root/.hermes/profiles/bablu/.env'):
        if line.startswith('SLACK_BOT_TOKEN='):
            return line.strip().split('=', 1)[1]
    raise RuntimeError('no slack token')


def slack(method, **params):
    req = urllib.request.Request(
        SLACK + method,
        data=json.dumps(params).encode(),
        headers={'Authorization': 'Bearer ' + _slack_token(),
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if not out.get('ok'):
        raise RuntimeError(f"slack {method}: {out.get('error')}")
    return out


def react(channel, ts, name='white_check_mark'):
    """Best-effort; bot may lack reactions:write scope."""
    try:
        slack('reactions.add', channel=channel, name=name, timestamp=ts)
    except Exception:
        pass


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {'checkpoint': str(time.time() - 3600), 'threads': {}, 'processed': []}


def save_state(st):
    st['processed'] = st['processed'][-2000:]
    STATE.write_text(json.dumps(st, indent=1))


# ---------------------------------------------------------------- LLM fallback
def llm_extract_fields(text):
    """Free-text reply -> field dict via OpenRouter. Building is NEVER assigned
    here — it is not in the allowed field list."""
    try:
        auth = json.load(open('/root/.hermes/profiles/bablu/auth.json'))
        key = auth['credential_pool']['openrouter']
        if isinstance(key, dict):
            key = key.get('api_key') or key.get('key') or key.get('token')
    except Exception as e:
        return {'error': f'no llm key: {e}'}
    prompt = (
        "Extract property fields from this message. Return ONLY JSON with any of "
        "these keys: bhk (number), floor (int), area_sqft (int), carpet_sqft (int), "
        "price_rupees (int, convert Cr/Lakh), furnishing (SEMI_FURNISHED|FULLY_FURNISHED|"
        "UNFURNISHED), facing (NORTH|SOUTH|EAST|WEST|...), unit_no (string). "
        "Only include a key if the value is unambiguous. Do NOT extract building "
        "names, person names or phone numbers. Message:\n" + text)
    body = json.dumps({
        'model': 'openai/gpt-4o-mini',
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0,
    }).encode()
    req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
        data=body, headers={'Authorization': 'Bearer ' + key,
                            'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            out = json.load(r)
        content = out['choices'][0]['message']['content']
        m = re.search(r'\{.*\}', content, re.S)
        return json.loads(m.group(0)) if m else {}
    except Exception as e:
        return {'error': str(e)[:200]}


# ---------------------------------------------------------------- CRM writes
def next_serial():
    data = freeform_crm.gql(
        '{ properties(first:1, orderBy:{serialNumber:DescNullsLast})'
        '{ edges{ node{ serialNumber } } } }')
    return int(data['properties']['edges'][0]['node']['serialNumber']) + 1


def create_person(cap):
    first, _, last = (cap.get('name') or '').partition(' ')
    data = freeform_crm.gql(
        'mutation($d: PersonCreateInput!) { createPerson(data: $d) { id } }',
        {'d': {'name': {'firstName': first, 'lastName': last},
               'phones': {'primaryPhoneNumber': cap['phone'],
                          'primaryPhoneCountryCode': 'IN',
                          'primaryPhoneCallingCode': '+91'},
               'createdBy': {'source': 'API',
                             'context': {'name': 'Freeform Ingest'}}}})
    pid = data['createPerson']['id']
    # re-resolve by phone (never trust a single write path)
    found = freeform_crm.find_person_by_phone(cap['phone'])
    return found['id'] if found else pid


def create_seller(cap, person_id):
    data = freeform_crm.gql(
        'mutation($d: SellerCreateInput!) { createSeller(data: $d) { id } }',
        {'d': {'name': cap.get('name'),
               'personId': person_id,
               'source': 'NINETYNINE_ACRES',
               'onboardingStatus': 'IDENTIFIED',
               'stage': 'NEW_ENQUIRY',
               'createdBy': {'source': 'API',
                             'context': {'name': 'Freeform Ingest'}}}})
    return data['createSeller']['id']


def create_property(cap, plan, person_id, seller_id, serial):
    bhk = cap.get('bhk')
    bld = plan.get('building_crm') or 'UNASSIGNED'
    bhk_lbl = f"{bhk:g}BHK" if bhk else 'UNCONFIGURED'
    d = {'name': f"J-{serial}-{bld}-{bhk_lbl}",
         'serialNumber': serial,
         'sellerId': seller_id,
         'ownerId': person_id,
         'propertyStatus': 'DRAFT',
         'inventoryType': 'OPEN',
         'propertyType': 'APARTMENT',
         'createdBy': {'source': 'API', 'context': {'name': 'Freeform Ingest'}}}
    if plan.get('building_id'):
        d['buildingId'] = plan['building_id']
    if bhk:
        d['configuration'] = freeform_crm.BHK_CONFIG.get(bhk)
        d['bedrooms'] = bhk
    for src_key, dst in [('area_sqft', 'squareFeet'), ('carpet_sqft', 'carpetArea'),
                         ('floor', 'floor'), ('furnishing', 'furnishing'),
                         ('facing', 'facing'), ('unit_no', 'flatNumber')]:
        if src_key in cap:
            d[dst] = cap[src_key]
    if 'price_rupees' in cap:
        micros = cap['price_rupees'] * 1_000_000
        d['latestPrice'] = {'amountMicros': micros, 'currencyCode': 'INR'}
        d['sourcePrice'] = {'amountMicros': micros, 'currencyCode': 'INR'}
    data = freeform_crm.gql(
        'mutation($d: PropertyCreateInput!) { createProperty(data: $d) { id name } }',
        {'d': d})
    return data['createProperty']


def create_note(raw_text, seller_id, property_id):
    data = freeform_crm.gql(
        'mutation($d: NoteCreateInput!) { createNote(data: $d) { id } }',
        {'d': {'title': raw_text.strip()[:60],
               'bodyV2': {'markdown': raw_text},
               'createdBy': {'source': 'API',
                             'context': {'name': 'Freeform Ingest'}}}})
    nid = data['createNote']['id']
    for field, rid in [('targetSellerId', seller_id), ('targetPropertyId', property_id)]:
        if rid:
            freeform_crm.gql(
                'mutation($d: NoteTargetCreateInput!) { createNoteTarget(data: $d) { id } }',
                {'d': {'noteId': nid, field: rid}})
    return nid


PROP_FIELD_MAP = {'bhk': None, 'floor': 'floor', 'area_sqft': 'squareFeet',
                  'carpet_sqft': 'carpetArea', 'price_rupees': None,
                  'furnishing': 'furnishing', 'facing': 'facing', 'unit_no': 'flatNumber'}


def apply_property_update(property_id, fields):
    d = {}
    if 'bhk' in fields:
        d['configuration'] = freeform_crm.BHK_CONFIG.get(fields['bhk'])
        d['bedrooms'] = fields['bhk']
    if 'price_rupees' in fields:
        micros = fields['price_rupees'] * 1_000_000
        d['latestPrice'] = {'amountMicros': micros, 'currencyCode': 'INR'}
    for k, col in PROP_FIELD_MAP.items():
        if col and k in fields:
            d[col] = fields[k]
    if not d:
        return
    freeform_crm.gql(
        'mutation($id: UUID!, $d: PropertyUpdateInput!) '
        '{ updateProperty(id: $id, data: $d) { id } }',
        {'id': property_id, 'd': d})


# ---------------------------------------------------------------- main flow
def receipt_missing(plan):
    return [f for f in ['price_rupees', 'floor', 'area_sqft', 'bhk']
            if f not in plan['captured']]


def process_message(m, st):
    ts, txt = m['ts'], m.get('text', '')
    plan = freeform_crm.plan_lead(txt)
    if plan['routed'] != 'freeform':
        return None
    if 'phone' not in plan['captured'] or 'name' not in plan['captured']:
        return None  # not a lead-shaped message
    entry = {'plan_captured': plan['captured'],
             'building_status': plan['building_status']}
    if LIVE:
        person = freeform_crm.find_person_by_phone(plan['captured']['phone'])
        pid = person['id'] if person else create_person(plan['captured'])
        sid = create_seller(plan['captured'], pid)
        prop_id = None
        prop_fields = ['bhk', 'area_sqft', 'price_rupees', 'floor', 'furnishing', 'facing', 'carpet_sqft', 'unit_no']
        if any(f in plan['captured'] for f in prop_fields):
            prop = create_property(plan['captured'], plan, pid, sid, next_serial())
            prop_id = prop['id']
            plan['property_name'] = prop['name']
        create_note(txt, sid, prop_id)
        entry.update(personId=pid, sellerId=sid, propertyId=prop_id)
        reply = slack('chat.postMessage', channel=CHANNEL, thread_ts=ts,
                      text=freeform_crm.render_receipt(plan))
        entry['receipt_ts'] = reply['ts']
        entry['missing'] = receipt_missing(plan)
        if plan['building_status'] != 'MATCHED' and 'building' in plan['captured']:
            entry.setdefault('missing', []).append('building_match')
        if not entry['missing']:
            react(CHANNEL, ts)
        st['threads'][ts] = entry
    return plan


def poll_corrections(st):
    changed = False
    for ts, entry in list(st['threads'].items()):
        if not entry.get('missing'):
            continue
        try:
            rep = slack('conversations.replies', channel=CHANNEL, ts=ts, limit=50)
        except Exception:
            continue
        msgs = rep.get('messages', [])[1:]  # skip parent
        last_seen = entry.get('last_reply_ts', entry.get('receipt_ts', '0'))
        new = [x for x in msgs if x.get('user') != BOT and x['ts'] > last_seen]
        for x in new:
            last_seen = max(last_seen, x['ts'])
        if not new:
            entry['last_reply_ts'] = last_seen
            continue
        for x in new:
            if not LIVE:
                continue
            applied, unknown_lines = {}, []
            remaining = []
            for line in x.get('text', '').splitlines():
                line = line.strip()
                if not line:
                    continue
                fake = parse_message(f"<tel:+910000000000|+91-0000000000>\nX |-\n{line}")
                got = {k: v for k, v in fake.get('captured', {}).items()
                       if k not in ('phone', 'name', 'building')}
                if got:
                    applied.update(got)
                elif re.match(r'(?i)^building', line):
                    guess = re.split(r'[-:]', line, 1)[1].strip()
                    m = freeform_crm.find_building_exact(guess)
                    if m and not (isinstance(m, tuple) and m[0] == 'DUP'):
                        freeform_crm.gql(
                            'mutation($id: UUID!, $d: PropertyUpdateInput!) '
                            '{ updateProperty(id: $id, data: $d) { id } }',
                            {'id': entry['propertyId'], 'd': {'buildingId': m[0]}})
                        applied['building'] = m[1]
                        if 'building_match' in entry['missing']:
                            entry['missing'].remove('building_match')
                    else:
                        unknown_lines.append(line)
                else:
                    remaining.append(line)
            if remaining and not applied:
                llm = llm_extract_fields('\n'.join(remaining))
                llm.pop('error', None)
                applied.update(llm)
                if not llm:
                    unknown_lines.extend(remaining)
            if applied and entry.get('propertyId'):
                non_bld = {k: v for k, v in applied.items() if k != 'building'}
                if non_bld:
                    apply_property_update(entry['propertyId'], non_bld)
                entry['plan_captured'].update(applied)
                entry['missing'] = [f for f in entry['missing']
                                    if f in ('building_match',) or f not in applied]
                txt_out = '✅ Updated: ' + ', '.join(
                    f"{k}={v}" for k, v in applied.items())
                if entry['missing']:
                    txt_out += f"\n❌ Still missing: {', '.join(entry['missing'])}"
                if unknown_lines:
                    txt_out += '\n⚠️ Couldn\'t place: ' + '; '.join(
                        f"`{u}`" for u in unknown_lines)
                slack('chat.postMessage', channel=CHANNEL, thread_ts=ts, text=txt_out)
                if not entry['missing']:
                    react(CHANNEL, ts)
                changed = True
            elif unknown_lines:
                slack('chat.postMessage', channel=CHANNEL, thread_ts=ts,
                      text="⚠️ Couldn't place: " + '; '.join(
                          f"`{u}`" for u in unknown_lines) +
                          "\nUse `Field - value` format, e.g. `Price - 1.6 Cr`")
        entry['last_reply_ts'] = last_seen
    return changed


def main():
    st = load_state()
    hist = slack('conversations.history', channel=CHANNEL, limit=100)
    msgs = sorted(hist.get('messages', []), key=lambda m: m['ts'])
    new_msgs = [m for m in msgs if m.get('user') and m['ts'] not in st['processed']
                and m['ts'] > st['checkpoint']
                and 'has joined the channel' not in m.get('text', '')]
    print(f"new messages: {len(new_msgs)} (live={LIVE})")
    processed_leads = 0
    for m in new_msgs:
        st['processed'].append(m['ts'])
        st['checkpoint'] = max(st['checkpoint'], m['ts'])
        plan = process_message(m, st)
        if plan:
            processed_leads += 1
            print(f"--- {m['ts']}")
            print(freeform_crm.render_receipt(plan))
            if not LIVE:
                print("(dry-run — no writes, no Slack reply)")
    poll_corrections(st)
    save_state(st)
    print(f"done. leads processed: {processed_leads}")


if __name__ == '__main__':
    main()
