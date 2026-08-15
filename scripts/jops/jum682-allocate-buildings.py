#!/usr/bin/env python3
import json, subprocess, sys
from pathlib import Path
WS = 'workspace_1l3urgumjmspnjxohclmfz6fx'
DB = 'twenty-db-1'
MANIFEST = Path('/opt/jops/jum682-territories.normalized.json')

def q(sql):
    r = subprocess.run(['docker','exec','-i',DB,'psql','-U','twenty','-d','default','-t','-A','-F','\t','-c',sql], capture_output=True, text=True, check=True)
    return [x.split('\t') for x in r.stdout.strip().splitlines() if x.strip()]

def pip(lat, lon, poly):
    inside = False
    j = len(poly) - 1
    for i, (x1, y1) in enumerate(poly):
        x2, y2 = poly[j]
        if ((y1 > lat) != (y2 > lat)) and lon < (x2-x1)*(lat-y1)/(y2-y1)+x1:
            inside = not inside
        j = i
    return inside

def main(check=False):
    m = json.load(open(MANIFEST))
    version = 'jum682-csv-' + m['sha256']
    ids = {n:z for n,z in q(f'''SELECT "name",id FROM {WS}."_zoneallocation" WHERE "deletedAt" IS NULL AND "createdByContext"->>'routingVersion'='{version}' ''')}
    if len(ids) != 11:
        raise SystemExit(f'expected 11 JUM682 zones, found {len(ids)}')
    rows = q(f'''SELECT id,latitude,longitude,COALESCE("zoneId"::text,'') FROM {WS}._building WHERE "deletedAt" IS NULL AND latitude IS NOT NULL AND longitude IS NOT NULL''')
    changes, counts, no_match = [], {}, []
    for bid, lat, lon, old in rows:
        hits = [r for r in m['territories'] if pip(float(lat), float(lon), r['coordinates_lon_lat'])]
        hits.sort(key=lambda r: (0 if r['zone_name'] == 'Bagalur' else 1, r['source_row']))
        if not hits:
            no_match.append(bid)
            continue
        zone = hits[0]['zone_name']
        # Business-approved canonical spelling: Kadugori (CSV says Kadugodi).
        zone_id_name = 'Kadugori' if zone == 'Kadugodi' else zone
        zid = ids[zone_id_name]
        counts[zone_id_name] = counts.get(zone_id_name, 0) + 1
        if old != zid:
            changes.append((bid, zid))
    print(json.dumps({'buildings': len(rows), 'changes': len(changes), 'no_match': len(no_match), 'by_zone': counts, 'check': check}, indent=2))
    if check:
        print('[CHECK] no writes')
        return
    for bid, zid in changes:
        q(f'''UPDATE {WS}._building SET "zoneId"='{zid}',"updatedAt"=NOW() WHERE id='{bid}' AND "deletedAt" IS NULL''')
    print('UPDATED', len(changes))

if __name__ == '__main__':
    main('--check' in sys.argv)
