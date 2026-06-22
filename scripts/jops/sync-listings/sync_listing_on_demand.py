"""
On-demand listing sync trigger.
Sync one or more specific listings from CRM to Supabase by listing ID or serial number.

Usage:
  python3 sync_listing_on_demand.py --id <supabase_listing_id>
  python3 sync_listing_on_demand.py --sn <serial_number>
  python3 sync_listing_on_demand.py --ids <id1,id2,id3>
  python3 sync_listing_on_demand.py --sns <sn1,sn2,sn3>
"""
import sys, subprocess

# Parse args
by_id = None
by_sn = None
multi_ids = None
multi_sns = None

for i, arg in enumerate(sys.argv):
    if arg == '--id' and i + 1 < len(sys.argv):
        by_id = sys.argv[i + 1]
    elif arg == '--sn' and i + 1 < len(sys.argv):
        by_sn = sys.argv[i + 1]
    elif arg == '--ids' and i + 1 < len(sys.argv):
        multi_ids = [x.strip() for x in sys.argv[i + 1].split(',')]
    elif arg == '--sns' and i + 1 < len(sys.argv):
        multi_sns = [x.strip() for x in sys.argv[i + 1].split(',')]

# Build the command to call the main sync script
cmd = ['python3', '/tmp/sync_listings_to_supabase.py']

if by_id:
    cmd.extend(['--id', by_id])
elif by_sn:
    # Look up supabase_id from serial number
    import psycopg2
    conn = psycopg2.connect(
        host='aws-1-ap-south-1.pooler.supabase.com', port=6543,
        user='postgres.dcukqjnvgyhnynsxpkzx', password='870SW5q7hto4mraa', dbname='postgres'
    )
    cur = conn.cursor()
    cur.execute('SELECT "id" FROM listing WHERE "glide_serial_number" = %s', (int(by_sn),))
    row = cur.fetchone()
    conn.close()
    if not row:
        print(f"ERROR: No listing found with serial number {by_sn}")
        sys.exit(1)
    cmd.extend(['--id', str(row[0])])
elif multi_ids:
    cmd.extend(['--ids', ','.join(multi_ids)])
elif multi_sns:
    import psycopg2
    conn = psycopg2.connect(
        host='aws-1-ap-south-1.pooler.supabase.com', port=6543,
        user='postgres.dcukqjnvgyhnynsxpkzx', password='870SW5q7hto4mraa', dbname='postgres'
    )
    cur = conn.cursor()
    ids = []
    for sn in multi_sns:
        cur.execute('SELECT "id" FROM listing WHERE "glide_serial_number" = %s', (int(sn),))
        row = cur.fetchone()
        if row:
            ids.append(str(row[0]))
        else:
            print(f"WARNING: No listing found with serial number {sn}")
    conn.close()
    if not ids:
        print("ERROR: No valid listings found")
        sys.exit(1)
    cmd.extend(['--ids', ','.join(ids)])
else:
    print(__doc__)
    sys.exit(1)

print(f"Running: {' '.join(cmd)}")
result = subprocess.run(cmd)
sys.exit(result.returncode)
