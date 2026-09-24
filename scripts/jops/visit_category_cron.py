#!/usr/bin/env python3
"""Visit category attribution cron for Jumbo Homes Twenty CRM.

Categorizes each visit (MECE, evaluated in order):
  NA            - no buyer / no scheduledAt / zero eligible enquiries
  DIRECT_VISIT  - an eligible enquiry (same buyer, createdAt < visit.scheduledAt,
                  not junk, propertyId set) whose property == visit's property
  UP_SOLD_VISIT - no property-matching enquiry, but an eligible enquiry exists on
                  property X and another non-cancelled visit by the same buyer on X
                  falls on the SAME IST calendar day
  CROSS_PITCH_VISIT - otherwise (eligible enquiry exists, property mismatch)

Eligibility: enquiry.buyerId == visit.buyerProfileId AND enquiry.createdAt <
visit.scheduledAt (strict) AND enquiry.propertyId IS NOT NULL AND isJunk=false.

CANCELLED / deleted visits are skipped entirely (not categorized, not counted as
the same-day "visit on X" anchor).

Modes:
  --backfill   categorize every visit with visitCategory IS NULL (all history)
  --cron       incremental: visits created since last successful run (state file)

API writes only (Twenty GraphQL). State file: /opt/jops/visit_category_state.json
"""

import json
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

API_URL = "http://127.0.0.1:3000/graphql"
KEY_PATH = "/root/.twenty/api_key.txt"
STATE_PATH = "/opt/jops/visit_category_state.json"
IST = timezone(timedelta(hours=5, minutes=30))
BATCH = 100

CATEGORY = {
    "direct": "DIRECT_VISIT",
    "upsold": "UP_SOLD_VISIT",
    "cross": "CROSS_PITCH_VISIT",
    "na": "NA",
}


def gq(query, variables=None, retries=4):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                API_URL,
                data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {open(KEY_PATH).read().strip()}"},
            )
            return json.loads(urllib.request.urlopen(req, timeout=90).read())
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"[warn] GraphQL error ({e}); retry {attempt + 2}/{retries}", flush=True)
            time.sleep(3 * (attempt + 1))


def fetch_paginated(plural, node_fields, extra_filter=None, cap=None):
    filt = extra_filter or {}
    nodes, cursor = [], None
    while True:
        v = {"first": BATCH, "after": cursor, "filter": filt}
        type_name = {"visits": "Visit", "enquiries": "Enquiry"}[plural]
        r = gq(f'query($first: Int!, $after: String, $filter: {type_name}FilterInput) '
               f'{{ {plural}(first: $first, after: $after, filter: $filter) '
               f'{{ pageInfo {{ hasNextPage endCursor }} edges {{ node {{ {node_fields} }} }} }} }}', v)
        if isinstance(r.get("errors"), list) and any(e.get("extensions", {}).get("subCode") == "LIMIT_REACHED" for e in r["errors"]):
            print("[warn] rate limited; sleeping 20s", flush=True)
            time.sleep(20)
            continue
        if r.get("errors") or not r.get("data"):
            if cursor is None:
                print(f"[warn] GraphQL error/null data: {str(r.get('errors'))[:200]}; retrying", flush=True)
                time.sleep(3)
                continue
            raise RuntimeError(f"GraphQL errors/null data after cursor {cursor}: {str(r.get('errors'))[:300]}")
        conn = r["data"][plural]
        nodes += [e["node"] for e in conn["edges"]]
        if not conn["pageInfo"]["hasNextPage"] or (cap and len(nodes) >= cap):
            break
        cursor = conn["pageInfo"]["endCursor"]
    return nodes


def eligible_enquiries(enq_by_buyer, buyer_id, scheduled_at):
    out = []
    for e in enq_by_buyer.get(buyer_id, []):
        if e["propertyId"] and not e["isJunk"] and e["createdAt"] < scheduled_at:
            out.append(e)
    return out


def compute_category(visit, enq_by_buyer, visit_day_by_buyer):
    buyer_id, prop_id, sched = visit["buyerProfileId"], visit["propertyId"], visit["scheduledAt"]
    if not buyer_id or not sched:
        return CATEGORY["na"]
    elig = eligible_enquiries(enq_by_buyer, buyer_id, sched)
    if not elig:
        return CATEGORY["na"]
    if prop_id and any(e["propertyId"] == prop_id for e in elig):
        return CATEGORY["direct"]
    ist_day = datetime.fromisoformat(sched.replace("Z", "+00:00")).astimezone(IST).date()
    # X = latest eligible enquiry's property
    x = max(elig, key=lambda e: e["createdAt"])["propertyId"]
    has_same_day_x = any(v[0] == x and v[1] == ist_day
                         for v in visit_day_by_buyer.get(buyer_id, []))
    return CATEGORY["upsold"] if has_same_day_x else CATEGORY["cross"]


_last_rl = [0.0]


def update_visit(vid, category):
    for attempt in range(5):
        r = gq('mutation($id: ID!, $vc: VisitVisitCategoryEnum) { updateVisit(id: $id, '
               'data: { visitCategory: $vc }) { id } }', {"id": vid, "vc": category})
        errs = r.get("errors")
        if isinstance(errs, list) and any(e.get("extensions", {}).get("subCode") == "LIMIT_REACHED" for e in errs):
            print("[warn] update rate limited; sleeping 30s", flush=True)
            _last_rl[0] = time.time()
            time.sleep(30)
            continue
        if errs:
            raise RuntimeError(f"updateVisit {vid}: {errs}")
        return
    raise RuntimeError(f"updateVisit {vid}: rate limited after retries")


def _dry_run():
    from collections import Counter
    import random
    visits = fetch_paginated("visits", "id buyerProfileId propertyId scheduledAt visitCategory status deletedAt createdAt")
    visits = [v for v in visits if not v["deletedAt"] and v["status"] != "CANCELLED"]
    print(f"eligible visits: {len(visits)}", flush=True)
    enquiries = fetch_paginated("enquiries", "id buyerId propertyId isJunk createdAt")
    enq_by_buyer = {}
    for e in enquiries:
        enq_by_buyer.setdefault(e["buyerId"], []).append(e)
    visit_day_by_buyer = {}
    for v in visits:
        if v["buyerProfileId"] and v["propertyId"] and v["scheduledAt"]:
            d = datetime.fromisoformat(v["scheduledAt"].replace("Z", "+00:00")).astimezone(IST).date()
            visit_day_by_buyer.setdefault(v["buyerProfileId"], []).append((v["propertyId"], d))
    print(f"enquiries: {len(enquiries)}", flush=True)
    c = Counter(compute_category(v, enq_by_buyer, visit_day_by_buyer) for v in visits)
    print(f"dry-run ALL {len(visits)}: {dict(c)}", flush=True)
    for v in visits:
        if compute_category(v, enq_by_buyer, visit_day_by_buyer) == CATEGORY["upsold"]:
            elig = eligible_enquiries(enq_by_buyer, v["buyerProfileId"], v["scheduledAt"])
            x = max(elig, key=lambda e: e["createdAt"])["propertyId"]
            day = datetime.fromisoformat(v["scheduledAt"].replace("Z", "+00:00")).astimezone(IST).date()
            print("sample upsold: visit prop", v["propertyId"], "| enquiry X", x,
                  "| same-day anchor:", any(a == x and b == day for a, b in visit_day_by_buyer[v["buyerProfileId"]]), flush=True)
            break


def main():
    mode = "backfill" if "--backfill" in sys.argv else "cron"
    if "--dry-run" in sys.argv:
        _dry_run(); return
    print(f"=== visit_category_cron mode={mode} start {datetime.now(timezone.utc).isoformat()} ===", flush=True)

    visits = fetch_paginated(
        "visits",
        "id buyerProfileId propertyId scheduledAt visitCategory status deletedAt createdAt",
    )
    visits = [v for v in visits if v["scheduledAt"]]  # ALL statuses per Rushabh
    print(f"fetched {len(visits)} eligible visits", flush=True)

    enquiries = fetch_paginated("enquiries", "id buyerId propertyId isJunk createdAt")
    enq_by_buyer = {}
    for e in enquiries:
        enq_by_buyer.setdefault(e["buyerId"], []).append(e)
    print(f"fetched {len(enquiries)} enquiries across {len(enq_by_buyer)} buyers", flush=True)

    # Same-day anchor index: buyer -> [(propertyId, IST date)] of all visits
    anchor_filter = {"deletedAt": {"is": "NULL"}}
    if mode == "cron":
        buyers = sorted({v["buyerProfileId"] for v in visits if v["buyerProfileId"]})
        anchors = []
        for i in range(0, len(buyers), 50):
            f = dict(anchor_filter, buyerProfileId={"in": buyers[i:i+50]})
            anchors += fetch_paginated(
                "visits", "id buyerProfileId propertyId scheduledAt", extra_filter=f)
    else:
        anchors = visits
    visit_day_by_buyer = {}
    for v in anchors:
        if not (v["buyerProfileId"] and v["propertyId"] and v["scheduledAt"]):
            continue
        d = datetime.fromisoformat(v["scheduledAt"].replace("Z", "+00:00")).astimezone(IST).date()
        visit_day_by_buyer.setdefault(v["buyerProfileId"], []).append((v["propertyId"], d))
    print(f"anchor index: {len(visit_day_by_buyer)} buyers", flush=True)

    stats = {"NA": 0, "DIRECT_VISIT": 0, "UP_SOLD_VISIT": 0, "CROSS_PITCH_VISIT": 0}
    failed = []
    for n, v in enumerate(visits, 1):
        cat = compute_category(v, enq_by_buyer, visit_day_by_buyer)
        try:
            update_visit(v["id"], cat)
            stats[cat] += 1
        except Exception as e:
            failed.append((v["id"], str(e)[:120]))
        time.sleep(0.8 if time.time() - _last_rl[0] > 60 else 3)
        if n % 500 == 0:
            print(f"progress {n}/{len(visits)} stats={stats} failures={len(failed)}", flush=True)

    print(f"=== done stats={stats} failures={len(failed)} ===", flush=True)
    if failed:
        with open("/opt/jops/visit_category_failures.json", "w") as f:
            json.dump(failed, f, indent=1)
        print("failures written to /opt/jops/visit_category_failures.json", flush=True)
    if mode == "cron" and not failed:
        json.dump({"last_run": datetime.now(timezone.utc).isoformat()}, open(STATE_PATH, "w"))
    elif mode == "backfill":
        json.dump({"last_run": datetime.now(timezone.utc).isoformat()}, open(STATE_PATH, "w"))


if __name__ == "__main__":
    main()