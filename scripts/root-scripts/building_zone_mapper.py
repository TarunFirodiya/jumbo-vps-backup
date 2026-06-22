#!/usr/bin/env python3
"""
Building-to-Zone Mapping Script for JUM-550
Runs ON the VPS. Maps buildings to zones using point-in-polygon against geo-fence data.
"""

import json
import sys
import subprocess

# Zone geo-fence data from DB (lat, lon pairs)
ZONE_DATA = {
    "North": {
        "id": "d1ca3b1a-cf2a-412e-bc7c-36473bb1b801",
        "coords": [
            [13.256738, 77.717199], [13.31162, 77.541504], [13.193488, 77.503052],
            [13.072288, 77.505798], [13.044864, 77.507858], [13.018244, 77.566309],
            [13.010168, 77.585754], [12.996168, 77.662109], [13.010168, 77.697144],
            [13.090623, 77.71471], [13.193488, 77.770538], [13.256738, 77.717199]
        ]
    },
    "Whitefield": {
        "id": "d2ca3b1a-cf2a-412e-bc7c-36473bb1b802",
        "coords": [
            [12.999916, 77.67643], [12.989629, 77.688446], [12.983022, 77.679005],
            [12.954584, 77.682567], [12.957052, 77.698746], [12.952541, 77.711277],
            [12.952541, 77.711449], [12.952541, 77.712135], [12.952541, 77.71265],
            [12.952541, 77.713852], [12.952541, 77.714195], [12.952541, 77.715225],
            [12.951537, 77.71562], [12.94685, 77.71562], [12.946347, 77.71562],
            [12.945845, 77.71562], [12.945175, 77.72531], [12.94484, 77.732349],
            [12.944338, 77.734237], [12.94417, 77.740589], [12.943165, 77.746082],
            [12.942328, 77.74429], [12.941993, 77.74695]
        ]
    },
    "KR Puram": {
        "id": "d3ca3b1a-cf2a-412e-bc7c-36473bb1b803",
        "coords": [
            [13.000606, 77.677674], [13.01129, 77.698917], [13.038718, 77.704668],
            [13.090623, 77.71471], [13.136893, 77.747144], [13.151744, 77.753325],
            [13.14234, 77.773581], [13.073402, 77.800017], [12.99723, 77.78182],
            [12.99723, 77.771177], [12.99723, 77.766371], [12.99723, 77.753496],
            [12.99723, 77.751264], [12.99723, 77.749032], [12.99723, 77.747144],
            [12.99723, 77.744569], [12.99723, 77.73796], [12.99723, 77.733669],
            [12.99723, 77.7281], [12.99423, 77.726555], [12.99123, 77.724838],
            [12.99023, 77.723122], [12.99666, 77.713852], [12.99711, 77.703209]
        ]
    },
    "Varthur": {
        "id": "d4ca3b1a-cf2a-412e-bc7c-36473bb1b804",
        "coords": [
            [12.956571, 77.745309], [12.954051, 77.753946], [12.955943, 77.764513],
            [12.946127, 77.764513], [12.93723, 77.764513], [12.915723, 77.75475],
            [12.903209, 77.741277], [12.915225, 77.74003], [12.915723, 77.733669],
            [12.92135, 77.732468], [12.92349, 77.732349], [12.924838, 77.731319],
            [12.92711, 77.72752], [12.92849, 77.713852], [12.92909, 77.70528],
            [12.93652, 77.703209], [12.94187, 77.703209], [12.94429, 77.713852],
            [12.94429, 77.724838], [12.94429, 77.733669], [12.94429, 77.73652],
            [12.94429, 77.73923], [12.94429, 77.740195], [12.94429, 77.743225]
        ]
    },
    "Sarjapura": {
        "id": "d5ca3b1a-cf2a-412e-bc7c-36473bb1b805",
        "coords": [
            [12.902206, 77.706417], [12.908867, 77.705795], [12.909819, 77.705773],
            [12.915723, 77.704229], [12.915723, 77.704513], [12.91144, 77.705138],
            [12.91281, 77.705138], [12.91365, 77.705138], [12.913852, 77.70528],
            [12.915225, 77.70562], [12.920195, 77.70562], [12.92135, 77.70562],
            [12.92349, 77.70562], [12.92849, 77.713852], [12.92849, 77.715225],
            [12.92849, 77.716371], [12.92849, 77.72032], [12.926371, 77.723122],
            [12.924229, 77.731319], [12.92127, 77.732349], [12.915723, 77.733669],
            [12.913852, 77.735225], [12.911277, 77.73652], [12.91032, 77.741277]
        ]
    },
    "Haralur": {
        "id": "d6ca3b1a-cf2a-412e-bc7c-36473bb1b806",
        "coords": [
            [12.917118, 77.623215], [12.889164, 77.640209], [12.877503, 77.647076],
            [12.871136, 77.653496], [12.86992, 77.653496], [12.871136, 77.653496],
            [12.87038, 77.655225], [12.87266, 77.662135], [12.873225, 77.664513],
            [12.873225, 77.67135], [12.873225, 77.673496], [12.873225, 77.675225],
            [12.873225, 77.67136], [12.873225, 77.673225], [12.873225, 77.674229],
            [12.873225, 77.676371], [12.873225, 77.67791], [12.873225, 77.678438],
            [12.873225, 77.67923], [12.873225, 77.681277], [12.86992, 77.682349],
            [12.86652, 77.684513], [12.862135, 77.691277], [12.86809, 77.697144]
        ]
    },
    "Bellandur": {
        "id": "d7ca3b1a-cf2a-412e-bc7c-36473bb1b807",
        "coords": [
            [12.9554, 77.689433], [12.954699, 77.689465], [12.953476, 77.689186],
            [12.951523, 77.682349], [12.951225, 77.682349], [12.94944, 77.682349],
            [12.94898, 77.682349], [12.94752, 77.68788], [12.941277, 77.68788],
            [12.94032, 77.682135], [12.94032, 77.673669], [12.94032, 77.67526],
            [12.94032, 77.67135], [12.94032, 77.670195], [12.94032, 77.664513],
            [12.94032, 77.662349], [12.94032, 77.661225], [12.94032, 77.660195],
            [12.924838, 77.660195], [12.92349, 77.660195], [12.92135, 77.660195],
            [12.920195, 77.660195], [12.920195, 77.661225], [12.920195, 77.664513]
        ]
    },
    "Electronic City": {
        "id": "d8ca3b1a-cf2a-412e-bc7c-36473bb1b808",
        "coords": [
            [12.916988, 77.622973], [12.916282, 77.620516], [12.912653, 77.621412],
            [12.911277, 77.62087], [12.903225, 77.620195], [12.901225, 77.620195],
            [12.90032, 77.613852], [12.900195, 77.611277], [12.900195, 77.61032],
            [12.89032, 77.61032], [12.890195, 77.610195], [12.890195, 77.610138],
            [12.873496, 77.603209], [12.864513, 77.601225], [12.853225, 77.591277],
            [12.801225, 77.62349], [12.771225, 77.653496], [12.741225, 77.682349],
            [12.74986, 77.741277], [12.753496, 77.753225], [12.755225, 77.764513],
            [12.764513, 77.771225], [12.771225, 77.773496]
        ]
    }
}


def point_in_polygon(lat, lon, polygon):
    """
    Ray casting algorithm. Polygon vertices are [lat, lon].
    lat = y-axis, lon = x-axis.
    """
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]    # lat, lon of vertex i
        yj, xj = polygon[j]    # lat, lon of vertex j
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def find_zone_for_building(lat, lon):
    """Returns (zone_name, zone_id) or (None, None)"""
    for zone_name, zone_info in ZONE_DATA.items():
        if point_in_polygon(lat, lon, zone_info["coords"]):
            return zone_name, zone_info["id"]
    return None, None


def run_sql(sql):
    """Run SQL directly on local docker DB"""
    result = subprocess.run(
        ['docker', 'exec', '-i', 'twenty-db-1', 'psql', '-U', 'twenty', '-d', 'default'],
        input=sql, capture_output=True, text=True, timeout=60
    )
    return result.stdout, result.stderr, result.returncode


def main():
    print("=== Building-to-Zone Mapping (JUM-550) ===\n")

    # Fetch buildings
    sql = """SET search_path TO workspace_1l3urgumjmspnjxohclmfz6fx, core, public;
SELECT id, name, latitude, longitude, locality
FROM "_building"
WHERE "deletedAt" IS NULL
  AND latitude IS NOT NULL
  AND longitude IS NOT NULL;"""

    stdout, stderr, rc = run_sql(sql)
    if rc != 0:
        print(f"ERROR fetching buildings: {stderr}")
        sys.exit(1)

    lines = stdout.strip().split('\n')
    buildings = []
    for line in lines:
        parts = [p.strip() for p in line.split('|')]
        if len(parts) >= 5 and parts[0] not in ('id', 'SET', '') and not parts[0].startswith('---'):
            try:
                buildings.append({
                    'id': parts[0], 'name': parts[1],
                    'lat': float(parts[2]), 'lon': float(parts[3]),
                    'locality': parts[4] if len(parts) > 4 else ''
                })
            except (ValueError, IndexError):
                continue

    print(f"Fetched {len(buildings)} buildings with coordinates\n")

    # Map buildings to zones
    mapped = 0
    unmapped = 0
    updates = []
    unmapped_list = []

    for b in buildings:
        zone_name, zone_id = find_zone_for_building(b['lat'], b['lon'])
        if zone_name:
            mapped += 1
            updates.append((b['id'], zone_id, zone_name, b['name'], b['locality']))
        else:
            unmapped += 1
            unmapped_list.append(b)

    # Zone distribution
    zone_counts = {}
    for _, zone_id, zone_name, _, _ in updates:
        zone_counts[zone_name] = zone_counts.get(zone_name, 0) + 1

    print(f"Results: {mapped} mapped, {unmapped} unmapped out of {len(buildings)} total\n")
    print("Zone distribution:")
    for zone, count in sorted(zone_counts.items(), key=lambda x: -x[1]):
        print(f"  {zone}: {count}")
    print()

    if unmapped > 0:
        print(f"Sample unmapped buildings (first 15):")
        for b in unmapped_list[:15]:
            print(f"  {b['name']} ({b['locality']}) — lat:{b['lat']}, lon:{b['lon']}")
        if len(unmapped_list) > 15:
            print(f"  ... and {len(unmapped_list) - 15} more")
        print()

    # Save dry-run results
    with open('/tmp/zone_mapping_dryrun.json', 'w') as f:
        json.dump({
            'total': len(buildings), 'mapped': mapped, 'unmapped': unmapped,
            'zone_distribution': zone_counts,
            'unmapped_buildings': [{'name': b['name'], 'locality': b['locality'], 'lat': b['lat'], 'lon': b['lon']} for b in unmapped_list]
        }, f, indent=2)
    print("Dry-run saved to /tmp/zone_mapping_dryrun.json")

    if mapped == 0:
        print("Nothing to map. Exiting.")
        sys.exit(0)

    # Apply updates
    print(f"\nApplying {mapped} updates to DB...")
    success = 0
    failed = 0
    for building_id, zone_id, zone_name, building_name, locality in updates:
        update_sql = f"""SET search_path TO workspace_1l3urgumjmspnjxohclmfz6fx, core, public;
UPDATE "_building" SET "zoneId" = '{zone_id}' WHERE id = '{building_id}' AND "deletedAt" IS NULL;"""
        stdout, stderr, rc = run_sql(update_sql)
        if rc == 0:
            success += 1
        else:
            failed += 1
            print(f"  FAILED: {building_name} -> {zone_name}: {stderr.strip()}")

    print(f"\nDB update complete: {success} updated, {failed} failed")

    # Verify
    verify_sql = """SET search_path TO workspace_1l3urgumjmspnjxohclmfz6fx, core, public;
SELECT COUNT(*) FROM "_building" WHERE "zoneId" IS NOT NULL AND "deletedAt" IS NULL;"""
    stdout, stderr, rc = run_sql(verify_sql)
    print(f"Verification: {stdout.strip()} buildings now have zoneId set")


if __name__ == '__main__':
    main()
