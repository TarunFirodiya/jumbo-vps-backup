#!/usr/bin/env python3
"""Reconcile assignments after a ZoneAgent change.

Scope:
- Properties: all building-linked properties in a zone whose active ZoneAgent
  allocation changed on/after ROUTING_START.
- Enquiries/visits: only records created at or after that zone allocation's
  changedAt. Older historical event ownership is never modified.

All CRM mutations use Twenty GraphQL so the CRM timeline is retained.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

WS = 'workspace_1l3urgumjmspnjxohclmfz6fx'
ROUTING_START = '2026-09-01 00:00:00+00'
API = 'http://localhost:3000/graphql'


def sql(query):
    r = subprocess.run(
        ['docker', 'exec', 'twenty-db-1', 'psql', '-U', 'twenty', '-d', 'default', '-t', '-A', '-F', '\t', '-c', query],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return [line.split('\t') for line in r.stdout.splitlines() if line.strip()]


def gql(key, mutation, variables):
    request = urllib.request.Request(
        API,
        data=json.dumps({'query': mutation, 'variables': variables}).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
    )
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
            if result.get('errors'):
                raise RuntimeError(json.dumps(result['errors'])[:1000])
            if not result.get('data'):
                raise RuntimeError('GraphQL response has no data')
            return result['data']
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
            last = exc
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise last


def candidates(kind):
    active = f'''SELECT za."zoneId", za."agentId", za."updatedAt"
      FROM {WS}."_zoneAgent" za
      JOIN {WS}."_zoneallocation" z ON z.id=za."zoneId" AND z."deletedAt" IS NULL
      WHERE za."deletedAt" IS NULL AND za.isactive IS TRUE AND za."agentId" IS NOT NULL
        AND za."updatedAt">='{ROUTING_START}' '''
    if kind == 'property':
        query = f'''WITH active AS ({active})
        SELECT p.id, a."agentId", p."assignedAgentId", z.name
        FROM {WS}."_property" p
        JOIN {WS}."_building" b ON b.id=p."buildingId" AND b."deletedAt" IS NULL
        JOIN active a ON a."zoneId"=b."zoneId"
        JOIN {WS}."_zoneallocation" z ON z.id=b."zoneId"
        WHERE p."deletedAt" IS NULL AND p."assignedAgentId" IS DISTINCT FROM a."agentId"
        ORDER BY z.name, p.id;'''
    elif kind == 'enquiry':
        query = f'''WITH active AS ({active})
        SELECT e.id, a."agentId", e."assignedAgentId", z.name
        FROM {WS}."_enquiry" e
        LEFT JOIN {WS}."_building" b1 ON b1.id=e."buildingId" AND b1."deletedAt" IS NULL
        LEFT JOIN {WS}."_property" p ON p.id=e."propertyId" AND p."deletedAt" IS NULL
        LEFT JOIN {WS}."_building" b2 ON b2.id=p."buildingId" AND b2."deletedAt" IS NULL
        JOIN active a ON a."zoneId"=COALESCE(b1."zoneId", b2."zoneId") AND e."createdAt">=a."updatedAt"
        JOIN {WS}."_zoneallocation" z ON z.id=a."zoneId"
        WHERE e."deletedAt" IS NULL AND e."assignedAgentId" IS DISTINCT FROM a."agentId"
        ORDER BY z.name, e.id;'''
    else:
        query = f'''WITH active AS ({active})
        SELECT v.id, a."agentId", v."visitAgentId", z.name
        FROM {WS}."_visit" v
        JOIN {WS}."_property" p ON p.id=v."propertyId" AND p."deletedAt" IS NULL
        JOIN {WS}."_building" b ON b.id=p."buildingId" AND b."deletedAt" IS NULL
        JOIN active a ON a."zoneId"=b."zoneId" AND v."createdAt">=a."updatedAt"
        JOIN {WS}."_zoneallocation" z ON z.id=a."zoneId"
        WHERE v."deletedAt" IS NULL AND v."visitAgentId" IS DISTINCT FROM a."agentId"
        ORDER BY z.name, v.id;'''
    return candidates_from_rows(sql(query))


def candidates_from_rows(rows):
    return [{'id': r[0], 'target': r[1], 'current': r[2] or None, 'zone': r[3]} for r in rows]


def mutation_for(kind):
    field = {'property': 'assignedAgentId', 'enquiry': 'assignedAgentId', 'visit': 'visitAgentId'}[kind]
    singular = kind.capitalize()
    return f'''mutation Update($id: UUID!, $data: {singular}UpdateInput!) {{
      update{singular}(id: $id, data: $data) {{ id {field} }}
    }}''', field, 'update' + singular


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--object', choices=['property', 'enquiry', 'visit', 'all'], default='all')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    kinds = ['property', 'enquiry', 'visit'] if args.object == 'all' else [args.object]
    plans = {kind: candidates(kind) for kind in kinds}
    for kind in kinds:
        zones = {}
        for row in plans[kind]:
            zones[row['zone']] = zones.get(row['zone'], 0) + 1
        print(f'{kind}: {len(plans[kind])} candidates; by zone={zones}')
    if not args.live:
        print('[CHECK] no writes')
        return
    key = open('/root/.twenty/api_key.txt').read().strip()
    for kind in kinds:
        mutation, field, response_key = mutation_for(kind)
        total = len(plans[kind])
        for n, row in enumerate(plans[kind], 1):
            data = gql(key, mutation, {'id': row['id'], 'data': {field: row['target']}})
            node = data.get(response_key) or {}
            if node.get(field) != row['target']:
                raise RuntimeError(f'{kind} {row["id"]}: readback in mutation did not match target')
            if n % 25 == 0 or n == total:
                print(f'{kind}: {n}/{total} updated', flush=True)
            time.sleep(0.65)
    remaining = {kind: len(candidates(kind)) for kind in kinds}
    print('POST_VERIFY remaining=' + json.dumps(remaining, sort_keys=True))
    if any(remaining.values()):
        raise RuntimeError('post-write reconciliation remains')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        raise
