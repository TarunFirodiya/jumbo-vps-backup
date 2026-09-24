#!/usr/bin/env python3
"""
Free-form seller lead parser (JUM-700 Phase 6).

Parses #temp-growth messages with NO portal URL, e.g.:

    <tel:+919****5333|+91-9916455333>
    Pranay Kumar |-
    SBR Pravanika
    3BHK
    Semi-Furnished
    Floor - 7
    Area - 1336
    Price - 1.50 Cr

Design rules (agreed with Rushabh, Sep 22 2026):
- Conservative extraction: a field is written ONLY when the pattern is
  unambiguous. Anything uncertain goes to `unknown` with its raw text.
- No building fuzzy match. Building name is captured verbatim; exact/normalized
  match against CRM happens downstream, not here.
- Output buckets: captured / unknown / missing.
"""
import re, json

# ---------------------------------------------------------------- patterns
RE_TEL_LABEL = re.compile(r'<tel:[^|]*\|([^>]+)>')           # label carries real number
RE_TEL_ANY   = re.compile(r'(\+?\d[\d\s\-]{8,14}\d)')
RE_URL       = re.compile(r'https?://')
RE_NAME_SEP  = re.compile(r'^(.*?)\s*\|\s*-?\s*$')           # "Pranay Kumar |-"
RE_BHK       = re.compile(r'^(\d(?:\.\d)?)\s*BHK(?:\s*[-–:]?\s*(villa|independent house|apartment|flat|builder floor|house|plot|penthouse|studio))?$', re.I)
RE_FLOOR     = re.compile(r'^floor\s*[-:]\s*(\d+)', re.I)
RE_AREA      = re.compile(r'^area\s*[-:]\s*([\d,]+)\s*(sq\.?\s*ft|sqft)?$', re.I)
RE_CARPET    = re.compile(r'^carpet\s*[-:]\s*([\d,]+)\s*(sq\.?\s*ft|sqft)?$', re.I)
RE_PRICE     = re.compile(r'^price\s*[-:]\s*([\d.,]+)\s*(cr|crore|lakhs?|lacs?|l|k)?$', re.I)
RE_UNIT      = re.compile(r'^unit\s*no\.?\s*[-:]\s*(\S+)', re.I)
RE_FURNISH   = re.compile(r'^(semi|fully|un)\s*[-–]?\s*furnished$', re.I)
RE_FACING    = re.compile(r'^(north|south|east|west|ne|nw|se|sw)(?:\s*facing)?$', re.I)
RE_SOURCE    = re.compile(r'^source\s*[-:]\s*(.+)$', re.I)

FURNISH_MAP = {
    'semi': 'SEMI_FURNISHED', 'semi-furnished': 'SEMI_FURNISHED', 'semi furnished': 'SEMI_FURNISHED',
    'fully': 'FULLY_FURNISHED', 'fully-furnished': 'FULLY_FURNISHED', 'fully furnished': 'FULLY_FURNISHED',
    'un': 'UNFURNISHED', 'unfurnished': 'UNFURNISHED',
}
FACING_MAP = {'ne': 'NORTH_EAST', 'nw': 'NORTH_WEST', 'se': 'SOUTH_EAST', 'sw': 'SOUTH_WEST'}

EXPECTED = ['phone', 'name', 'building', 'bhk', 'price_rupees']  # core fields for "missing" report


def norm_phone(raw):
    d = re.sub(r'\D', '', raw)
    if len(d) == 12 and d.startswith('91'):
        d = d[2:]
    if len(d) == 11 and d.startswith('0'):
        d = d[1:]
    return d if len(d) == 10 else None


def price_to_rupees(num, unit):
    v = float(num.replace(',', '').replace('lakhs', '').replace('lakh', ''))
    u = (unit or '').lower()
    if u in ('cr', 'crore'):
        return int(v * 10_000_000)
    if u in ('l', 'lac', 'lacs', 'lakh', 'lakhs'):
        return int(v * 100_000)
    if u == 'k':
        return int(v * 1_000)
    return None  # no unit -> not certain, skip


def parse_message(text):
    """Return dict(captured, unknown, missing)."""
    captured, unknown = {}, []

    if RE_URL.search(text):
        return {'routed': 'url_track'}  # not free-form; existing URL pipeline handles

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # phone: prefer tel: label (Slack masks the URI, label is real)
    m = RE_TEL_LABEL.search(text)
    if m:
        ph = norm_phone(m.group(1))
        if ph:
            captured['phone'] = ph
        else:
            unknown.append(('phone?', m.group(1)))
    else:
        m = RE_TEL_ANY.search(text)
        if m:
            ph = norm_phone(m.group(1))
            if ph:
                captured['phone'] = ph
            else:
                unknown.append(('phone?', m.group(1)))

    # name: line ending with "|-" or "| -"
    consumed = set()
    for i, l in enumerate(lines):
        mm = RE_NAME_SEP.match(l)
        if mm and mm.group(1).strip() and not RE_TEL_LABEL.search(l):
            captured['name'] = mm.group(1).strip()
            consumed.add(i)
            break

    # field lines
    building_candidates = []
    for i, l in enumerate(lines):
        if i in consumed or RE_TEL_LABEL.search(l):
            continue
        if (m := RE_BHK.match(l)):
            captured['bhk'] = float(m.group(1)) if '.' in m.group(1) else int(m.group(1))
            if m.group(2):
                t = m.group(2).lower()
                captured['property_type'] = ('VILLA' if 'villa' in t or 'house' in t
                                             else 'PENTHOUSE' if 'penthouse' in t
                                             else 'APARTMENT')
        elif (m := RE_FLOOR.match(l)):
            captured['floor'] = int(m.group(1))
        elif (m := RE_AREA.match(l)):
            captured['area_sqft'] = int(m.group(1).replace(',', ''))
        elif (m := RE_CARPET.match(l)):
            captured['carpet_sqft'] = int(m.group(1).replace(',', ''))
        elif (m := RE_PRICE.match(l)):
            r = price_to_rupees(m.group(1), m.group(2))
            if r:
                captured['price_rupees'] = r
            else:
                unknown.append(('price?', l))
        elif (m := RE_UNIT.match(l)):
            captured['unit_no'] = m.group(1)
        elif (m := RE_FURNISH.match(l)):
            key = m.group(1).lower()
            captured['furnishing'] = FURNISH_MAP.get(key, FURNISH_MAP.get(key + '-furnished', 'SEMI_FURNISHED'))
        elif (m := RE_FACING.match(l)):
            f = m.group(1).upper()
            captured['facing'] = FACING_MAP.get(m.group(1).lower(), f)
        elif (m := RE_SOURCE.match(l)):
            captured['source_raw'] = m.group(1).strip()
        else:
            building_candidates.append(l)

    # building: exactly one leftover free line -> certain enough (format convention).
    # More than one -> NOT sure which is the building; all go to unknown.
    if len(building_candidates) == 1:
        captured['building'] = building_candidates[0]
    elif building_candidates:
        for b in building_candidates:
            unknown.append(('building?', b))

    missing = [f for f in EXPECTED if f not in captured]
    return {'routed': 'freeform', 'captured': captured, 'unknown': unknown, 'missing': missing}


if __name__ == '__main__':
    import sys
    msgs = json.load(open(sys.argv[1]))
    for m in msgs:
        txt = m.get('text', '')
        if 'has joined the channel' in txt:
            continue
        r = parse_message(txt)
        print('=' * 60)
        print('ts', m['ts'])
        if r['routed'] == 'url_track':
            print('-> URL track (existing pipeline)')
            continue
        print('CAPTURED:', json.dumps(r['captured'], indent=2))
        if r['unknown']:
            print('UNKNOWN :', r['unknown'])
        if r['missing']:
            print('MISSING :', r['missing'])


SOURCE_MAP = {
    '99acres': 'NINETYNINE_ACRES', '99 acres': 'NINETYNINE_ACRES',
    'housing': 'HOUSING', 'housing.com': 'HOUSING',
    'magicbricks': 'MAGICBRICKS', 'makaan': 'MAGICBRICKS',
    'mygate': 'MYGATE', 'website': 'WEBSITE', 'instagram': 'INSTAGRAM',
    'resale board': 'RESALE_BOARD', 'wa community': 'WA_COMMUNITY',
    'whatsapp': 'WA_COMMUNITY', 'reddit': 'REDDIT', 'on site': 'ON_SITE',
    'friend': 'FRIEND_RELATIVE', 'referral buyer': 'REFERRAL_BUYER',
    'referral seller': 'REFERRAL_SELLER', 'referral': 'REFERRAL_AP',
}


def map_source(cap):
    raw = cap.get('source_raw')
    if raw:
        return SOURCE_MAP.get(raw.lower().strip(), 'NINETYNINE_ACRES')
    return 'NINETYNINE_ACRES'
