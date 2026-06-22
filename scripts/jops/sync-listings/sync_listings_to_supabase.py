"""
Task 4: Listing Data Sync (CRM -> Supabase)
Sync all listing field values from Twenty CRM to Supabase.
Handles direct field mapping, enum transforms, USP expansion, and photo sync.

Usage:
  python3 sync_listings_to_supabase.py                       # sync all 1902 matched listings
  python3 sync_listings_to_supabase.py --id <supabase_id>    # sync one listing
  python3 sync_listings_to_supabase.py --ids <id1,id2,...>   # sync specific listings
  python3 sync_listings_to_supabase.py --limit 5             # sync first 5 (testing)
  python3 sync_listings_to_supabase.py --check               # dry run (show what would sync)
"""
import psycopg2, subprocess, json, sys

DRY_RUN = '--check' in sys.argv
WS = 'workspace_1l3urgumjmspnjxohclmfz6fx'

# ---- Parse args ----
SINGLE_ID = None
MULTI_IDS = None
LIMIT = None
for i, arg in enumerate(sys.argv):
    if arg == '--id' and i + 1 < len(sys.argv):
        SINGLE_ID = sys.argv[i + 1]
    elif arg == '--ids' and i + 1 < len(sys.argv):
        MULTI_IDS = [x.strip() for x in sys.argv[i + 1].split(',')]
    elif arg == '--limit' and i + 1 < len(sys.argv):
        LIMIT = int(sys.argv[i + 1])

# ---- DB Connections ----
def get_supabase_conn():
    return psycopg2.connect(
        host='aws-1-ap-south-1.pooler.supabase.com', port=6543,
        user='postgres.dcukqjnvgyhnynsxpkzx', password='870SW5q7hto4mraa', dbname='postgres'
    )

def crm_query(sql, timeout=30):
    result = subprocess.run(
        ['docker', 'exec', 'twenty-db-1', 'psql', '-U', 'twenty', '-d', 'default',
         '-t', '-A', '-F', '|', '-c', sql],
        capture_output=True, text=True, timeout=timeout
    )
    return result.stdout.strip()

# ---- Enum Mappings ----
STATUS_MAP = {
    'INSPECTION_PENDING': 'INSPECTION_PENDING',
    'CATALOGUE_PENDING': 'CATALOGUE_PENDING',
    'LIVE': 'LIVE',
    'ON_HOLD': 'ON_HOLD',
    'SOLD': 'SOLD',
    'OFFBOARDED': None,
}
FURNISHING_MAP = {
    'UNFURNISHED': 'UNFURNISHED',
    'SEMI_FURNISHED': 'SEMI_FURNISHED',
    'FULLY_FURNISHED': 'FURNISHED',
    'FURNISHED': 'FURNISHED',
}
FACING_MAP = {
    'NORTH': 'NORTH', 'SOUTH': 'SOUTH', 'EAST': 'EAST', 'WEST': 'WEST',
    'NORTH_EAST': 'NORTH_EAST', 'NORTH_WEST': 'NORTH_WEST',
    'SOUTH_EAST': 'SOUTH_EAST', 'SOUTH_WEST': 'SOUTH_WEST',
}
OCCUPANCY_MAP = {
    'VACANT': 'VACANT', 'TENANT_OCCUPIED': 'TENANT_OCCUPIED',
    'OWNER_OCCUPIED': 'OWNER_OCCUPIED', 'BUILDER_OCCUPIED': 'BUILDER_OCCUPIED',
}
CONFIG_MAP = {
    # Standard
    '1 BHK': 'ONE_BHK', '2 BHK': 'TWO_BHK', '2.5 BHK': 'TWO_POINT_FIVE_BHK',
    '3 BHK': 'THREE_BHK', '3.5 BHK': 'THREE_POINT_FIVE_BHK', '4 BHK': 'FOUR_BHK',
    '5 BHK': 'FIVE_BHK', 'ONE_BHK': 'ONE_BHK', 'TWO_BHK': 'TWO_BHK',
    'TWO_POINT_FIVE_BHK': 'TWO_POINT_FIVE_BHK', 'THREE_BHK': 'THREE_BHK',
    'THREE_POINT_FIVE_BHK': 'THREE_POINT_FIVE_BHK', 'FOUR_BHK': 'FOUR_BHK',
    'FIVE_BHK': 'FIVE_BHK',
    # Optional/studio variants (CRM-specific, map to closest standard)
    'OPT1_BHK': 'ONE_BHK',
    'OPT2_BHK': 'TWO_BHK',
    'OPT2_5_BHK': 'TWO_POINT_FIVE_BHK',
    'OPT3_BHK': 'THREE_BHK',
    'OPT3_5_BHK': 'THREE_POINT_FIVE_BHK',
    'OPT4_BHK': 'FOUR_BHK',
    'OPT5_BHK': 'FIVE_BHK',
}
PROPERTY_TYPE_MAP = {
    'APARTMENT': 'APARTMENT', 'VILLA': 'VILLA', 'PENTHOUSE': 'PENTHOUSE',
    'STUDIO': 'STUDIO', 'COMMERCIAL': 'COMMERCIAL',
}
URGENCY_MAP = {
    'IMMEDIATE': 'IMMEDIATE', 'THREE_MONTHS': 'THREE_MONTHS',
    'SIX_MONTHS': 'SIX_MONTHS', 'BEST_PRICE': 'BEST_PRICE',
}
EKHATA_MAP = {
    'E_KHATA_AVAILABLE': True,
    'E_KHATA_NOT_AVAILABLE': False,
    'YES': True, 'NO': False, 'PENDING': None, 'NA': None,
}
HOME_TYPE_MAP = {
    'GHGP': 'GHGP', 'GHBP': 'GHBP', 'BHGP': 'BHGP', 'BHBP': 'BHBP',
    'RESALE': 'RESALE', 'NEW': 'NEW', 'UNDER_CONSTRUCTION': 'UNDER_CONSTRUCTION',
}
INVENTORY_TYPE_MAP = {
    'OPEN': 'OPEN', 'EXCLUSIVE': 'EXCLUSIVE', 'CO_BROKE': 'CO_BROKE',
    'READY_TO_MOVE': 'READY_TO_MOVE', 'UNDER_CONSTRUCTION': 'UNDER_CONSTRUCTION',
}

# CRM photo tag → Supabase media_category enum
PHOTO_TAG_MAP = {
    'Bedroom': 'LISTING_IMAGE_BEDROOM_1',
    'Kitchen': 'LISTING_IMAGE_KITCHEN',
    'Bathroom': 'LISTING_IMAGE_BATHROOM_1',
    'Balcony': 'LISTING_IMAGE_BALCONY_1',
    'Living Room': 'LISTING_IMAGE_LIVING_ROOM',
    'Parking': 'LISTING_IMAGE_PARKING',
    'Floor Plan': 'LISTING_FLOOR_PLAN',
}

def _map_photo_tag_to_category(tag):
    """Map CRM photo tag to Supabase media category enum value."""
    if not tag:
        return 'LISTING_IMAGE_BEDROOM_1'  # fallback
    return PHOTO_TAG_MAP.get(tag, 'LISTING_IMAGE_BEDROOM_1')

URGENCY_MAP = {
    'IMMEDIATE': 'IMMEDIATE', 'THREE_MONTHS': 'THREE_MONTHS',
    'SIX_MONTHS': 'SIX_MONTHS', 'BEST_PRICE': 'BEST_PRICE',
    'LESS_THAN_1M': 'IMMEDIATE',  # Map to closest
}

def safe_int(v, default=0):
    try:
        return int(float(v)) if v and v not in ('None', '', 'null') else default
    except (ValueError, TypeError):
        return default

def safe_float(v, default=0):
    try:
        return float(v) if v and v not in ('None', '', 'null') else default
    except (ValueError, TypeError):
        return default

def safe_bool(v):
    if v is None or v in ('None', '', 'null', 'f', 'false', '0'):
        return False
    return str(v).lower() in ('true', 't', '1', 'yes')

def safe_str(v):
    if v is None or v in ('None', '', 'null'):
        return None
    return str(v).strip()

def micros_to_lakhs(v):
    if v is None:
        return None
    return round(v / 1_000_000, 2)

def parse_jsonb(val):
    if not val or val in ('None', '', 'null'):
        return None
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None

def map_enum(val, mapping, default=None):
    if not val or val in ('None', '', 'null'):
        return default
    if val in mapping:
        return mapping[val]  # Could be None (explicit unmapped)
    return default if default is not None else val

# ---- MAIN ----
print("=== Listing Data Sync (CRM -> Supabase) ===")
print(f"  Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")

conn = get_supabase_conn()
cur = conn.cursor()

# Get listings to sync
query = '''
    SELECT l."id", l."internal_id", l."glide_serial_number"
    FROM listing l
    WHERE l."internal_id" IS NOT NULL
'''
if SINGLE_ID:
    query += f" AND l.\"id\" = '{SINGLE_ID}'"
elif MULTI_IDS:
    ids_str = ','.join(f"'{x}'" for x in MULTI_IDS)
    query += f" AND l.\"id\" IN ({ids_str})"
if LIMIT:
    query += f" LIMIT {LIMIT}"

cur.execute(query)
listings = cur.fetchall()
print(f"  Listings to sync: {len(listings)}")

if not listings:
    print("  No listings found.")
    conn.close()
    sys.exit(0)

synced = 0
errors = 0
skipped = 0
total_photos = 0
total_usps = 0

for supabase_id, internal_id, glide_sn in listings:
    crm_id = internal_id

    try:
        # Fetch CRM property data
        crm_raw = crm_query(f"""
            SELECT
                "name", "serialNumber"::int, "flatNumber", "floor", "facing",
                "configuration", "bedrooms", "bathrooms", "balcony", "parking",
                "carpetArea", "squareFeet", "mspAmountMicros",
                "latestPriceAmountMicros", "occupancy", "furnishing",
                "propertyStatus", "eKhata", "balconyView1", "balconyView2",
                "balconyView3", "balconyView4", "homeType", "inventoryType",
                "propertyType", "sellerUrgency", "maintenanceAmountMicros",
                "usp1", "usp2", "usp3", "files", "lpg", "offboarding",
                "undividedShare", "spotlight"
            FROM {WS}._property
            WHERE "id" = '{crm_id}' AND "deletedAt" IS NULL
        """)

        if not crm_raw:
            print(f"  SKIP: {supabase_id} (CRM {crm_id} not found/deleted)")
            skipped += 1
            continue

        p = crm_raw.split('|')
        # idx: 0=name, 1=sn, 2=flat, 3=floor, 4=facing, 5=config, 6=bed, 7=bath,
        # 8=balc, 9=park, 10=carpet, 11=sqft, 12=msp, 13=price, 14=occ,
        # 15=furn, 16=status, 17=ekhata, 18-21=balcView1-4, 22=homeType,
        # 23=invType, 24=propType, 25=urgency, 26=maint, 27-29=usp1-3,
        # 30=files, 31=lpg, 32=offboard, 33=undivided, 34=spotlight

        status_raw = safe_str(p[16]) if len(p) > 16 else None
        status = map_enum(status_raw, STATUS_MAP)
        if status is None and status_raw == 'OFFBOARDED':
            # Map OFFBOARDED to a sensible value or skip
            status = 'ON_HOLD'  # fallback

        # Build update params with defaults for NOT NULL columns
        # Supabase NOT NULL columns: bedrooms, bathrooms, facing, configuration,
        # carpet_area, super_builtup_area, ask_price, status, furnishing, building_id, parkings
        # Use 0 for numeric, existing values for enums when CRM is null
        update_params = {
            'flat_no': safe_str(p[2]) if len(p) > 2 else None,
            'floor': safe_int(p[3]) if len(p) > 3 else None,
            'facing': map_enum(safe_str(p[4]) if len(p) > 4 else None, FACING_MAP, 'NORTH'),
            'configuration': map_enum(safe_str(p[5]) if len(p) > 5 else None, CONFIG_MAP, 'TWO_BHK'),
            'bedrooms': safe_int(p[6]) if len(p) > 6 else 0,
            'bathrooms': safe_int(p[7]) if len(p) > 7 else 0,
            'balconies': safe_int(p[8]) if len(p) > 8 else 0,
            'parkings': safe_int(p[9]) if len(p) > 9 else 0,
            'carpet_area': safe_float(p[10]) if len(p) > 10 else 0,
            'super_builtup_area': safe_float(p[11]) if len(p) > 11 else 0,
            'msp': micros_to_lakhs(safe_float(p[12]) if len(p) > 12 else None),
            'ask_price': micros_to_lakhs(safe_float(p[13]) if len(p) > 13 else 0),
            'occupancy': map_enum(safe_str(p[14]) if len(p) > 14 else None, OCCUPANCY_MAP, 'VACANT'),
            'furnishing': map_enum(safe_str(p[15]) if len(p) > 15 else None, FURNISHING_MAP, 'UNFURNISHED'),
            'status': status or 'INSPECTION_PENDING',
            'has_ekhata': map_enum(safe_str(p[17]) if len(p) > 17 else None, EKHATA_MAP),
            'balcony_view_1': safe_str(p[18]) if len(p) > 18 else None,
            'balcony_view_2': safe_str(p[19]) if len(p) > 19 else None,
            'balcony_view_3': safe_str(p[20]) if len(p) > 20 else None,
            'balcony_view_4': safe_str(p[21]) if len(p) > 21 else None,
            'home_type': map_enum(safe_str(p[22]) if len(p) > 22 else None, HOME_TYPE_MAP),
            'inventory_type': map_enum(safe_str(p[23]) if len(p) > 23 else None, INVENTORY_TYPE_MAP),
            'property_type': map_enum(safe_str(p[24]) if len(p) > 24 else None, PROPERTY_TYPE_MAP, 'APARTMENT'),
            'seller_urgency': map_enum(safe_str(p[25]) if len(p) > 25 else None, URGENCY_MAP),
            'maintenance': micros_to_lakhs(safe_float(p[26]) if len(p) > 26 else None),
            'has_lowest_price': safe_bool(p[31]) if len(p) > 31 else False,
        }

        # Supabase column names for UPDATE
        sb_cols = [
            '"flat_no"', '"floor"', '"facing"', '"configuration"', '"bedrooms"',
            '"bathrooms"', '"balconies"', '"parkings"', '"carpet_area"',
            '"super_builtup_area"', '"msp"', '"ask_price"', '"occupancy"',
            '"furnishing"', '"status"', '"has_ekhata"', '"balcony_view_1"',
            '"balcony_view_2"', '"balcony_view_3"', '"balcony_view_4"',
            '"home_type"', '"inventory_type"', '"property_type"', '"seller_urgency"',
            '"maintenance"', '"has_lowest_price"', '"updated_at"'
        ]
        sb_vals = [
            update_params['flat_no'], update_params['floor'], update_params['facing'],
            update_params['configuration'], update_params['bedrooms'], update_params['bathrooms'],
            update_params['balconies'], update_params['parkings'], update_params['carpet_area'],
            update_params['super_builtup_area'], update_params['msp'], update_params['ask_price'],
            update_params['occupancy'], update_params['furnishing'], update_params['status'],
            update_params['has_ekhata'], update_params['balcony_view_1'],
            update_params['balcony_view_2'], update_params['balcony_view_3'],
            update_params['balcony_view_4'], update_params['home_type'],
            update_params['inventory_type'], update_params['property_type'],
            update_params['seller_urgency'], update_params['maintenance'],
            update_params['has_lowest_price'], 'NOW()'
        ]

        if DRY_RUN:
            print(f"  [DRY] Would sync sn={glide_sn}: status={update_params['status']}, price={update_params['ask_price']}, furnishing={update_params['furnishing']}")
            synced += 1
            continue

        # Update listing main fields
        set_clause = ', '.join(f'{c} = %s' for c in sb_cols)
        cur.execute(
            f'UPDATE listing SET {set_clause} WHERE "id" = %s',
            sb_vals + [supabase_id]
        )

        # Sync USPs (usp1/usp2/usp3 -> listing_usps rows)
        usps = []
        for usp_idx in range(27, 30):
            val = safe_str(p[usp_idx]) if len(p) > usp_idx else None
            if val:
                usps.append(val)

        if usps:
            # Delete existing USPs
            cur.execute('DELETE FROM listing_usps WHERE "listing_id" = %s', (supabase_id,))
            for usp_val in usps:
                cur.execute(
                    'INSERT INTO listing_usps ("listing_id", "usp", "created_at", "updated_at") VALUES (%s, %s, NOW(), NOW())',
                    (supabase_id, usp_val)
                )
            total_usps += len(usps)

        # Sync photos from _property.files (Cloudinary URLs)
        files_raw = safe_str(p[30]) if len(p) > 30 else None
        photo_count = 0
        if files_raw and files_raw != 'null':
            photo_list = parse_jsonb(files_raw)
            if photo_list and isinstance(photo_list, list):
                # Delete existing media for this listing
                cur.execute('DELETE FROM media WHERE "listing_id" = %s', (supabase_id,))
                for photo in photo_list:
                    if photo.get('source') == 'cloudinary' and photo.get('url'):
                        # Map CRM tag to Supabase media category
                        tag = photo.get('tag', '')
                        category = _map_photo_tag_to_category(tag)
                        cur.execute(
                            '''INSERT INTO media ("url", "type", "category", "is_active", "listing_id", "created_at", "updated_at")
                               VALUES (%s, %s, %s, %s, %s, NOW(), NOW())''',
                            (photo['url'], 'IMAGE', category, True, supabase_id)
                        )
                        photo_count += 1
                        total_photos += 1

        synced += 1
        conn.commit()
        print(f"  OK: sn={glide_sn}, supabase={supabase_id[:8]}... (fields + {len(usps)} usps + {photo_count} photos)")

    except Exception as e:
        errors += 1
        conn.rollback()
        print(f"  ERROR: sn={glide_sn}, supabase={supabase_id[:8]}...: {e}")

print(f"\n=== Summary ===")
print(f"  Synced: {synced}")
print(f"  Skipped: {skipped}")
print(f"  Errors: {errors}")
print(f"  Total photos synced: {total_photos}")
print(f"  Total USPs synced: {total_usps}")

conn.close()
print("=== Done ===")
