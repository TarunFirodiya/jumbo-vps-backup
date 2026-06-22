"""
Task 3 Test: Sync one building from CRM to Supabase
Building: AWHO Sandeep Vihar (CRM: 88eff2d1, Supabase: 59e6e6da)
"""
import psycopg2, subprocess, json

# Step 1: Get building data from CRM
def crm_query(sql):
    result = subprocess.run(
        ['docker', 'exec', 'twenty-db-1', 'psql', '-U', 'twenty', '-d', 'default', '-t', '-A', '-F', '|', '-c', sql],
        capture_output=True, text=True
    )
    return result.stdout.strip()

crm_data = crm_query("""
    SELECT 
        "name",
        "locality",
        "nearestLandmark",
        "fulladrress",
        "totalFloors",
        "totalUnits",
        "acres",
        "latitude",
        "longitude",
        "mapLink",
        "khata",
        "reraNumber",
        "jumboPriceEstimateAmountMicros",
        "googleRating",
        "modelFlat",
        "ocreceived",
        "amenities",
        "waterSource",
        "builderCategory"
    FROM workspace_1l3urgumjmspnjxohclmfz6fx._building
    WHERE "id" = '88eff2d1-8028-40c0-8789-60ceee1c30fb' AND "deletedAt" IS NULL
""")

print("=== CRM Building Data ===")
parts = crm_data.split('|')
fields = ['name','locality','nearestLandmark','fulladrress','totalFloors','totalUnits','acres','latitude','longitude','mapLink','khata','reraNumber','jumboPriceEstimateAmountMicros','googleRating','modelFlat','ocreceived','amenities','waterSource','builderCategory']
for f, v in zip(fields, parts):
    print(f'  {f}: {v}')

# Step 2: Map CRM fields to Supabase fields per the sheet
# Per the Google Sheet mapping:
# building.name <- name
# building.locality <- locality
# building.nearest_landmark <- nearestLandmark
# building.full_address <- fulladrress
# building.total_floors <- totalFloors
# building.total_units <- totalUnits
# building.acres <- acres
# building.latitude <- latitude
# building.longitude <- longitude
# building.map_link <- mapLink
# building.khata <- khata (need to map enum: "Khata A" -> "KHATA_A")
# building.rera_number <- reraNumber
# building.price_estimate <- jumboPriceEstimateAmountMicros
# building.google_rating <- googleRating
# building.has_model_flat <- modelFlat
# building.has_occupancy_certificate <- ocreceived
# building_amenities.amenity <- amenities (array -> 1:N rows)
# building_water_sources.waterSource <- waterSource (array -> 1:N rows)
# building.building_tier <- builderCategory

# Map khata enum
khata_map = {
    'Khata A': 'KHATA_A',
    'Khata B': 'KHATA_B',
    'Khata E': 'KHATA_E',
    'A Khata': 'KHATA_A',
    'B Khata': 'KHATA_B',
    'E Khata': 'KHATA_A',
}

# Parse CRM values
crm = dict(zip(fields, parts))
crm['khata'] = khata_map.get(crm['khata'], crm['khata']) if crm['khata'] else None

# Normalize locality: replace spaces with underscores, uppercase
if crm['locality']:
    crm['locality'] = crm['locality'].strip().replace(' ', '_').upper()

# Handle empty strings as None
for k, v in crm.items():
    if v == '' or v == 'None' or v is None:
        crm[k] = None

# Convert types
def to_float(v):
    try: return float(v)
    except: return None

def to_int(v):
    try: return int(float(v))
    except: return None

# Step 3: Update Supabase
pwd = '870SW5q7hto4mraa'
conn = psycopg2.connect(
    host='aws-1-ap-south-1.pooler.supabase.com', port=6543,
    user='postgres.dcukqjnvgyhnynsxpkzx', password=pwd, dbname='postgres',
    connect_timeout=15
)
cur = conn.cursor()

# Update building fields
cur.execute("""
    UPDATE building SET
        "name" = %s,
        "locality" = %s,
        "nearest_landmark" = %s,
        "full_address" = %s,
        "total_floors" = %s,
        "total_units" = %s,
        "acres" = %s,
        "latitude" = %s,
        "longitude" = %s,
        "map_link" = %s,
        "khata" = %s,
        "rera_number" = %s,
        "price_estimate" = %s,
        "google_rating" = %s,
        "has_model_flat" = %s,
        "has_occupancy_certificate" = %s,
        "building_tier" = %s,
        "updated_at" = NOW()
    WHERE "id" = %s
""", (
    crm['name'],
    crm['locality'],
    crm['nearestLandmark'],
    crm['fulladrress'],
    to_float(crm['totalFloors']),
    to_int(crm['totalUnits']),
    to_float(crm['acres']),
    to_float(crm['latitude']),
    to_float(crm['longitude']),
    crm['mapLink'],
    crm['khata'],
    crm['reraNumber'],
    to_int(crm['jumboPriceEstimateAmountMicros']),
    to_float(crm['googleRating']),
    crm['modelFlat'] == 'true' if crm['modelFlat'] else False,
    crm['ocreceived'] == 'true' if crm['ocreceived'] else False,
    crm['builderCategory'],  # building_tier
    '59e6e6da-2c5c-4ab2-bd9b-dccf6a20ab25'
))
print(f"\nUpdated building: {cur.rowcount} rows")

# Step 4: Sync amenities (delete existing, re-insert from CRM)
amenities_raw = crm.get('amenities', '')
if amenities_raw and amenities_raw != '{}' and amenities_raw:
    # Parse PostgreSQL array format: {GYM,SWIMMING_POOL}
    amenities = [a.strip() for a in amenities_raw.strip('{}').split(',') if a.strip()]
    print(f"Amenities from CRM: {amenities}")
    
    # Delete existing amenities for this building
    cur.execute('DELETE FROM building_amenities WHERE "building_id" = %s', 
                ('59e6e6da-2c5c-4ab2-bd9b-dccf6a20ab25',))
    print(f"Deleted existing amenities: {cur.rowcount} rows")
    
    # Insert new amenities
    for amenity in amenities:
        cur.execute(
            'INSERT INTO building_amenities ("building_id", "amenity") VALUES (%s, %s)',
            ('59e6e6da-2c5c-4ab2-bd9b-dccf6a20ab25', amenity)
        )
    print(f"Inserted {len(amenities)} amenities")
else:
    print("No amenities in CRM")

# Step 5: Sync water sources
water_raw = crm.get('waterSource', '')
if water_raw and water_raw != '{}' and water_raw:
    sources = [s.strip() for s in water_raw.strip('{}').split(',') if s.strip()]
    print(f"Water sources from CRM: {sources}")
    
    cur.execute('DELETE FROM building_water_sources WHERE "building_id" = %s',
                ('59e6e6da-2c5c-4ab2-bd9b-dccf6a20ab25',))
    print(f"Deleted existing water sources: {cur.rowcount} rows")
    
    for source in sources:
        cur.execute(
            'INSERT INTO building_water_sources ("building_id", "water_source") VALUES (%s, %s)',
            ('59e6e6da-2c5c-4ab2-bd9b-dccf6a20ab25', source)
        )
    print(f"Inserted {len(sources)} water sources")
else:
    print("No water sources in CRM")

conn.commit()

# Step 6: Verify
cur.execute('SELECT "name", "locality", "khata", "total_floors", "total_units", "google_rating", "has_model_flat" FROM building WHERE "id" = %s', 
            ('59e6e6da-2c5c-4ab2-bd9b-dccf6a20ab25',))
r = cur.fetchone()
print(f"\n=== Supabase after sync ===")
print(f"  name: {r[0]}")
print(f"  locality: {r[1]}")
print(f"  khata: {r[2]}")
print(f"  total_floors: {r[3]}")
print(f"  total_units: {r[4]}")
print(f"  google_rating: {r[5]}")
print(f"  has_model_flat: {r[6]}")

cur.execute('SELECT "amenity" FROM building_amenities WHERE "building_id" = %s', 
            ('59e6e6da-2c5c-4ab2-bd9b-dccf6a20ab25',))
amenities = cur.fetchall()
print(f"  amenities: {[a[0] for a in amenities]}")

cur.execute('SELECT "water_source" FROM building_water_sources WHERE "building_id" = %s',
            ('59e6e6da-2c5c-4ab2-bd9b-dccf6a20ab25',))
sources = cur.fetchall()
print(f"  water_sources: {[s[0] for s in sources]}")

conn.close()
print("\n=== TEST COMPLETE ===")
