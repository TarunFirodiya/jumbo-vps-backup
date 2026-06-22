#!/usr/bin/env python3
"""
Property Status Calculator for Jumbo Homes Twenty CRM

Computes property_status field based on business rules:
  Priority (highest first):
  1. SOLD               - if ANY related offer stage is in SOLD_STAGES
  2. ON_HOLD            - if property.onHold is True
  3. LIVE               - if property.jumboUrl is non-empty
  4. INSPECTION_PENDING - if NO related inspection report exists
  5. CATALOGUE_PENDING  - if related inspection exists but not APPROVED

Rate limit aware: batches operations to stay under 100 req/min.
Uses longer page sizes to minimize query count.

Usage:
  python3 property-status-calculator.py           # run once
  python3 property-status-calculator.py --dry-run  # preview only
  python3 property-status-calculator.py --verify   # spot-check a few
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

# --- Config ---
API_URL = "https://admin.jumbohomes.in/graphql"
API_KEY_PATH = "/root/.twenty/api_key.txt"
TOKEN_BUDGET = 90  # stay under 100/min to be safe
BATCH_SIZE = 100  # properties per page -- larger = fewer queries
MUTATION_DELAY_S = 1.1  # seconds between mutations (60/min max, target ~50/min)
PAGE_DELAY_S = 2.0  # delay between page fetches
POST_MUTATION_DELAY_S = 1.0  # delay after each mutation
ERROR_BACKOFF_S = 15  # wait this long after a rate limit error

SOLD_STAGES = {"TOKEN_PAID", "AFS_MOU_SIGNED", "SALE_DEED_REGISTERED"}

QUERY_ALL_PROPERTIES = """
query GetAllProperties($first: Int, $after: String) {
  properties(first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        id
        name
        propertyStatus
        onHold
        jumboUrl
        offers {
          edges {
            node {
              id
              stage
            }
          }
        }
        inspections {
          edges {
            node {
              id
              status
            }
          }
        }
      }
    }
  }
}
"""

MUTATION_UPDATE = """
mutation UpdatePropertyStatus($id: String!, $status: String!) {
  updateProperty(id: $id, data: { propertyStatus: $status }) {
    id
    propertyStatus
  }
}
"""


def get_api_key():
    with open(API_KEY_PATH) as f:
        return f.read().strip()


def gql(api_key, query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    auth = "Bearer " + api_key
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", API_URL,
         "-H", "Content-Type: application/json",
         "-H", "Authorization: " + auth,
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=60
    )
    return json.loads(result.stdout)


def is_rate_limit_error(result):
    if "errors" not in result:
        return False
    for err in result["errors"]:
        msg = err.get("message", "").lower()
        if "rate limit" in msg or "limit reached" in msg:
            return True
    return False


def fetch_all_properties(api_key):
    """Fetch all properties with pagination, respecting rate limits."""
    all_properties = []
    cursor = None
    page = 0

    while True:
        variables = {"first": BATCH_SIZE}
        if cursor:
            variables["after"] = cursor

        result = gql(api_key, QUERY_ALL_PROPERTIES, variables)

        if is_rate_limit_error(result):
            print(f"  Rate limited on page fetch, backing off {ERROR_BACKOFF_S}s...")
            time.sleep(ERROR_BACKOFF_S)
            continue

        if "errors" in result:
            print(f"ERROR fetching: {json.dumps(result['errors'])}", file=sys.stderr)
            break

        data = result.get("data", {}).get("properties", {})
        edges = data.get("edges", [])
        all_properties.extend(edges)
        page += 1

        page_info = data.get("pageInfo", {})
        has_next = page_info.get("hasNextPage", False)

        if not has_next:
            break

        cursor = page_info.get("endCursor")
        print(f"  Page {page}: {len(all_properties)} properties fetched...")
        time.sleep(PAGE_DELAY_S)

    return all_properties


def compute_status(prop):
    """Compute the correct status based on business rules."""
    offers = prop.get("offers", {}).get("edges", [])
    inspections = prop.get("inspections", {}).get("edges", [])
    on_hold = prop.get("onHold", False)
    jumbo_url = prop.get("jumboUrl")

    has_sold_offer = any(
        edge["node"].get("stage", "") in SOLD_STAGES
        for edge in offers
    )

    has_inspection = len(inspections) > 0
    has_approved_inspection = any(
        edge["node"].get("status", "") == "APPROVED"
        for edge in inspections
    )

    if has_sold_offer:
        return "SOLD"
    if on_hold:
        return "ON_HOLD"
    if jumbo_url:
        return "LIVE"
    if not has_inspection:
        return "INSPECTION_PENDING"
    if not has_approved_inspection:
        return "CATALOGUE_PENDING"
    return "LIVE"


def mutation_with_retry(api_key, prop_id, status, max_retries=3):
    """Execute mutation with rate limit retry."""
    for attempt in range(max_retries):
        result = gql(api_key, MUTATION_UPDATE, {"id": prop_id, "status": status})

        if is_rate_limit_error(result):
            wait = ERROR_BACKOFF_S * (attempt + 1)
            print(f"    RATE LIMITED, backing off {wait}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait)
            continue

        if "errors" in result:
            return False, result["errors"][0].get("message", "unknown")
        return True, None

    return False, "max retries exceeded"


def main():
    dry_run = "--dry-run" in sys.argv
    verify_mode = "--verify" in sys.argv

    api_key = get_api_key()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mode = "DRY RUN" if dry_run else ("VERIFY" if verify_mode else "LIVE")
    print(f"[{ts}] Property Status Calculator ({mode})")

    # --- Phase 1: Fetch all properties ---
    print("\nPhase 1: Fetching all properties...")
    properties = fetch_all_properties(api_key)
    print(f"Fetched {len(properties)} properties")

    # --- Phase 2: Compute what needs updating ---
    print("\nPhase 2: Computing status changes...")
    to_update = []
    skipped = 0

    for edge in properties:
        prop = edge["node"]
        current = prop.get("propertyStatus")
        new = compute_status(prop)

        if current == new:
            skipped += 1
        else:
            to_update.append({
                "id": prop["id"],
                "name": prop["name"],
                "old": current,
                "new": new,
            })

    print(f"  Need to update: {len(to_update)}")
    print(f"  Already correct: {skipped}")

    if verify_mode:
        # Show a sample of what would change
        print("\nSample changes (first 20):")
        for item in to_update[:20]:
            print(f"  {item['name']}: {item['old']} -> {item['new']}")
        return

    if dry_run:
        print("\nDry run results (first 20):")
        for item in to_update[:20]:
            print(f"  {item['name']}: {item['old']} -> {item['new']}")
        print(f"\nTotal: {len(to_update)} would be updated, {skipped} skipped")
        return

    # --- Phase 3: Apply updates with rate limiting ---
    print(f"\nPhase 3: Applying updates (1 mutation every {MUTATION_DELAY_S}s)...")
    updated = 0
    errors = 0
    error_details = []

    for i, item in enumerate(to_update):
        success, err_msg = mutation_with_retry(api_key, item["id"], item["new"])

        if success:
            updated += 1
            if (i + 1) % 10 == 0:
                print(f"  Progress: {i + 1}/{len(to_update)} updated")
        else:
            errors += 1
            error_details.append(f"{item['name']}: {err_msg}")

        # Rate limit pacing
        time.sleep(MUTATION_DELAY_S)

    # --- Summary ---
    print(f"\n{'='*60}")
    print(f"DONE. Updated: {updated}, Skipped: {skipped}, Errors: {errors}")

    if error_details:
        print(f"\nErrors ({len(error_details)}):")
        for detail in error_details[:20]:
            print(f"  {detail}")
        if len(error_details) > 20:
            print(f"  ... and {len(error_details) - 20} more")


if __name__ == "__main__":
    main()
