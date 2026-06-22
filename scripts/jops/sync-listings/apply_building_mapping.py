import psycopg2, json, sys

# Load matches from Task 1
with open('/tmp/building_matches.json') as f:
    data = json.load(f)

matches = data['matches']
print(f"Loaded {len(matches)} matches")

# Connect to Supabase staging
pwd = '870SW5q7hto4mraa'
conn = psycopg2.connect(
    host='aws-1-ap-south-1.pooler.supabase.com', port=6543,
    user='postgres.dcukqjnvgyhnynsxpkzx', password=pwd, dbname='postgres'
)
cur = conn.cursor()

# Write internal_id for each matched building
updated = 0
errors = 0
for m in matches:
    supabase_id = m['supabase_id']
    crm_id = m['crm_id']
    try:
        cur.execute(
            'UPDATE building SET "internal_id" = %s WHERE "id" = %s AND ("internal_id" IS NULL OR "internal_id" != %s)',
            (crm_id, supabase_id, crm_id)
        )
        if cur.rowcount > 0:
            updated += 1
    except Exception as e:
        errors += 1
        print(f"  ERROR: {supabase_id} -> {crm_id}: {e}")

conn.commit()
print(f"Updated {updated} buildings with internal_id, {errors} errors")

# Verify
cur.execute('SELECT COUNT(*) FROM building WHERE "internal_id" IS NOT NULL')
total_with_id = cur.fetchone()[0]
cur.execute('SELECT COUNT(*) FROM building')
total = cur.fetchone()[0]
print(f"Supabase buildings with internal_id: {total_with_id}/{total}")

conn.close()
