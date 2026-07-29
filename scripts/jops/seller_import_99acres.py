#!/usr/bin/env python3
"""
Full seller import: 99acres leads → Twenty CRM.
Reads sheet, matches/creates persons, creates sellers and notes.
"""
import csv, io, re, json, requests, subprocess, uuid, os, sys

SHEET_ID = "1jsDoGS4C4w82Yxyl-2g4OtAiX_um1epq5ABHVss9K4M"
DRY_RUN = '--live' not in sys.argv  # Dry-run by default

# ============================================================
# Enums
# ============================================================
ONBOARDING_MAP = {
    'Identified': 'IDENTIFIED',
    'Contacted': 'CONTACTED',
    'Active': 'ACTIVE',
    'Inactive': 'INACTIVE',
    'RnR 1': 'RNR',
    'RnR 2': 'RNR',
}

DROP_REASON_MAP = {
    'Fees not Ok': 'FEES_NOT_OK',
    'Non Servicable Location': 'NON_SERVICABLE_LOCATION',
    'Broker Lead': 'BROKER_LEAD',
    'Duplicate': 'DUPLICATE',
    'Not Interest': 'NOT_INTEREST',
    'Already Sold': 'ALREADY_SOLD',
    'Invalid Number': 'INVALID_NUMBER',
    'Rented out': 'RENTED_OUT',
}

SOURCE = 'NINETYNINE_ACRES'
PHONE_SKIP_9DIGIT = {'812992233', '760219374', '886231505', '901171237'}

# ============================================================
# Helpers
# ============================================================
def parse_phones(raw):
    if not raw or not raw.strip():
        return []
    raw = raw.strip()
    parts = re.split(r'\s*(?:,|\band\b)\s*', raw)
    phones = []
    for p in parts:
        p = p.strip().strip('"').strip()
        if not p:
            continue
        p = p.replace('.0', '')
        if p.startswith('+'):
            clean = '+' + re.sub(r'[^\d]', '', p[1:])
        else:
            digits = re.sub(r'\D', '', p)
            if len(digits) == 10:
                clean = '+91' + digits
            elif len(digits) == 11 and digits.startswith('0'):
                clean = '+91' + digits[1:]
            elif len(digits) == 12 and digits.startswith('91'):
                clean = '+' + digits
            else:
                continue
        phones.append(clean)
    return phones

def esc_sql(s):
    if s is None:
        return 'NULL'
    s = s.replace('\\', '\\\\').replace("'", "''").replace('\n', '\\n')
    return f"E'{s}'"

def run_sql(sql):
    r = subprocess.run(
        ['docker', 'exec', '-i', 'twenty-db-1', 'psql', '-U', 'twenty',
         '-d', 'default', '-t', '-A'],
        input=sql, capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0 or 'ERROR' in r.stderr:
        print(f"  SQL ERROR (rc={r.returncode}): {r.stderr[:300]}")
        return ''
    return r.stdout.strip()

# ============================================================
# 1. Read sheet
# ============================================================
print("=== Reading sheet ===")
url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
r = requests.get(url, timeout=30)
content = r.content.decode('utf-8')
reader = csv.reader(io.StringIO(content))
rows = list(reader)
data = rows[1:]

records = []
stats = {'skipped_filler': 0, 'skipped_9digit': 0, 'no_id': 0, 'parsed': 0}

for ri, row in enumerate(data):
    name = row[1].strip() if len(row) > 1 else ''
    phone_raw = row[2].strip() if len(row) > 2 else ''
    email = row[3].strip() if len(row) > 3 else ''
    seller_status = row[6].strip() if len(row) > 6 else ''
    drop_reason = row[7].strip() if len(row) > 7 else ''
    url_val = row[9].strip() if len(row) > 9 else ''

    if not name and not phone_raw:
        stats['skipped_filler'] += 1
        continue

    all_phones = parse_phones(phone_raw)

    # Skip 9-digit numbers
    if all_phones and any(p in PHONE_SKIP_9DIGIT for p in all_phones):
        stats['skipped_9digit'] += 1
        continue

    if not all_phones and not email:
        stats['no_id'] += 1
        continue

    primary_phone = all_phones[0] if all_phones else None
    secondary_phones = all_phones[1:] if len(all_phones) > 1 else []

    # Extra notes (cols beyond J)
    extra_notes = []
    for ci in range(10, len(row)):
        v = row[ci].strip()
        if v:
            extra_notes.append(v)

    note_parts = []
    if secondary_phones:
        note_parts.append(f"Secondary phone numbers: {', '.join(secondary_phones)}")
    if extra_notes:
        note_parts.extend(extra_notes)
    note_text = '\n'.join(note_parts) if note_parts else None

    name_parts = name.strip().split(' ', 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ''

    records.append({
        'name': name,
        'first_name': first_name,
        'last_name': last_name,
        'primary_phone': primary_phone,
        'secondary_phones': secondary_phones,
        'email': email,
        'seller_status': seller_status,
        'drop_reason': drop_reason,
        'url': url_val,
        'note_text': note_text,
    })
    stats['parsed'] += 1

print(f"Parsed: {stats['parsed']} | Skipped (filler): {stats['skipped_filler']} | "
      f"Skipped (9-digit): {stats['skipped_9digit']} | No ID: {stats['no_id']}")

# ============================================================
# 2. Fetch existing persons
# ============================================================
print("\n=== Fetching existing CRM persons ===")
res = subprocess.run(
    ['docker', 'exec', '-i', 'twenty-db-1', 'psql', '-U', 'twenty', '-d', 'default', '-t', '-A', '-F', '|'],
    input="SELECT id, \"phonesPrimaryPhoneNumber\", \"emailsPrimaryEmail\" "
          "FROM workspace_1l3urgumjmspnjxohclmfz6fx.person "
          'WHERE "deletedAt" IS NULL',
    capture_output=True, text=True, timeout=30
)

phone_to_person = {}
email_to_person = {}
person_by_id = {}

for line in res.stdout.strip().split('\n'):
    if not line or '|' not in line:
        continue
    parts = line.split('|', 2)
    if len(parts) < 3:
        continue
    pid, phone, email_addr = parts
    pid = pid.strip()
    phone = phone.strip() if phone != '' else None
    email_addr = email_addr.strip() if email_addr != '' else None
    person_by_id[pid] = True
    if phone:
        phone_to_person[phone] = pid
    if email_addr and email_addr.lower():
        email_to_person[email_addr.lower()] = pid

print(f"Existing persons: {len(person_by_id)}")

# ============================================================
# 3. Match records
# ============================================================
existing_persons = []
new_persons = []

for rec in records:
    matched = None
    if rec['primary_phone'] and rec['primary_phone'] in phone_to_person:
        matched = phone_to_person[rec['primary_phone']]
    if not matched and rec['email']:
        em = rec['email'].lower().strip()
        if em in email_to_person:
            matched = email_to_person[em]
    rec['person_id'] = matched
    if matched:
        existing_persons.append(rec)
    else:
        new_persons.append(rec)

# Check existing sellers for matched persons
matched_ids = [r['person_id'] for r in existing_persons if r['person_id']]
existing_seller_person_ids = set()
if matched_ids:
    for i in range(0, len(matched_ids), 200):
        chunk = matched_ids[i:i+200]
        ids_list = "','".join(chunk)
        sql = f"SELECT \"personId\" FROM workspace_1l3urgumjmspnjxohclmfz6fx._seller WHERE \"deletedAt\" IS NULL AND \"personId\" IN ('{ids_list}')"
        res2 = run_sql(sql)
        for line in res2.split('\n'):
            if line.strip():
                existing_seller_person_ids.add(line.strip())

needs_seller = [r for r in existing_persons if r['person_id'] not in existing_seller_person_ids]
already_done = [r for r in existing_persons if r['person_id'] in existing_seller_person_ids]

print(f"\n=== Match Results ===")
print(f"Already in CRM (person+seller): {len(already_done)}")
print(f"Person exists, needs seller:    {len(needs_seller)}")
print(f"Completely new (person+seller):  {len(new_persons)}")

if DRY_RUN:
    print("\n" + "="*60)
    print("DRY RUN — No data written")
    print("="*60)
    print(f"\nINSERT person:  {len(new_persons)}")
    print(f"INSERT seller:  {len(new_persons) + len(needs_seller)}")
    print(f"INSERT notes:   ~{sum(1 for r in (new_persons + needs_seller + already_done) if r['note_text'])}")
    print(f"\nPass --live to execute")
    sys.exit(0)

# ============================================================
# 4. LIVE — Create persons + sellers + notes
# ============================================================
print("\n" + "="*60)
print("LIVE — Writing to CRM")
print("="*60)

created_persons = 0
created_sellers = 0
created_notes = 0
skipped_sellers = 0

# Process all records that need creation
all_to_create = new_persons + needs_seller

for rec in all_to_create:
    name = rec['name']
    first_name = rec['first_name']
    last_name = rec['last_name']
    primary_phone = rec['primary_phone']
    email = rec['email']
    seller_status = rec['seller_status']
    drop_reason = rec['drop_reason']
    url_val = rec['url']
    note_text = rec['note_text']
    person_id = rec['person_id']

    # Create person if new
    if not person_id:
        person_id = str(uuid.uuid4())
        sql = (
            f"INSERT INTO workspace_1l3urgumjmspnjxohclmfz6fx.person "
            f"(id, \"nameFirstName\", \"nameLastName\", \"phonesPrimaryPhoneNumber\", "
            f"\"emailsPrimaryEmail\", \"createdAt\", \"updatedAt\") "
            f"VALUES ('{person_id}', {esc_sql(first_name)}, {esc_sql(last_name)}, "
            f"{esc_sql(primary_phone)}, {esc_sql(email)}, NOW(), NOW()) "
            f"ON CONFLICT DO NOTHING;"
        )
        run_sql(sql)
        created_persons += 1

    # Create seller
    seller_id = str(uuid.uuid4())
    onboarding_val = ONBOARDING_MAP.get(seller_status, 'NULL')
    drop_val = DROP_REASON_MAP.get(drop_reason, 'NULL')
    onboarding_sql = f"'{onboarding_val}'" if onboarding_val != 'NULL' else 'NULL'
    drop_sql = f"'{drop_val}'" if drop_val != 'NULL' else 'NULL'

    sql = (
        f"INSERT INTO workspace_1l3urgumjmspnjxohclmfz6fx._seller "
        f"(id, name, \"personId\", \"sourceUrlPrimaryLinkUrl\", source, "
        f"\"onboardingStatus\", \"dropReason\", \"createdAt\", \"updatedAt\", "
        f"\"createdBySource\", \"createdByName\", \"updatedBySource\", "
        f"\"updatedByName\", position) "
        f"VALUES ('{seller_id}', {esc_sql(name)}, '{person_id}', "
        f"{esc_sql(url_val)}, "
        f"'{SOURCE}'::\"workspace_1l3urgumjmspnjxohclmfz6fx\".\"_seller_source_enum\", "
        f"{onboarding_sql}::\"workspace_1l3urgumjmspnjxohclmfz6fx\".\"_seller_onboardingStatus_enum\", "
        f"{drop_sql}::\"workspace_1l3urgumjmspnjxohclmfz6fx\".\"_seller_dropReason_enum\", "
        f"NOW(), NOW(), 'IMPORT', 'Operator', 'IMPORT', 'Operator', 0) "
        f"ON CONFLICT DO NOTHING;"
    )
    run_sql(sql)

    # Verify
    check = run_sql(f"SELECT id FROM workspace_1l3urgumjmspnjxohclmfz6fx._seller WHERE id = '{seller_id}'")
    if check:
        created_sellers += 1
    else:
        skipped_sellers += 1
        continue

    # Create note if needed
    if note_text:
        note_id = str(uuid.uuid4())
        nt_id = str(uuid.uuid4())
        title = note_text[:60]  # First 60 chars of actual text

        sql_note = (
            f"INSERT INTO workspace_1l3urgumjmspnjxohclmfz6fx.note "
            f"(id, title, \"bodyV2Markdown\", \"createdAt\", \"updatedAt\", "
            f"\"createdBySource\", \"createdByName\", \"updatedBySource\", \"updatedByName\") "
            f"VALUES ('{note_id}', {esc_sql(title)}, {esc_sql(note_text)}, "
            f"NOW(), NOW(), 'IMPORT', 'Operator', 'IMPORT', 'Operator') "
            f"ON CONFLICT DO NOTHING;"
        )
        run_sql(sql_note)

        sql_nt = (
            f"INSERT INTO workspace_1l3urgumjmspnjxohclmfz6fx.\"noteTarget\" "
            f"(id, \"noteId\", \"targetSellerId\", \"createdAt\", \"updatedAt\", "
            f"\"createdBySource\", \"createdByName\", \"updatedBySource\", \"updatedByName\", position) "
            f"VALUES ('{nt_id}', '{note_id}', '{seller_id}', "
            f"NOW(), NOW(), 'IMPORT', 'Operator', 'IMPORT', 'Operator', 0) "
            f"ON CONFLICT DO NOTHING;"
        )
        run_sql(sql_nt)
        created_notes += 1

    # Progress
    if created_persons % 50 == 0:
        print(f"  Progress: {created_persons} persons, {created_sellers} sellers, {created_notes} notes")

print(f"\n=== Done ===")
print(f"Persons created: {created_persons}")
print(f"Sellers created: {created_sellers}")
print(f"Notes created: {created_notes}")
print(f"Skipped (already existed): {skipped_sellers}")
print(f"Skipped (already in CRM): {len(already_done)}")