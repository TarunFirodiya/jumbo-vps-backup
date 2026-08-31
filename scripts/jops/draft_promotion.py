#!/usr/bin/env python3
"""
Draft Property Promotion Gate — Jumbo Homes CRM (JUM-700 follow-up)
====================================================================
Pipeline (agreed with Tarun, Aug 30 2026):
  new property created (webhook/Bablu ingest) -> DRAFT
  this gate, for each DRAFT property:
    1. Assign seller agent via chain: property.assignedAgentId <- buildingId
       -> _building.zoneId -> workspaceMember.assignedZoneId (fill-in only)
       Also mirrors onto the linked seller's assignedAgentId.
    2. Auto-assign next serial number (J-xyz): MAX(serialNumber) over ALL
       rows INCLUDING soft-deleted (the unique index covers deleted rows too
       — see serial-number-allocation-pitfall.md). Retry +1 on conflict.
    3. When both done -> propertyStatus DRAFT -> INSPECTION_PENDING.
       Never touches anything already past DRAFT.

Idempotent. DRY-RUN by default; pass --live to write.
Usage:
  python3 /opt/jops/draft_promotion.py             # dry run
  python3 /opt/jops/draft_promotion.py --live      # live
  python3 /opt/jops/draft_promotion.py --live --verbose
"""
import argparse
import subprocess
import sys

DB = ["docker", "exec", "-i", "twenty-db-1", "psql", "-U", "twenty",
      "-d", "default", "-t", "-A", "-F", "|"]
WS = "workspace_1l3urgumjmspnjxohclmfz6fx"


def sql(q, timeout=60):
    r = subprocess.run(DB + ["-c", q], capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("psql error: " + r.stderr.strip()[:500])
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually write")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    # --- 0. DRAFT properties
    drafts = sql(f"""
        SELECT p.id, p.name, p."buildingId", p."sellerId", p."serialNumber"
        FROM {WS}."_property" p
        WHERE p."deletedAt" IS NULL AND p."propertyStatus" = 'DRAFT';
    """)
    if not drafts:
        print("OK no draft properties pending. Nothing to do.")
        return

    # --- 1. zone -> agent map.
    # KNOWN SPLIT-BRAIN (Aug 30 2026): workspaceMembers are assigned to OLD
    # (soft-deleted) zone rows while buildings point at NEW active zone rows
    # of the same name. Resolve agent by ZONE NAME as primary path; fall back
    # to direct id match.
    zone_name_agent = {}
    for line in sql(f"""
        SELECT z.name, ws.id, z.id FROM {WS}."workspaceMember" ws
        JOIN {WS}."_zoneallocation" z ON ws."assignedZoneId" = z.id
        WHERE ws."assignedZoneId" IS NOT NULL;
    """).splitlines():
        if "|" in line:
            zname, mid, zid = line.split("|", 2)
            zone_name_agent[zname.strip().lower()] = mid
            zone_agents[zid] = mid
    zone_agents_by_name = zone_name_agent

    plan = []
    for line in drafts.splitlines():
        pid, name, building_id, seller_id, serial = line.split("|", 4)
        serial = serial if serial and serial != "" else None
        agent_id = None
        if building_id and building_id not in ("", "None"):
            zid = sql(f'SELECT "zoneId" FROM {WS}."_building" WHERE id=\'{building_id}\';')
            zname = sql(f'SELECT name FROM {WS}."_zoneallocation" WHERE id=\'{zid}\';') if zid else ""
            agent_id = zone_agents.get(zid) or zone_name_agent.get(
                (zname or "").strip().lower())
        plan.append({"id": pid, "name": name, "seller_id": seller_id
                     if seller_id not in ("", "None") else None,
                     "serial": serial, "agent_id": agent_id,
                     "building_id": building_id})

    if args.verbose:
        for p in plan:
            print(f"  draft {p['name']} serial={p['serial']} "
                  f"agent={'yes' if p['agent_id'] else 'MISSING(building/zone)'} "
                  f"seller={'yes' if p['seller_id'] else 'none'}")

    updated = promoted = skipped = 0
    for p in plan:
        if not p["agent_id"]:
            skipped += 1
            print(f"  SKIP {p['name']}: no building/zone/agent mapping "
                  f"(buildingId={p['building_id']}) — needs manual review")
            continue

        if not args.live:
            print(f"  DRY {p['name']}: would assign agent {p['agent_id'][:8]}…"
                  f"{', promote' if not p['serial'] else ''}")
            updated += 1
            promoted += 1
            continue

        # --- live: assign property agent (fill-in only)
        sql(f"""
            UPDATE {WS}."_property" SET "assignedAgentId" = '{p["agent_id"]}',
            "updatedAt" = NOW() WHERE id = '{p["id"]}'
            AND "assignedAgentId" IS NULL;
        """)
        # mirror to seller if linked
        if p["seller_id"]:
            sql(f"""
                UPDATE {WS}."_seller" SET "assignedAgentId" = '{p["agent_id"]}',
                "updatedAt" = NOW() WHERE id = '{p["seller_id"]}'
                AND "assignedAgentId" IS NULL;
            """)

        # --- live: serial if missing (max over ALL rows incl soft-deleted)
        if not p["serial"]:
            for _ in range(5):
                nxt = int(sql(
                    f'SELECT COALESCE(MAX("serialNumber"), 2500) + 1 '
                    f'FROM {WS}."_property";'))
                r = sql(f"""
                    UPDATE {WS}."_property"
                    SET "serialNumber" = {nxt}, "updatedAt" = NOW()
                    WHERE id = '{p["id"]}' AND "serialNumber" IS NULL
                    RETURNING id;
                """)
                if r:
                    print(f"  {p['name']}: serial J-{nxt}")
                    break
            else:
                print(f"  WARN {p['name']}: serial allocation failed 5x — "
                      f"leaving DRAFT")
                skipped += 1
                continue

        # --- live: promote
        sql(f"""
            UPDATE {WS}."_property"
            SET "propertyStatus" = 'INSPECTION_PENDING', "updatedAt" = NOW()
            WHERE id = '{p["id"]}' AND "propertyStatus" = 'DRAFT';
        """)
        promoted += 1
        updated += 1

    mode = "LIVE" if args.live else "DRY"
    print(f"[{mode}] drafts={len(plan)} updated={updated} "
          f"promoted={promoted} skipped={skipped}")


if __name__ == "__main__":
    main()
