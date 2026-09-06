#!/usr/bin/env python3
"""
Seller Lead -> Zone Agent Recurring Assignment -- Jumbo Homes CRM
=================================================================
Idempotent: only fills sellers with NULL assignedAgentId.
Run on cron every 10-15 minutes to catch newly created seller leads.

Schema trail: seller <-- property.buildingId -> _building.zoneId
  -> _zoneAgent.zoneId -> _zoneAgent.sellerAgentId

Usage:
  python3 /opt/jops/seller-agent-allocator.py             # dry run
  python3 /opt/jops/seller-agent-allocator.py --live      # live
"""
import argparse
import subprocess
import sys

DB = ["docker", "exec", "-i", "twenty-db-1", "psql", "-U", "twenty",
      "-d", "default", "-t", "-A", "-F", "|||"]
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

    # --- Build zone -> seller agent map
    za_q = """SELECT za."zoneId"::text, z.name, za."sellerAgentId"::text
        FROM {ws}."_zoneAgent" za
        JOIN {ws}."_zoneallocation" z ON za."zoneId" = z.id
        WHERE za."deletedAt" IS NULL AND za."isactive" = true
          AND za."sellerAgentId" IS NOT NULL;""".format(ws=WS)

    za_map = {}       # zoneId -> sellerAgentId
    za_name_map = {}  # zone name (lower) -> sellerAgentId
    for line in sql(za_q).splitlines():
        if "|||" in line:
            zid, zname, said = line.split("|||", 2)
            za_map[zid] = said
            za_name_map[zname.strip().lower()] = said

    if not za_map:
        print("ERROR: no active zone agents found -- check _zoneAgent data")
        sys.exit(1)

    # --- Find sellers needing agent (all time, not yet assigned)
    sellers_q = """SELECT s.id::text, s.name,
               p.id::text as property_id, p.name as property_name,
               b.id::text as building_id, b.name as building_name,
               b."zoneId"::text
        FROM {ws}."_seller" s
        JOIN {ws}."_property" p ON p."sellerId" = s.id AND p."deletedAt" IS NULL
        JOIN {ws}."_building" b ON b.id = p."buildingId" AND b."deletedAt" IS NULL
        WHERE s."deletedAt" IS NULL
          AND s."assignedAgentId" IS NULL
          AND (s."onboardingStatus" IS NULL OR s."onboardingStatus"::text <> 'DROPPED')
          AND p."buildingId" IS NOT NULL
        ORDER BY s."createdAt";""".format(ws=WS)

    candidates = sql(sellers_q)

    if not candidates:
        if args.verbose:
            print("OK -- no seller leads pending agent assignment.")
        return

    plan = []
    for line in candidates.splitlines():
        if "|||" not in line:
            continue
        parts = line.split("|||")
        sid, sname, pid, pname, bid, bname, zid = (parts + [""]*7)[:7]
        zid = zid.strip() if zid and zid != "" else ""

        agent_id = za_map.get(zid) if zid else None
        if not agent_id and zid:
            zq = "SELECT name FROM {ws}.\"_zoneallocation\" WHERE id = '{z}';".format(ws=WS, z=zid)
            zname = sql(zq)
            if zname:
                agent_id = za_name_map.get(zname.strip().lower())

        if agent_id:
            plan.append({"seller_id": sid, "agent_id": agent_id})

    if not plan:
        if args.verbose:
            print("OK -- no assignable sellers.")
        return

    # --- Live write
    if args.live:
        updated = 0
        for p in plan:
            upd_q = ("UPDATE {ws}.\"_seller\" SET \"assignedAgentId\" = '{aid}', "
                     "\"updatedAt\" = NOW() WHERE id = '{sid}';").format(
                ws=WS, aid=p["agent_id"], sid=p["seller_id"])
            sql(upd_q)
            updated += 1
        print("[LIVE] Updated {} seller leads.".format(updated))
    else:
        print("[DRY-RUN] Would update {} sellers. Pass --live to write.".format(len(plan)))


if __name__ == "__main__":
    main()
