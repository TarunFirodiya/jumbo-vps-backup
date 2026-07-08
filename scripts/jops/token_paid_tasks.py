#!/usr/bin/env python3
"""
token_paid_tasks.py  -- Jumbo Homes

When an Opportunity (Offer) reaches the TOKEN_PAID stage, automatically create
the full set of TAT tasks for that offer, linked to the offer record and
assigned to the responsible workspace member.

- Triggered by a Hermes cron every 10 min (offset schedule 3,13,23,33,43,53).
- Idempotent:
    * Per-offer: a state file records which offer IDs already had their FULL
      task set generated. An offer is only marked done when ALL its tasks
      succeeded (no partial generations).
    * Per-task: before creating, we check whether a task with the exact
      "[Day N] <title>" already exists and is linked to this offer. If so,
      skip. This makes re-runs safe even after a partial failure.
- Only RESALE and ASSIGNMENT transaction types are covered (current scope).
- Due dates skip Saturday and Sunday ("working days"). Day 0 = Token Paid date.
- Rate-limit aware: Twenty allows ~100 GraphQL "tokens" per 60s. We budget
  ~70 tokens per run and stop early if we approach the ceiling, leaving the
  remaining offers for the next run (they are NOT marked processed).

Reads task definitions from tat_tasks.json (editable without code changes).
Silent on success. Prints to stdout on failure (Hermes no_agent cron alerts).
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

GRAPHQL_URL = "http://localhost:3000/graphql"
API_KEY_PATH = "/root/.twenty/api_key.txt"
STATE_FILE = "/opt/jops/token_paid_tasks.state.json"
TAT_FILE = "/opt/jops/tat_tasks.json"

# Only offers created within this many days are eligible (new offers guard).
NEW_OFFER_WINDOW_DAYS = 30

# Token budget per run. Twenty caps at 100 tokens / 60s. Each createTask +
# createTaskTarget is ~2 tokens. We leave headroom for other cron jobs.
TOKEN_BUDGET = 70
TOKEN_WINDOW_SECONDS = 60


def load_api_key():
    with open(API_KEY_PATH) as f:
        return f.read().strip()


# Token metering
_token_count = 0
_token_window_start = time.time()


def _spend(tokens=1):
    global _token_count, _token_window_start
    now = time.time()
    if now - _token_window_start > TOKEN_WINDOW_SECONDS:
        _token_window_start = now
        _token_count = 0
    if _token_count + tokens > TOKEN_BUDGET:
        return False  # budget exhausted for this window
    _token_count += tokens
    return True


def gql(query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=data,
        headers={
            "Authorization": "Bearer " + API_KEY,
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=30)
    result = json.loads(resp.read())
    if "errors" in result:
        raise Exception("GraphQL error: " + str(result["errors"]))
    return result["data"]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed": {}}  # offerId -> {generatedAt, transactionType, tasksCreated}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def add_working_days(start_date, n):
    """start_date = Day 0 (date). Return start_date + n working days (skip Sat/Sun)."""
    d = start_date
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 0=Mon ... 4=Fri
            added += 1
    return d


def iso_due(day0_date, n):
    due = add_working_days(day0_date, n)
    dt = datetime(due.year, due.month, due.day, 18, 0, 0, tzinfo=IST)
    return dt.isoformat()


def offer_already_has_task(offer_id, title):
    """Check if a task with this exact title is already linked to the offer."""
    q = """
    query Check($oid: ID!, $title: String!) {
      opportunities(filter: { id: { eq: $oid } }) {
        edges {
          node {
            taskTargets(filter: { task: { title: { eq: $title } } }) {
              totalCount
            }
          }
        }
      }
    }
    """
    try:
        d = gql(q, {"oid": offer_id, "title": title})
        edges = d["opportunities"]["edges"]
        if not edges:
            return False
        return edges[0]["node"]["taskTargets"]["totalCount"] > 0
    except Exception:
        return False


def main():
    global API_KEY
    API_KEY = load_api_key()

    with open(TAT_FILE) as f:
        tat = json.load(f)
    poc_map = tat["poc_map"]

    state = load_state()

    test_offer = None
    if "--test-offer" in sys.argv:
        idx = sys.argv.index("--test-offer")
        if idx + 1 < len(sys.argv):
            test_offer = sys.argv[idx + 1]

    # 1. Find offers at TOKEN_PAID, created recently, not yet fully processed.
    find_q = """
    query FindTokenPaid($windowStart: DateTime!) {
      opportunities(first: 100,
        filter: { stage: { eq: "TOKEN_PAID" },
                  createdAt: { gte: $windowStart } }) {
        edges {
          node { id name transactionType createdAt updatedAt }
        }
      }
    }
    """
    window_start = (datetime.now(timezone.utc) - timedelta(days=NEW_OFFER_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = gql(find_q, {"windowStart": window_start})
    edges = data["opportunities"]["edges"]

    pending = []
    for e in edges:
        node = e["node"]
        oid = node["id"]
        if oid in state["processed"]:
            continue
        if test_offer and oid != test_offer:
            continue
        pending.append(node)

    if not pending:
        return  # silent

    errors = []
    generated_total = 0
    budget_exhausted = False

    for offer in pending:
        if budget_exhausted:
            break
        oid = offer["id"]
        txn = offer.get("transactionType")
        if txn not in ("RESALE", "ASSIGNMENT"):
            state["processed"][oid] = {
                "generatedAt": datetime.now(IST).isoformat(),
                "transactionType": txn,
                "skipped": "out_of_scope",
            }
            continue

        tasks = tat.get(txn, [])
        if not tasks:
            continue

        day0 = datetime.now(IST).date()
        created_here = 0
        failed_here = 0

        for t in tasks:
            if budget_exhausted:
                break
            title = f"[Day {t['day']}] {t['title']}"
            poc = t["poc"]
            assignee = poc_map.get(poc)
            if not assignee:
                errors.append(f"[{oid}] Unknown POC '{poc}' for task '{title}'")
                failed_here += 1
                continue

            # Idempotent: skip if already linked to this offer.
            try:
                if offer_already_has_task(oid, title):
                    created_here += 1
                    continue
            except Exception:
                pass

            # Budget check before spending tokens.
            if not _spend(2):
                budget_exhausted = True
                break

            due_iso = iso_due(day0, t["day"])

            # createTask
            try:
                ct = gql(
                    """
                    mutation CreateTask($data: TaskCreateInput!) {
                      createTask(data: $data) { id }
                    }
                    """,
                    {
                        "data": {
                            "title": title,
                            "dueAt": due_iso,
                            "status": "TODO",
                            "assigneeId": assignee,
                        }
                    },
                )
                task_id = ct["createTask"]["id"]
            except Exception as ex:
                errors.append(f"[{oid}] createTask failed for '{title}': {ex}")
                failed_here += 1
                continue

            # link to offer via createTaskTarget
            try:
                gql(
                    """
                    mutation LinkTask($data: TaskTargetCreateInput!) {
                      createTaskTarget(data: $data) { id }
                    }
                    """,
                    {"data": {"taskId": task_id, "targetOpportunityId": oid}},
                )
                created_here += 1
            except Exception as ex:
                errors.append(f"[{oid}] linkTaskTarget failed for task {task_id}: {ex}")
                failed_here += 1

        # Only mark processed if EVERY task for this offer succeeded (full set).
        if failed_here == 0 and created_here == len(tasks):
            state["processed"][oid] = {
                "generatedAt": datetime.now(IST).isoformat(),
                "transactionType": txn,
                "tasksCreated": created_here,
            }
        elif budget_exhausted:
            # Leave unprocessed; next run (10 min later) will continue.
            pass
        else:
            # Some tasks failed (e.g. transient). Leave unprocessed so it retries.
            errors.append(f"[{oid}] partial generation ({created_here}/{len(tasks)}); will retry next run.")

        generated_total += created_here

    save_state(state)

    if errors:
        print("ERRORS:")
        for er in errors:
            print("  " + er)
        print(f"Tasks created this run: {generated_total}")
        sys.exit(1)


if __name__ == "__main__":
    main()
