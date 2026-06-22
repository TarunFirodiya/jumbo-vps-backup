"""
Task 5: External User Mapping + Sync (Supabase <-> CRM)
Match Supabase external_user -> CRM person + buyer via phone number.
For matches: update CRM person + buyer.
For new users: create person + buyer + enquiry in CRM.
Write back CRM buyer.id -> Supabase external_user.internal_id.

Usage:
  python3 sync_external_users.py                    # sync all external_users
  python3 sync_external_users.py --limit 5          # test on first 5
  python3 sync_external_users.py --id <supabase_id> # sync one by Supabase UUID
  python3 sync_external_users.py --check            # dry run
"""
import psycopg2, subprocess, sys, json, re
from datetime import datetime

DRY_RUN = '--check' in sys.argv
WS = 'workspace_1l3urgumjmspnjxohclmfz6fx'

# Parse args
LIMIT = None
SINGLE_ID = None
for i, arg in enumerate(sys.argv):
    if arg == '--limit' and i + 1 < len(sys.argv):
        LIMIT = int(sys.argv[i + 1])
    elif arg == '--id' and i + 1 < len(sys.argv):
        SINGLE_ID = sys.argv[i + 1]

def get_supabase_conn():
    return psycopg2.connect(
        host='aws-1-ap-south-1.pooler.supabase.com', port=6543,
        user='postgres.dcukqjnvgyhnynsxpkzx', password='870SW5q7hto4mraa', dbname='postgres',
        connect_timeout=15
    )

def crm_query(sql, timeout=30):
    result = subprocess.run(
        ['docker', 'exec', 'twenty-db-1', 'psql', '-U', 'twenty', '-d', 'default',
         '-t', '-A', '-F', '|', '-c', sql],
        capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        return None
    lines = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
    return lines

def crm_execute(sql, timeout=30):
    result = subprocess.run(
        ['docker', 'exec', 'twenty-db-1', 'psql', '-U', 'twenty', '-d', 'default',
         '-c', sql],
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode == 0, result.stdout, result.stderr

def parse_row(line):
    """Parse pipe-delimited CRM query result."""
    return line.split('|')

def strip_phone(phone):
    """Strip +91 prefix and any non-digit chars to get 10-digit Indian phone."""
    if not phone:
        return None
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    if len(digits) == 10:
        return digits
    return None

def split_name(full_name):
    """Split full name into first and last name."""
    if not full_name:
        return '', ''
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], ''

# Stats
stats = {
    'total': 0,
    'matched_person': 0,
    'new_person': 0,
    'buyer_created': 0,
    'buyer_updated': 0,
    'person_updated': 0,
    'enquiry_created': 0,
    'write_back_success': 0,
    'write_back_failed': 0,
    'errors': [],
}

print(f"{'[DRY RUN] ' if DRY_RUN else ''}Starting external_user sync...")
print(f"Workspace: {WS}")
print()

# 1. Fetch all external_user records from Supabase
sconn = get_supabase_conn()
scur = sconn.cursor()

if SINGLE_ID:
    scur.execute("""
        SELECT id, name, phone_number, email, drop_reason, created_at, updated_at
        FROM external_user WHERE id = %s
    """, (SINGLE_ID,))
else:
    scur.execute("""
        SELECT id, name, phone_number, email, drop_reason, created_at, updated_at
        FROM external_user ORDER BY created_at ASC
    """)

rows = scur.fetchall()
if LIMIT:
    rows = rows[:LIMIT]

stats['total'] = len(rows)
print(f"Fetched {len(rows)} external_user records from Supabase")
print()

for idx, (su_id, su_name, su_phone, su_email, su_drop_reason, su_created, su_updated) in enumerate(rows, 1):
    clean_phone = strip_phone(su_phone)
    print(f"[{idx}/{len(rows)}] {su_name} | phone={su_phone} (clean: {clean_phone}) | email={su_email}")

    if not clean_phone:
        print(f"  SKIP: Cannot parse phone number '{su_phone}'")
        stats['errors'].append(f"{su_id}: unparseable phone {su_phone}")
        continue

    # 2. Try to match CRM person by phone
    person_lines = crm_query(
        f"SELECT p.id, p.\"nameFirstName\", p.\"nameLastName\", p.\"emailsPrimaryEmail\" "
        f"FROM {WS}.person p "
        f"WHERE p.\"phonesPrimaryPhoneNumber\" = '{clean_phone}' AND p.\"deletedAt\" IS NULL "
        f"ORDER BY p.\"createdAt\" ASC LIMIT 1"
    )

    person_id = None
    if person_lines and len(person_lines) > 0:
        pparts = parse_row(person_lines[0])
        person_id = pparts[0]
        print(f"  Matched CRM person: id={person_id}, name={pparts[1]} {pparts[2]}")

    if person_id:
        # 3a. Person exists -> update person + buyer (or create buyer if missing)
        stats['matched_person'] += 1

        # Update person name/email if needed (COALESCE: don't overwrite with NULL)
        first_name, last_name = split_name(su_name)
        update_parts = []
        if first_name:
            update_parts.append(f"\"nameFirstName\" = COALESCE(NULLIF('{first_name}', ''), \"nameFirstName\")")
        if last_name:
            update_parts.append(f"\"nameLastName\" = COALESCE(NULLIF('{last_name}', ''), \"nameLastName\")")
        if su_email:
            update_parts.append(f"\"emailsPrimaryEmail\" = COALESCE(NULLIF('{su_email.replace(chr(39), chr(39)+chr(39))}', ''), \"emailsPrimaryEmail\")")
        update_parts.append(f"\"phonesPrimaryPhoneNumber\" = '{clean_phone}'")
        update_parts.append(f"\"updatedAt\" = NOW()")

        if update_parts and not DRY_RUN:
            update_sql = f"UPDATE {WS}.person SET {', '.join(update_parts)} WHERE id = '{person_id}'"
            ok, out, err = crm_execute(update_sql)
            if ok:
                stats['person_updated'] += 1
                print(f"  Updated person {person_id}")
            else:
                print(f"  ERROR updating person: {err}")
                stats['errors'].append(f"{su_id}: person update failed: {err[:100]}")
        elif DRY_RUN:
            print(f"  [DRY RUN] Would update person {person_id}")

        # Check if buyer exists for this person
        buyer_lines = crm_query(
            f"SELECT b.id, b.name, b.\"dropReason\" "
            f"FROM {WS}._buyer b "
            f"WHERE b.\"personId\" = '{person_id}' AND b.\"deletedAt\" IS NULL "
            f"ORDER BY b.\"createdAt\" ASC LIMIT 1"
        )

        if buyer_lines and len(buyer_lines) > 0:
            bparts = parse_row(buyer_lines[0])
            buyer_id = bparts[0]
            print(f"  Buyer exists: id={buyer_id}")

            # Update buyer
            buyer_update_parts = []
            if su_name:
                buyer_update_parts.append(f"name = COALESCE(NULLIF('{su_name}', ''), name)")
            if su_drop_reason:
                buyer_update_parts.append(f"\"dropReason\" = '{su_drop_reason}'::\"{WS}\".\"_buyer_dropReason_enum\"")
            buyer_update_parts.append(f"\"updatedAt\" = NOW()")

            if buyer_update_parts and not DRY_RUN:
                buyer_update_sql = f"UPDATE {WS}._buyer SET {', '.join(buyer_update_parts)} WHERE id = '{buyer_id}'"
                ok, out, err = crm_execute(buyer_update_sql)
                if ok:
                    stats['buyer_updated'] += 1
                    print(f"  Updated buyer {buyer_id}")
                else:
                    print(f"  ERROR updating buyer: {err}")
                    stats['errors'].append(f"{su_id}: buyer update failed: {err[:100]}")
            elif DRY_RUN:
                print(f"  [DRY RUN] Would update buyer {buyer_id}")
        else:
            # Create buyer for existing person
            buyer_id = None
            if not DRY_RUN:
                first_name, last_name = split_name(su_name)
                create_buyer_sql = f"""
                    INSERT INTO {WS}._buyer (id, name, "personId", "createdBySource", "createdAt", "updatedAt", "position")
                    VALUES (uuid_generate_v4(), '{su_name or ''}', '{person_id}', 'IMPORT', NOW(), NOW(), 0)
                    RETURNING id
                """
                ok, out, err = crm_execute(create_buyer_sql)
                if ok:
                    # Extract UUID from output
                    match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', out)
                    if match:
                        buyer_id = match.group(1)
                        stats['buyer_created'] += 1
                        print(f"  Created buyer {buyer_id} for existing person")
                else:
                    print(f"  ERROR creating buyer: {err}")
                    stats['errors'].append(f"{su_id}: buyer create failed: {err[:100]}")
            else:
                print(f"  [DRY RUN] Would create buyer for existing person {person_id}")

        # Write back buyer.id -> Supabase
        if buyer_id and not DRY_RUN:
            scur.execute("UPDATE external_user SET internal_id = %s, updated_at = NOW() WHERE id = %s", (buyer_id, su_id))
            sconn.commit()
            stats['write_back_success'] += 1
            print(f"  Write-back: external_user.internal_id = {buyer_id}")
        elif buyer_id and DRY_RUN:
            print(f"  [DRY RUN] Would write-back internal_id = {buyer_id}")

    else:
        # 3b. Person doesn't exist -> create person + buyer + enquiry
        stats['new_person'] += 1
        print(f"  No CRM match. Creating person + buyer + enquiry...")

        if DRY_RUN:
            print(f"  [DRY RUN] Would create person, buyer, enquiry for {su_name}")
            continue

        # Create person
        first_name, last_name = split_name(su_name)
        # Escape single quotes
        first_name_esc = first_name.replace("'", "''")
        last_name_esc = last_name.replace("'", "''")
        email_val = 'NULL' if not su_email else f"'{su_email.replace(chr(39), chr(39)+chr(39))}'"

        create_person_sql = f"""
            INSERT INTO {WS}.person (
                id, "nameFirstName", "nameLastName", "phonesPrimaryPhoneNumber",
                "phonesPrimaryPhoneCountryCode", "phonesPrimaryPhoneCallingCode",
                "emailsPrimaryEmail", "createdBySource", "createdByName",
                "createdAt", "updatedAt", "position"
            ) VALUES (
                uuid_generate_v4(), '{first_name_esc}', '{last_name_esc}', '{clean_phone}',
                'IN', '+91',
                {email_val}, 'IMPORT', 'Jumbo Sync',
                NOW(), NOW(), 0
            ) RETURNING id
        """
        ok, out, err = crm_execute(create_person_sql)
        if not ok:
            print(f"  ERROR creating person: {err}")
            stats['errors'].append(f"{su_id}: person create failed: {err[:100]}")
            continue

        match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', out)
        if not match:
            print(f"  ERROR: Could not extract person ID from output: {out}")
            stats['errors'].append(f"{su_id}: person ID extraction failed")
            continue
        new_person_id = match.group(1)
        print(f"  Created person: {new_person_id}")

        # Create buyer
        name_esc = (su_name or '').replace("'", "''")
        create_buyer_sql = f"""
            INSERT INTO {WS}._buyer (
                id, name, "personId", "createdBySource", "createdByName",
                "createdAt", "updatedAt", "position"
            ) VALUES (
                uuid_generate_v4(), '{name_esc}', '{new_person_id}', 'IMPORT', 'Jumbo Sync',
                NOW(), NOW(), 0
            ) RETURNING id
        """
        ok, out, err = crm_execute(create_buyer_sql)
        if not ok:
            print(f"  ERROR creating buyer: {err}")
            stats['errors'].append(f"{su_id}: buyer create failed: {err[:100]}")
            continue

        match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', out)
        new_buyer_id = match.group(1) if match else None
        if new_buyer_id:
            stats['buyer_created'] += 1
            print(f"  Created buyer: {new_buyer_id}")
        else:
            print(f"  ERROR: Could not extract buyer ID from output: {out}")
            stats['errors'].append(f"{su_id}: buyer ID extraction failed")
            continue

        # Create enquiry
        enquiry_name = f"Enquiry from {su_name or 'Website User'}"
        enquiry_name_esc = enquiry_name.replace("'", "''")
        create_enquiry_sql = f"""
            INSERT INTO {WS}._enquiry (
                id, name, "buyerId", "assignedAgentId", "createdBySource", "createdByName",
                "sourceDetail", "statusDetail",
                "createdAt", "updatedAt", "position"
            ) VALUES (
                uuid_generate_v4(), '{enquiry_name_esc}', '{new_buyer_id}', '404bdd9e-04c6-4ec6-a913-c9d98ab07c92', 'IMPORT', 'Jumbo Sync',
                'WEBSITE', 'NEW_LEAD',
                NOW(), NOW(), 0
            ) RETURNING id
        """
        ok, out, err = crm_execute(create_enquiry_sql)
        if ok:
            match = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', out)
            new_enquiry_id = match.group(1) if match else '?'
            stats['enquiry_created'] += 1
            print(f"  Created enquiry: {new_enquiry_id}")
        else:
            print(f"  ERROR creating enquiry: {err}")
            stats['errors'].append(f"{su_id}: enquiry create failed: {err[:100]}")

        # Write back buyer.id -> Supabase
        if new_buyer_id:
            scur.execute("UPDATE external_user SET internal_id = %s, updated_at = NOW() WHERE id = %s", (new_buyer_id, su_id))
            sconn.commit()
            stats['write_back_success'] += 1
            print(f"  Write-back: external_user.internal_id = {new_buyer_id}")

scur.close()
sconn.close()

# Print summary
print()
print("=" * 60)
print(f"{'[DRY RUN] ' if DRY_RUN else ''}SYNC SUMMARY")
print("=" * 60)
print(f"  Total external_user processed: {stats['total']}")
print(f"  Matched to existing CRM person: {stats['matched_person']}")
print(f"  New persons created:            {stats['new_person']}")
print(f"  Persons updated:                {stats['person_updated']}")
print(f"  Buyers created:                 {stats['buyer_created']}")
print(f"  Buyers updated:                 {stats['buyer_updated']}")
print(f"  Enquiries created:              {stats['enquiry_created']}")
print(f"  Write-backs successful:         {stats['write_back_success']}")
print(f"  Write-backs failed:             {stats['write_back_failed']}")
print(f"  Errors:                         {len(stats['errors'])}")
if stats['errors']:
    print()
    print("  Error details:")
    for e in stats['errors'][:20]:
        print(f"    - {e}")
    if len(stats['errors']) > 20:
        print(f"    ... and {len(stats['errors']) - 20} more")
print("=" * 60)
