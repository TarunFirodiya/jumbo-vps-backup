#!/usr/bin/env python3
import json, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

API_URL = "http://localhost:3000/graphql"

def get_api_key():
    with open("/root/.twenty/api_key.txt") as f:
        return f.read().strip()

def gql(query, variables=None, retries=3):
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    for attempt in range(retries):
        req = urllib.request.Request(API_URL, data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + get_api_key(), "Connection": "close"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 or "rate limit" in str(e.read().decode()).lower():
                print("  Rate limited, waiting 60s...")
                time.sleep(60)
                continue
            raise
        except (urllib.error.URLError, ConnectionResetError, BrokenPipeError, OSError) as e:
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"  Connection error ({e}), retrying in {wait}s... (attempt {attempt+1}/{retries})")
                time.sleep(wait)
                continue
            raise

def compute_stage(buyer, enquiries, visits):
    now = datetime.now(timezone.utc)
    buyer_created = datetime.fromisoformat(buyer["createdAt"].replace("Z", "+00:00"))
    days_since_signup = (now - buyer_created).total_seconds() / 86400
    sorted_enq = sorted(enquiries, key=lambda e: e["createdAt"], reverse=True)
    latest_enq = sorted_enq[0] if sorted_enq else None
    days_since_enquiry = 999999
    if latest_enq:
        days_since_enquiry = (now - datetime.fromisoformat(latest_enq["createdAt"].replace("Z", "+00:00"))).total_seconds() / 86400
    sorted_visits = sorted(visits, key=lambda v: v["createdAt"], reverse=True)
    has_visits = len(visits) > 0
    days_since_visit = 999999
    if sorted_visits:
        days_since_visit = (now - datetime.fromisoformat(sorted_visits[0]["createdAt"].replace("Z", "+00:00"))).total_seconds() / 86400
    if has_visits:
        if days_since_visit <= 30: return "ACTIVE_VISITOR"
        elif days_since_visit <= 90: return "AT_RISK_VISITOR"
        else: return "INACTIVE"
    if not latest_enq: return "INACTIVE"
    if days_since_enquiry <= 7: return "FRESH_LEAD"
    if 8 <= days_since_signup <= 30: return "AT_RISK_LEAD"
    return "INACTIVE"

def fetch_buyers_page(first=50, after=None):
    q = "query GetBuyers($first: Int!, $after: String) { buyers(first: $first, after: $after, filter: { deletedAt: { is: NULL } }) { edges { node { id leadStage qualified createdAt personId } } pageInfo { hasNextPage endCursor } } }"
    vars = {"first": first}
    if after: vars["after"] = after
    r = gql(q, vars)
    if "errors" in r:
        err_msg = str(r["errors"])
        if "rate limit" in err_msg.lower():
            print("  Rate limit hit, waiting 65s...")
            time.sleep(65)
            return fetch_buyers_page(first, after)
        print("Errors:", r["errors"])
        return None, None, False
    d = r["data"]["buyers"]
    return d["edges"], d["pageInfo"].get("endCursor"), d["pageInfo"].get("hasNextPage", False)

def fetch_enquiries(buyer_id):
    r = gql("query($id: UUID!) { enquiries(filter: { buyerId: { eq: $id }, deletedAt: { is: NULL } }) { edges { node { id statusDetail createdAt } } } }", {"id": buyer_id})
    if "errors" in r:
        if "rate limit" in str(r["errors"]).lower():
            time.sleep(65)
            return fetch_enquiries(buyer_id)
        return []
    return [e["node"] for e in r["data"]["enquiries"]["edges"]]

def fetch_visits(buyer_id):
    r = gql("query($id: UUID!) { visits(filter: { buyerProfileId: { eq: $id }, deletedAt: { is: NULL } }) { edges { node { id createdAt } } } }", {"id": buyer_id})
    if "errors" in r:
        if "rate limit" in str(r["errors"]).lower():
            time.sleep(65)
            return fetch_visits(buyer_id)
        return []
    return [v["node"] for v in r["data"]["visits"]["edges"]]

def update_stage(buyer_id, stage):
    r = gql("mutation($id: UUID!, $data: BuyerUpdateInput!) { updateBuyer(id: $id, data: $data) { id leadStage } }", {"id": buyer_id, "data": {"leadStage": stage}})
    if "errors" in r:
        if "rate limit" in str(r["errors"]).lower():
            time.sleep(65)
            return update_stage(buyer_id, stage)
        return False
    return True

def main():
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] Starting buyer stage calculation...")
    total, updated, errors, cursor, page = 0, 0, 0, None, 0
    while True:
        page += 1
        try:
            edges, next_cursor, has_next = fetch_buyers_page(50, cursor)
        except Exception as e:
            print(f"  Page {page} fetch failed after retries: {e}")
            break
        if edges is None:
            print(f"  Page {page}: GraphQL returned no edges, stopping")
            break
        print(f"  Page {page}: {len(edges)} buyers")
        for edge in edges:
            b = edge["node"]
            try:
                ns = compute_stage(b, fetch_enquiries(b["id"]), fetch_visits(b["id"]))
                if ns != b.get("leadStage"):
                    if update_stage(b["id"], ns): updated += 1
                    else: errors += 1
                total += 1
            except Exception as e:
                bid = b["id"][:8]
                print(f"  Error {bid}: {e}")
                errors += 1
            time.sleep(1.2)  # Rate limit: ~50 requests per 60s
        if not has_next or not next_cursor: break
        cursor = next_cursor
        print(f"  Cursor: {next_cursor[:20]}... (processed so far: {total})")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] Done! Processed: {total}, Updated: {updated}, Errors: {errors}")

if __name__ == "__main__":
    main()
