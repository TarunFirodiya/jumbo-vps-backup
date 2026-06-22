#!/usr/bin/env python3
"""
JUM-621: End-to-end test for ONE property.
Flow: Live property → inspection photos → Cloudinary → _property.files → housing.com payload
"""
import json
import os
import sys
import subprocess
import time
import requests

# Config
with open("/root/.twenty/cloudinary_config.json") as f:
    config = json.load(f)

CLOUDINARY_API_KEY = config["CLOUDINARY_API_KEY"]
CLOUDINARY_API_SECRET = config["CLOUDINARY_API_SECRET"]
CLOUDINARY_CLOUD_NAME = config["CLOUDINARY_CLOUD_NAME"]
CLOUDINARY_UPLOAD_URL = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"

WORKSPACE_SCHEMA = "workspace_1l3urgumjmspnjxohclmfz6fx"
TWENTY_STORAGE_BASE = "/app/packages/twenty-server/.local-storage/1acb6d7e-22d6-44a0-95fa-fd1b7b7be25d"
TWENTY_APP_ID = "2b178109-9d3a-4416-8227-d12e1eacf72a"

# Column name -> Housing.com tag label
PHOTO_TAG_MAP = {
    "bedroom1Photos": "Bedroom",
    "bedroom2Photos": "Bedroom",
    "bedroom3Photos": "Bedroom",
    "bedroom4Photos": "Bedroom",
    "bedroom5Photos": "Bedroom",
    "kitchenPhotos": "Kitchen",
    "livingRoomPhotos": "Living Room",
    "bathroom1Photos": "Bathroom",
    "bathroom2Photos": "Bathroom",
    "bathroom3Photos": "Bathroom",
    "bathroom4Photos": "Bathroom",
    "bathroom5Photos": "Bathroom",
    "balcony1Photos": "Balcony",
    "balcony2Photos": "Balcony",
    "balcony3Photos": "Balcony",
    "balcony4Photos": "Balcony",
    "balcony5Photos": "Balcony",
    "parkingPhotos": "Parking",
}


def db_query(sql):
    """Execute SQL and return rows as list of strings."""
    result = subprocess.run(
        ["docker", "exec", "-i", "twenty-db-1", "psql", "-U", "twenty", "-d", "default", "-t", "-A", "-F", "|"],
        input=sql, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"  DB ERROR: {result.stderr[:300]}")
        return []
    rows = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            rows.append(line.strip())
    return rows


def db_exec(sql):
    """Execute a SQL statement."""
    result = subprocess.run(
        ["docker", "exec", "-i", "twenty-db-1", "psql", "-U", "twenty", "-d", "default"],
        input=sql, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"  DB EXEC ERROR: {result.stderr[:300]}")
        return False
    return True


def get_property_and_inspection(property_id):
    """Get property details and its inspection."""
    # Property details
    sql = f"""
    SELECT p.id, p.name, p."propertyStatus", p."latestPriceAmountMicros",
           p."bedrooms", p."bathrooms", p."squareFeet", p."floor", p."flatNumber",
           p."facing", p."furnishing", p."buildingId",
           b.name as building_name, b.locality, b.latitude, b.longitude
    FROM "{WORKSPACE_SCHEMA}"._property p
    LEFT JOIN "{WORKSPACE_SCHEMA}"._building b ON b.id = p."buildingId"
    WHERE p.id = '{property_id}' AND p."deletedAt" IS NULL
    """
    rows = db_query(sql)
    if not rows:
        print("Property not found")
        return None, None

    parts = rows[0].split("|")
    prop = {
        "id": parts[0], "name": parts[1], "status": parts[2],
        "price_micros": parts[3], "bedrooms": parts[4], "bathrooms": parts[5],
        "sqft": parts[6], "floor": parts[7], "flat": parts[8],
        "facing": parts[9], "furnishing": parts[10], "building_id": parts[11],
        "building_name": parts[12], "locality": parts[13],
        "lat": parts[14] if len(parts) > 14 else None,
        "lng": parts[15] if len(parts) > 15 else None,
    }

    # Inspection with all photo columns
    photo_cols = '", "'.join(PHOTO_TAG_MAP.keys())
    sql = f"""
    SELECT id, "{photo_cols}"
    FROM "{WORKSPACE_SCHEMA}"."_propertyInspection"
    WHERE "propertyId" = '{property_id}' AND "deletedAt" IS NULL
    LIMIT 1
    """
    rows = db_query(sql)
    if not rows:
        print("No inspection found")
        return prop, None

    parts = rows[0].split("|")
    inspection = {"id": parts[0]}
    col_names = list(PHOTO_TAG_MAP.keys())
    for i, col in enumerate(col_names):
        val = parts[i + 1] if i + 1 < len(parts) else None
        if val and val != "null" and val.strip():
            try:
                photos = json.loads(val)
                if isinstance(photos, list):
                    inspection[col] = photos
                else:
                    inspection[col] = []
            except json.JSONDecodeError:
                inspection[col] = []
        else:
            inspection[col] = []

    return prop, inspection


def find_file_in_container(file_id, extension):
    """Find the file in Twenty server container storage."""
    result = subprocess.run(
        ["docker", "exec", "twenty-server-1", "find", TWENTY_STORAGE_BASE,
         "-name", f"{file_id}{extension}", "-type", "f"],
        capture_output=True, text=True, timeout=30
    )
    paths = result.stdout.strip().split("\n")
    for p in paths:
        if p.strip():
            return p.strip()
    return None


def upload_to_cloudinary(file_path, public_id):
    """Upload a local file to Cloudinary."""
    with open(file_path, "rb") as f:
        response = requests.post(
            CLOUDINARY_UPLOAD_URL,
            auth=(CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET),
            data={
                "public_id": public_id,
                "folder": "jumbo_listing",
                "overwrite": "true",
                "resource_type": "image",
            },
            files={"file": f},
            timeout=120
        )
    data = response.json()
    if "secure_url" in data:
        return {
            "public_id": data["public_id"],
            "url": data["secure_url"],
            "format": data.get("format", ""),
            "bytes": data.get("bytes", 0),
        }
    else:
        print(f"  Cloudinary error: {data.get('error', {}).get('message', str(data)[:200])}")
        return None


def save_to_property_files(property_id, images):
    """Save Cloudinary URLs to _property.files jsonb."""
    assets = []
    for img in images:
        assets.append({
            "source": "cloudinary",
            "url": img["url"],
            "public_id": img["public_id"],
            "format": img.get("format", "jpg"),
            "tag": img.get("tag", "Property"),
            "label": f"photo_{img.get('tag', 'property').lower().replace(' ', '_')}",
        })

    assets_json = json.dumps(assets)
    assets_json_escaped = assets_json.replace("'", "''")

    sql = f"""
    UPDATE "{WORKSPACE_SCHEMA}"._property
    SET "files" = '{assets_json_escaped}'::jsonb
    WHERE id = '{property_id}'
    """
    return db_exec(sql), assets


def build_housing_payload(property_data, images):
    """Build the Housing.com API payload with Pic/Tag pairs."""
    # Price: latestPriceAmountMicros is in micros (1 micro = 1/1000000 of base unit)
    # The value is already in the smallest currency unit
    price_value = property_data.get("price_micros", "0")
    try:
        price_num = int(price_value)
        # Convert from micro-lakhs to actual price value for Housing.com
        price_in_rupees = price_num / 1000000  # micros to rupees
    except (ValueError, TypeError):
        price_in_rupees = 0

    payload = {
        "name": property_data.get("name", ""),
        "propertyType": "APARTMENT",
        "bedrooms": int(float(property_data.get("bedrooms", 0))) if property_data.get("bedrooms") else 0,
        "bathrooms": int(float(property_data.get("bathrooms", 0))) if property_data.get("bathrooms") else 0,
        "area": int(float(property_data.get("sqft", 0))) if property_data.get("sqft") else 0,
        "floor": int(float(property_data.get("floor", 0))) if property_data.get("floor") else 0,
        "facing": property_data.get("facing", ""),
        "furnishing": property_data.get("furnishing", ""),
        "buildingName": property_data.get("building_name", ""),
        "locality": property_data.get("locality", ""),
        "city": "Bangalore",
        "price": price_in_rupees,
    }

    # Add lat/lng if available
    if property_data.get("lat") and property_data.get("lng"):
        try:
            payload["latitude"] = float(property_data["lat"])
            payload["longitude"] = float(property_data["lng"])
        except (ValueError, TypeError):
            pass

    # Add sequential Pic/Tag pairs
    pic_num = 1
    for img in images:
        payload[f"Pic {pic_num}"] = img["url"]
        payload[f"Tag {pic_num}"] = img.get("tag", "Property")
        pic_num += 1

    return payload


def get_eligible_properties():
    """Get all LIVE properties with APPROVED inspections that have photos."""
    sql = """
    SELECT p.id, p.name, p."latestPriceAmountMicros", p."bedrooms", p."bathrooms",
           p."squareFeet", p."floor", p."flatNumber", p."facing", p."furnishing",
           p."buildingId", b.name as building_name, b.locality, b.latitude, b.longitude,
           pi.id as inspection_id
    FROM "workspace_1l3urgumjmspnjxohclmfz6fx"._property p
    JOIN "workspace_1l3urgumjmspnjxohclmfz6fx"."_propertyInspection" pi ON pi."propertyId" = p.id
    LEFT JOIN "workspace_1l3urgumjmspnjxohclmfz6fx"._building b ON b.id = p."buildingId"
    WHERE p."propertyStatus" = 'LIVE' AND p."deletedAt" IS NULL
      AND pi."deletedAt" IS NULL AND pi.status = 'APPROVED'
    ORDER BY p.name
    """
    rows = db_query(sql)
    props = []
    for row in rows:
        parts = row.split("|")
        if len(parts) < 16:
            continue
        props.append({
            "id": parts[0], "name": parts[1], "price_micros": parts[2],
            "bedrooms": parts[3], "bathrooms": parts[4], "sqft": parts[5],
            "floor": parts[6], "flat": parts[7], "facing": parts[8],
            "furnishing": parts[9], "building_id": parts[10],
            "building_name": parts[11], "locality": parts[12],
            "lat": parts[13], "lng": parts[14], "inspection_id": parts[15],
        })
    return props


def main():
    print("=" * 70)
    print("JUM-621 Production Run: LIVE + APPROVED Inspections")
    print("=" * 70)

    props = get_eligible_properties()
    print(f"\nFound {len(props)} eligible properties (LIVE + APPROVED inspection)\n")

    total_uploaded = 0
    total_failed = 0
    results = []

    for idx, prop_data in enumerate(props):
        property_id = prop_data["id"]
        inspection_id = prop_data["inspection_id"]
        print(f"\n{'-'*70}")
        print(f"[{idx+1}/{len(props)}] {prop_data['name']}")
        print(f"  Building: {prop_data['building_name']}, {prop_data['locality']}")
        print(f"  Price: {int(prop_data['price_micros']) // 1000000} | "
              f"BHK: {prop_data['bedrooms']}B/{prop_data['bathrooms']}B/{prop_data['sqft']}sqft")

        # Get property and inspection data
        prop, inspection = get_property_and_inspection(property_id)
        if not prop:
            print("  SKIP: Property not found")
            results.append({"property": prop_data["name"], "status": "NOT_FOUND"})
            continue
        if not inspection:
            print("  SKIP: No inspection record found")
            results.append({"property": prop_data["name"], "status": "NO_INSPECTION"})
            continue

        # Collect all photos (no limit for production)
        all_photos = []
        for col, tag in PHOTO_TAG_MAP.items():
            photos = inspection.get(col, [])
            for photo in photos:
                file_id = photo.get("fileId", "")
                extension = photo.get("extension", ".jpg")
                if file_id:
                    all_photos.append({
                        "file_id": file_id,
                        "extension": extension,
                        "tag": tag,
                        "source_col": col,
                        "label": photo.get("label", ""),
                    })

        print(f"  Photos found: {len(all_photos)}")

        if not all_photos:
            print("  SKIP: No photos")
            results.append({"property": prop_data["name"], "status": "NO_PHOTOS"})
            continue

        # Upload to Cloudinary
        uploaded = []
        for i, photo in enumerate(all_photos):
            container_path = find_file_in_container(photo["file_id"], photo["extension"])
            if not container_path:
                print(f"  [{i+1}/{len(all_photos)}] SKIP: file not found on disk")
                total_failed += 1
                continue

            local_path = f"/tmp/j621_{photo['file_id']}{photo['extension']}"
            result = subprocess.run(
                ["docker", "cp", f"twenty-server-1:{container_path}", local_path],
                capture_output=True, timeout=30
            )
            if result.returncode != 0:
                print(f"  [{i+1}/{len(all_photos)}] SKIP: docker cp failed")
                total_failed += 1
                continue

            public_id = f"jumbo_listing/{property_id[:8]}/{photo['file_id']}"
            upload_result = upload_to_cloudinary(local_path, public_id)

            if upload_result:
                upload_result["tag"] = photo["tag"]
                uploaded.append(upload_result)
                print(f"  [{i+1}/{len(all_photos)}] OK: {photo['tag']} -> {upload_result['url'][:60]}...")
            else:
                print(f"  [{i+1}/{len(all_photos)}] FAIL: {photo['tag']}")
                total_failed += 1

            try:
                os.unlink(local_path)
            except OSError:
                pass
            time.sleep(0.3)

        # Save to _property.files
        if uploaded:
            success, assets = save_to_property_files(property_id, uploaded)
            if success:
                print(f"  Saved {len(assets)} Cloudinary URLs to _property.files ✓")
                total_uploaded += len(uploaded)
            else:
                print(f"  ERROR: Failed to save to _property.files")
                total_failed += len(uploaded)

        results.append({
            "property": prop_data["name"],
            "status": "OK",
            "uploaded": len(uploaded),
            "total": len(all_photos),
        })

    # Summary
    print(f"\n{'='*70}")
    print("PRODUCTION RUN COMPLETE")
    print(f"{'='*70}")
    for r in results:
        status = r["status"]
        if status == "OK":
            print(f"  ✓ {r['property']}: {r['uploaded']}/{r['total']} photos uploaded")
        else:
            print(f"  - {r['property']}: {status}")
    print(f"\n  Total uploaded: {total_uploaded}")
    print(f"  Total failures: {total_failed}")
    print(f"{'='*70}")

    return 0


def db_query(sql):
    """Execute a SQL query and return rows as list of strings."""
    result = subprocess.run(
        ["docker", "exec", "-i", "twenty-db-1", "psql", "-U", "twenty", "-d", "default", "-t", "-A", "-F", "|"],
        input=sql, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        print(f"  DB ERROR: {result.stderr[:200]}")
        return []
    rows = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            rows.append(line.strip())
    return rows

if __name__ == "__main__":
    sys.exit(main())
