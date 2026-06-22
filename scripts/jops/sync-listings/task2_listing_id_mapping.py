"""
Task 2: Listing ID Mapping
Match Supabase listing.glide_serial_number -> CRM _property.serialNumber
and populate listing.internal_id with the CRM property UUID.

Usage:
  python3 task2_listing_id_mapping.py           # live run
  python3 task2_listing_id_mapping.py --check   # dry run
"""
import psycopg2, subprocess, json, sys

DRY_RUN = '--check' in sys.argv
WS = 'workspace_1l3urgumjmspnjxohclmfz6fx'

# ---- Step 1: Dump CRM serial numbers to a temp file ----
print("=== Dumping CRM property serial numbers ===")
result = subprocess.run(
    ['docker', 'exec', 'twenty-db-1', 'psql', '-U', 'twenty', '-d', 'default',
     '-c', f"COPY (SELECT \"id\", \"serialNumber\"::int, \"buildingId\" FROM {WS}._property WHERE \"deletedAt\" IS NULL AND \"serialNumber\" IS NOT NULL ORDER BY \"serialNumber\") TO STDOUT WITH CSV"],
    capture_output=True, text=True, timeout=30
)

crm_by_sn = {}
for line in result.stdout.strip().split('\n'):
    if not line:
        continue
    parts = line.split(',')
    if len(parts) >= 2:
        try:
            sn = int(parts[1])
            crm_by_sn[sn] = {
                'id': parts[0],
                'buildingId': parts[2] if len(parts) > 2 and parts[2] else None
            }
        except ValueError:
            pass
print(f"  CRM properties with serialNumber: {len(crm_by_sn)}")

# ---- Step 2: Load Supabase listings ----
print("\n=== Loading Supabase listings ===")
conn = psycopg2.connect(
    host='aws-1-ap-south-1.pooler.supabase.com', port=6543,
    user='postgres.dcukqjnvgyhnynsxpkzx', password='870SW5q7hto4mraa', dbname='postgres'
)
cur = conn.cursor()
cur.execute('SELECT "id", "glide_serial_number", "internal_id", "building_id" FROM listing WHERE "glide_serial_number" IS NOT NULL')
supabase_listings = cur.fetchall()
print(f"  Supabase listings: {len(supabase_listings)}")

# ---- Step 3: Match ----
matches = []
unmatched_supabase = []

for supabase_id, glide_sn, internal_id, building_id in supabase_listings:
    sn = int(glide_sn) if glide_sn else None
    if sn and sn in crm_by_sn:
        crm_prop = crm_by_sn[sn]
        matches.append({
            'supabase_id': str(supabase_id),
            'crm_id': crm_prop['id'],
            'serial_number': sn,
            'crm_building_id': crm_prop['buildingId'],
            'supabase_building_id': str(building_id) if building_id else None,
            'already_mapped': internal_id is not None
        })
    else:
        unmatched_supabase.append({'supabase_id': str(supabase_id), 'glide_serial_number': sn})

already_mapped = sum(1 for m in matches if m['already_mapped'])
need_update = sum(1 for m in matches if not m['already_mapped'])

print(f"\n=== Match Results ===")
print(f"  Total matched: {len(matches)}")
print(f"  Already have internal_id: {already_mapped}")
print(f"  Need to update: {need_update}")
print(f"  Unmatched Supabase listings: {len(unmatched_supabase)}")
print(f"  Unmatched CRM properties: {len(crm_by_sn) - len(matches)}")

if unmatched_supabase:
    print(f"\n  Unmatched Supabase (first 15):")
    for u in unmatched_supabase[:15]:
        print(f"    sn={u['glide_serial_number']}, id={u['supabase_id']}")

# ---- Step 4: Write internal_id to Supabase ----
if not DRY_RUN and need_update > 0:
    print(f"\n=== Writing {need_update} internal_id values to Supabase ===")
    updated = 0
    errors = 0
    for m in matches:
        if m['already_mapped']:
            continue
        try:
            cur.execute(
                'UPDATE listing SET "internal_id" = %s WHERE "id" = %s',
                (m['crm_id'], m['supabase_id'])
            )
            if cur.rowcount > 0:
                updated += 1
        except Exception as e:
            errors += 1
            print(f"  ERROR: {m['supabase_id']} -> {m['crm_id']}: {e}")

    conn.commit()
    print(f"  Updated: {updated}, Errors: {errors}")

    cur.execute('SELECT COUNT(*) FROM listing WHERE "internal_id" IS NOT NULL')
    total_with_id = cur.fetchone()[0]
    cur.execute('SELECT COUNT(*) FROM listing')
    total = cur.fetchone()[0]
    print(f"  Supabase listings with internal_id: {total_with_id}/{total}")
elif DRY_RUN:
    print("\n[DRY RUN] No writes performed.")

conn.close()

# Save results
results = {
    'matched': len(matches),
    'already_mapped': already_mapped,
    'to_update': need_update,
    'unmatched_supabase': len(unmatched_supabase),
    'unmatched_crm': len(crm_by_sn) - len(matches),
    'matches': matches,
    'unmatched_supabase_list': unmatched_supabase,
}
with open('/tmp/listing_id_mapping_results.json', 'w') as f:
    json.dump(results, f, indent=2)
print(f"\nResults saved to /tmp/listing_id_mapping_results.json")
print("=== Task 2 Complete ===")
