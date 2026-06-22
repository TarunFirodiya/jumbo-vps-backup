#!/usr/bin/env python3
"""
JUM-553 Task 3: Sync building data from Twenty CRM → Supabase.

Reads building records from CRM (_building table) and upserts into Supabase
building table + building_amenities + building_water_sources junction tables.

The CRM _building.id maps to Supabase building.internal_id (populated in Task 1).

Usage:
    python3 sync_buildings_to_supabase.py              # full backfill (all 625)
    python3 sync_buildings_to_supabase.py --limit 5     # test on 5 buildings
    python3 sync_buildings_to_supabase.py --ids id1,id2  # specific Supabase UUIDs

Reference:
    - Field mapping: jum-553-sheet-mapping.md section #1
    - Workflow rules: jum-553-sync-workflow.md Task 3
"""

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from io import StringIO

import psycopg2
from psycopg2.extras import execute_values

# ─── Config ───────────────────────────────────────────────────────────────────

CRM_DB = "twenty-db-1"
WS = "workspace_1l3urgumjmspnjxohclmfz6fx"

SUPABASE_CONFIG = dict(
    host="aws-1-ap-south-1.pooler.supabase.com",
    port=6543,
    user="postgres.dcukqjnvgyhnynsxpkzx",
    password="870SW5q7hto4mraa",
    dbname="postgres",
    connect_timeout=15,
)

# ─── Enum Mappings ─────────────────────────────────────────────────────────────

# CRM khata (text) → Supabase building_khata_type
KHATA_MAP = {
    "Khata A": "KHATA_A",
    "Khata B": "KHATA_B",
}

# CRM builderCategory → Supabase building_tier_type
# Note: CRM has MARQUEE, Supabase has MARQUE
TIER_MAP = {
    "MARQUEE": "MARQUE",
    "HEAD": "HEAD",
    "TORSO": "TORSO",
    "TAIL": "TAIL",
}

# CRM amenities enum → Supabase building_amenities_type
AMENITY_MAP = {
    "POOL": "SWIMMING_POOL",
    "GYM": "GYM",
    "PARKING": None,           # no Supabase equivalent
    "SECURITY": None,          # no Supabase equivalent
    "ELEVATOR": None,          # no Supabase equivalent
    "CLUBHOUSE": "CLUBHOUSE",
    "PLAY_AREA": "CHILDRENS_PARK",
    "INDOOR_SPORTS": "INDOOR_SPORTS",
    "TABLE_TENNIS": "TABLE_TENNIS",
    "BADMINTON": "BADMINTON",
    "BASKETBALL": "BASKETBALL_COURT",
    "TENNIS": None,            # no Supabase equivalent
    "VOLLEYBALL": None,        # no Supabase equivalent
    "CRICKET": "CRICKET",
    "SQUASH": None,            # no Supabase equivalent
    "CO_WORKING": "CO_WORKING_SPACE",
    "CAFE": None,              # no Supabase equivalent
    "CRECHE_DAYCARE": "CRECHE_PLAY_GROUPS",
    "LIBRARY": "LIBRARY",
    "OPEN_GREEN_SPACE": "OPEN_GREEN_SPACE",
    "PICKLEBALL": None,        # no Supabase equivalent
    "SKATING": None,           # no Supabase equivalent
    "SPA": "SPA",
    "OPTION_24": None,         # no Supabase equivalent
    "KID_S_POOL": "CHILDRENS_POOL",
    "FIRE_SAFETY": None,       # no Supabase equivalent
    "RAIN_WATER_HARVESTING": "RAIN_WATER_HARVESTING",
    "SEWAGE_TREATMENT": "SEWAGE_TREATMENT_PLANT",
    "WATER_TREATMENT": None,   # no Supabase equivalent
    "GAS_PIPELINE": "GAS_PIPES",
    "DEDICATED_PARKING": None, # no Supabase equivalent
    "VISITOR_PARKING": None,   # no Supabase equivalent
    "CARDS_ROOM": "CARDS_ROOM",
    "MINI_THEATRE": "MINI_THEATER",
    "AMPITHEATRE": "AMPHITHEATER",
    "POOL_TABLE": "POOL_TABLE",
    "SNOOKER_TABLE": None,     # no Supabase equivalent
    "YOGA_ROOM": None,         # no Supabase equivalent
}

# CRM waterSource enum → Supabase building_water
WATER_MAP = {
    "CAUVERY": "CAUVERY",
    "WATER_TANKER": "WATER_TANKER",
    "BOREWELL": "BOREWELL",
}

# CRM modelFlat enum → boolean has_model_flat
MODEL_FLAT_MAP = {
    "MODEL_FLAT_AVAILABLE": True,
    "MODEL_FLAT_NOT_AVAILABLE": False,
}

# CRM ocreceived enum → boolean has_occupancy_certificate
OC_MAP = {
    "OC_RECEIVED": True,
    "OC_NOT_RECEIVED": False,
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def safe_int(v, default=None):
    try:
        return int(float(v)) if v and str(v) not in ("None", "", "null", "nan") else default
    except (ValueError, TypeError, OverflowError):
        return default


def safe_float(v, default=None):
    try:
        return float(v) if v and str(v) not in ("None", "", "null", "nan") else default
    except (ValueError, TypeError):
        return default


def safe_str(v):
    if v is None or str(v) in ("None", "", "null", "nan"):
        return None
    s = str(v).strip()
    return s if s else None


def parse_pg_array(val):
    """Parse a PostgreSQL array literal like {A,B,C} into a list of strings."""
    if not val or str(val) in ("None", "", "null", "nan"):
        return []
    s = str(val).strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    if not s:
        return []
    return [item.strip() for item in s.split(",") if item.strip()]


def map_enum(val, mapping, default=None):
    if not val or str(val) in ("None", "", "null", "nan"):
        return default
    if val in mapping:
        return mapping[val]  # Could be None (explicit unmapped)
    return default if default is not None else val


# ─── Step 1: Get building IDs from Supabase ──────────────────────────────────

def get_supabase_building_ids(supabase_uuids=None):
    """
    Get list of (supabase_id, crm_id) for buildings that have internal_id.
    crm_id = CRM _building.id = Supabase building.internal_id.
    """
    conn = psycopg2.connect(**SUPABASE_CONFIG)
    cur = conn.cursor()

    if supabase_uuids:
        placeholders = ",".join(["%s"] * len(supabase_uuids))
        cur.execute(
            f"SELECT id, internal_id FROM building WHERE internal_id IS NOT NULL AND id IN ({placeholders})",
            supabase_uuids,
        )
    else:
        cur.execute("SELECT id, internal_id FROM building WHERE internal_id IS NOT NULL ORDER BY name")

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows  # [(supabase_uuid, crm_uuid), ...]


# ─── Step 2: Export from CRM ─────────────────────────────────────────────────

def export_buildings_from_crm(crm_ids, limit=None):
    """
    Export building data from CRM for the given CRM IDs (internal_id values).
    Uses COPY TO STDOUT for efficient bulk export.
    """
    if not crm_ids:
        return []

    ids_sql = ", ".join(f"'{i}'" for i in crm_ids)
    limit_clause = f"LIMIT {int(limit)}" if limit else ""

    query = f"""
    COPY (
        SELECT
            b."id"                AS crm_id,
            b."name",
            b."locality",
            b."nearestLandmark",
            b."fulladrress",
            b."totalFloors",
            b."totalfloors",
            b."totalUnits",
            b."acres",
            b."latitude",
            b."longitude",
            b."mapLink",
            b."khata",
            b."reraNumber",
            b."builderCategory",
            b."modelFlat",
            b."googleRating",
            b."ocreceived",
            b."amenities",
            b."waterSource",
            b."createdAt",
            b."updatedAt"
        FROM "{WS}"."_building" b
        WHERE b."id" IN ({ids_sql})
          AND b."deletedAt" IS NULL
        ORDER BY b."name"
        {limit_clause}
    ) TO STDOUT WITH CSV HEADER
    """

    cmd = [
        "docker", "exec", "-i", CRM_DB,
        "psql", "-U", "twenty", "-d", "default", "-c", query,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"CRM export failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    reader = csv.DictReader(result.stdout.splitlines())
    return list(reader)


# ─── Step 3: Sync to Supabase ────────────────────────────────────────────────

def sync_buildings(buildings, id_map):
    """
    Upsert buildings into Supabase.
    id_map: {crm_id: supabase_id}
    Returns (synced, errors) counts.
    """
    conn = psycopg2.connect(**SUPABASE_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    synced = 0
    errors = 0
    amenity_rows = []
    water_rows = []
    skipped_amenities = {}
    skipped_water = {}

    for bld in buildings:
        try:
            crm_id = bld["crm_id"]
            supabase_id = id_map.get(crm_id)

            if not supabase_id:
                print(f"  SKIP {crm_id}: no Supabase mapping")
                errors += 1
                continue

            # ── Scalar fields ──
            name = safe_str(bld.get("name"))
            if not name:
                print(f"  SKIP {crm_id}: no name")
                errors += 1
                continue

            # Supabase locality is authoritative — do NOT overwrite
            nearest_landmark = safe_str(bld.get("nearestLandmark"))
            full_address = safe_str(bld.get("fulladrress"))

            # totalFloors: prefer totalfloors (where data actually is), fallback to totalFloors
            total_floors = safe_int(bld.get("totalfloors")) or safe_int(bld.get("totalFloors"))
            total_units = safe_int(bld.get("totalUnits"))
            acres = safe_float(bld.get("acres"))
            latitude = safe_float(bld.get("latitude"))
            longitude = safe_float(bld.get("longitude"))
            map_link = safe_str(bld.get("mapLink"))

            # Khata: map enum
            khata = map_enum(bld.get("khata"), KHATA_MAP)

            rera_number = safe_str(bld.get("reraNumber"))

            # Building tier: MARQUEE → MARQUE
            building_tier = map_enum(bld.get("builderCategory"), TIER_MAP)

            # modelFlat → boolean
            has_model_flat = map_enum(bld.get("modelFlat"), MODEL_FLAT_MAP)

            # googleRating
            google_rating = safe_float(bld.get("googleRating"))

            # ocreceived → boolean
            has_oc = map_enum(bld.get("ocreceived"), OC_MAP)

            # ── Upsert building ──
            # Note: locality is NOT updated (Supabase is authoritative, per workflow rule #7)
            cur.execute("""
                UPDATE building SET
                    name = COALESCE(%(name)s, name),
                    nearest_landmark = COALESCE(%(nearest_landmark)s, nearest_landmark),
                    full_address = COALESCE(%(full_address)s, full_address),
                    total_floors = COALESCE(%(total_floors)s, total_floors),
                    total_units = COALESCE(%(total_units)s, total_units),
                    acres = COALESCE(%(acres)s, acres),
                    latitude = COALESCE(%(latitude)s, latitude),
                    longitude = COALESCE(%(longitude)s, longitude),
                    map_link = COALESCE(%(map_link)s, map_link),
                    khata = COALESCE(%(khata)s, khata),
                    rera_number = COALESCE(%(rera_number)s, rera_number),
                    building_tier = COALESCE(%(building_tier)s, building_tier),
                    has_model_flat = COALESCE(%(has_model_flat)s, has_model_flat),
                    google_rating = COALESCE(%(google_rating)s, google_rating),
                    has_occupancy_certificate = COALESCE(%(has_oc)s, has_occupancy_certificate),
                    updated_at = NOW()
                WHERE id = %(supabase_id)s
            """, dict(
                name=name,
                nearest_landmark=nearest_landmark,
                full_address=full_address,
                total_floors=total_floors,
                total_units=total_units,
                acres=acres,
                latitude=latitude,
                longitude=longitude,
                map_link=map_link,
                khata=khata,
                rera_number=rera_number,
                building_tier=building_tier,
                has_model_flat=has_model_flat,
                google_rating=google_rating,
                has_oc=has_oc,
                supabase_id=supabase_id,
            ))

            if cur.rowcount == 0:
                print(f"  WARN {crm_id}: no row updated for supabase_id={supabase_id}")
                errors += 1
                continue

            # ── Delete + re-insert amenities (idempotent) ──
            cur.execute("DELETE FROM building_amenities WHERE building_id = %s", (supabase_id,))

            crm_amenities = parse_pg_array(bld.get("amenities"))
            for amenity_val in crm_amenities:
                mapped = map_enum(amenity_val, AMENITY_MAP)
                if mapped is not None:
                    amenity_rows.append((supabase_id, mapped))
                else:
                    skipped_amenities[amenity_val] = skipped_amenities.get(amenity_val, 0) + 1

            # ── Delete + re-insert water sources (idempotent) ──
            cur.execute("DELETE FROM building_water_sources WHERE building_id = %s", (supabase_id,))

            crm_water = parse_pg_array(bld.get("waterSource"))
            for water_val in crm_water:
                mapped = map_enum(water_val, WATER_MAP)
                if mapped is not None:
                    water_rows.append((supabase_id, mapped))
                else:
                    skipped_water[water_val] = skipped_water.get(water_val, 0) + 1

            synced += 1

        except Exception as e:
            print(f"  ERROR {bld.get('crm_id', '?')}: {e}", file=sys.stderr)
            errors += 1

    # Bulk insert amenities and water sources
    if amenity_rows:
        execute_values(
            cur,
            "INSERT INTO building_amenities (building_id, amenity) VALUES %s ON CONFLICT DO NOTHING",
            amenity_rows,
            page_size=500,
        )

    if water_rows:
        execute_values(
            cur,
            "INSERT INTO building_water_sources (building_id, water_source) VALUES %s ON CONFLICT DO NOTHING",
            water_rows,
            page_size=500,
        )

    conn.commit()
    cur.close()
    conn.close()

    # Print skipped enum summary
    if skipped_amenities:
        print(f"\n  Skipped amenities (no Supabase mapping):")
        for k, v in sorted(skipped_amenities.items(), key=lambda x: -x[1]):
            print(f"    {k}: {v} occurrences")

    if skipped_water:
        print(f"\n  Skipped water sources (no Supabase mapping):")
        for k, v in sorted(skipped_water.items(), key=lambda x: -x[1]):
            print(f"    {k}: {v} occurrences")

    return synced, errors


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync building data CRM → Supabase")
    parser.add_argument("--limit", type=int, help="Limit to N buildings (for testing)")
    parser.add_argument("--ids", type=str, help="Comma-separated Supabase building UUIDs")
    args = parser.parse_args()

    supabase_uuids = args.ids.split(",") if args.ids else None

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching building ID mappings from Supabase...")
    building_ids = get_supabase_building_ids(supabase_uuids=supabase_uuids)
    print(f"  Found {len(building_ids)} buildings with internal_id")

    if not building_ids:
        print("No buildings to sync. Exiting.")
        return

    # Apply limit before CRM export (more efficient)
    if args.limit:
        building_ids = building_ids[:args.limit]

    # Build lookup: crm_id → supabase_id
    id_map = {crm_id: supabase_id for supabase_id, crm_id in building_ids}
    crm_ids = list(id_map.keys())

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Exporting {len(crm_ids)} buildings from CRM...")
    buildings = export_buildings_from_crm(crm_ids)
    print(f"  Exported {len(buildings)} buildings")

    if not buildings:
        print("No buildings exported. Exiting.")
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Syncing to Supabase...")
    synced, errors = sync_buildings(buildings, id_map)

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Done.")
    print(f"  Synced: {synced}")
    print(f"  Errors: {errors}")
    print(f"  Total:  {len(buildings)}")


if __name__ == "__main__":
    main()
