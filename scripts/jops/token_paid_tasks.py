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
- Rate-limit safe: we pace ~1.5s between tasks to stay under Twenty's GraphQL
  limit, and use strict null-data checks so phantom IDs never slip through.

Reads task definitions from tat_tasks.json (editable without code changes).
Silent on success. Prints to stdout on failure (Hermes no_agent cron alerts).
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

LOG_PATH = "/opt/jops/token_paid_tasks.log"


def log(msg):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass

IST = timezone(timedelta(hours=5, minutes=30))

GRAPHQL_URL = "http://localhost:3000/graphql"
API_KEY_PATH = "/root/.twenty/api_key.txt"
STATE_FILE = "/opt/jops/token_paid_tasks.state.json"
TAT_FILE = "/opt/jops/tat_tasks.json"

# Only offers created within this many days are eligible (new offers guard).
NEW_OFFER_WINDOW_DAYS = 30

# Pace between tasks to stay under Twenty's ~100 GraphQL tokens / 60s limit.
SLEEP_BETWEEN_TASKS_S = 1.5


def load_api_key():
    with open(API_KEY_PATH) as f:
        return f.read().strip()


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
    if result.get("data") is None:
        raise Exception("GraphQL returned null data (likely rate limited / throttled)")
    return result["data"]


def gql_strict(query, variables=None, node_path=None):
    """gql + assert the expected node is non-null (catches silent nulls)."""
    data = gql(query, variables)
    if node_path:
        cur = data
        for k in node_path:
            cur = (cur or {}).get(k)
        if cur is None or (isinstance(cur, dict) and cur.get("id") is None):
            raise Exception(f"Expected non-null node at {node_path}, got: {data}")
    return data


def verify_task_exists(task_id):
    """Read the task back by ID. Returns True only if it really persisted.

    NOTE: the singular `task(id:)` query is NOT allowed in this Twenty build
    (returns "Argument not allowed: id"). Use the `tasks(filter:{id:{eq}})` form.
    """
    q = "query($id:ID!){tasks(filter:{id:{eq:$id}}){totalCount}}"
    try:
        d = gql(q, {"id": task_id})
        return (d.get("tasks") or {}).get("totalCount", 0) > 0
    except Exception:
        return False


def create_task_with_retry(title, due_iso, assignee, oid, max_retries=3):
    """Create a task + link to offer, verifying persistence. Returns task_id or None."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            ct = gql_strict(
                "mutation CreateTask($data: TaskCreateInput!) { createTask(data: $data) { id } }",
                {"data": {"title": title, "dueAt": due_iso, "status": "TODO", "assigneeId": assignee}},
                node_path=["createTask", "id"],
            )
            task_id = ct["createTask"]["id"]
            # Link via TaskTarget (create empty node, then populate with bare scalar IDs).
            tt = gql_strict(
                "mutation CreateTarget($data: TaskTargetCreateInput!) { createTaskTarget(data: $data) { id } }",
                {"data": {}},
                node_path=["createTaskTarget", "id"],
            )
            target_id = tt["createTaskTarget"]["id"]
            gql_strict(
                "mutation UpdateTarget($id: ID!, $data: TaskTargetUpdateInput!) { updateTaskTarget(id: $id, data: $data) { id } }",
                {"id": target_id, "data": {"taskId": task_id, "targetOpportunityId": oid}},
                node_path=["updateTaskTarget", "id"],
            )
            # Verify the task actually persisted (API can return an ID for a
            # task that silently fails to commit under load).
            if verify_task_exists(task_id):
                return task_id
            last_err = "task not found after create (phantom id)"
        except Exception as ex:
            last_err = str(ex)
        time.sleep(3 * attempt)  # backoff before retry
    log(f"[{oid}] FAILED to create task '{title}' after {max_retries} attempts: {last_err}")
    return None


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


def offer_linked_titles(offer_id):
    """Return the set of task TITLES actually linked to this specific offer.

    Uses the offer's taskTargets -> task -> title. Offer-scoped (not global),
    so identical titles on different offers don't collide.
    """
    q = """
    query($oid:ID!){
      opportunities(filter:{id:{eq:$oid}}){
        edges{node{taskTargets{edges{node{task{title}}}}}}
      }
    }
    """
    try:
        d = gql(q, {"oid": offer_id})
        titles = set()
        for e in d["opportunities"]["edges"][0]["node"]["taskTargets"]["edges"]:
            tk = e["node"].get("task")
            if tk and tk.get("title"):
                titles.add(tk["title"])
        return titles
    except Exception:
        return set()


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
    log(f"Found {len(edges)} token-paid offers in window")

    pending = []
    for e in edges:
        node = e["node"]
        oid = node["id"]
        if oid in state["processed"]:
            continue
        if test_offer and oid != test_offer:
            continue
        pending.append(node)

    log(f"pending={len(pending)} offers to process")

    errors = []
    generated_total = 0

    for offer in pending:
        oid = offer["id"]
        txn = offer.get("transactionType")
        if txn not in ("RESALE", "ASSIGNMENT"):
            # No transaction type yet. Skip WITHOUT marking processed, so the
            # offer is retried automatically once the type is set in CRM.
            # (Do NOT write out_of_scope to state — that would permanently block it.)
            log(f"[{oid}] no transactionType set; skipping (will retry when typed)")
            continue

        tasks = tat.get(txn, [])
        log(f"[{oid}] txn={txn} tasks={len(tasks)}")
        if not tasks:
            continue

        day0 = datetime.now(IST).date()
        failed_here = 0

        # Offer-scoped set of titles already linked (for dedup + reconcile).
        linked = offer_linked_titles(oid)
        log(f"[{oid}] entering task loop, {len(tasks)} tasks, {len(linked)} already linked")

        for t in tasks:
            title = t["title"]
            poc = t["poc"]
            assignee = poc_map.get(poc)
            if not assignee:
                errors.append(f"[{oid}] Unknown POC '{poc}' for task '{title}'")
                failed_here += 1
                continue

            # Idempotent: skip if already linked to THIS offer.
            if title in linked:
                log(f"[{oid}] dedup skip (already linked): {title}")
                continue

            due_iso = iso_due(day0, t["day"])
            task_id = create_task_with_retry(title, due_iso, assignee, oid)
            if not task_id:
                errors.append(f"[{oid}] createTask failed for '{title}'")
                failed_here += 1
                continue
            linked.add(title)  # track locally so later iterations don't dup
            log(f"[{oid}] created+linked task day={t['day']} id={task_id}")

            # Pace requests to stay under Twenty's ~100 GraphQL tokens / 60s limit.
            time.sleep(SLEEP_BETWEEN_TASKS_S)

        # Reconciliation pass: Twenty occasionally drops a persisted task after
        # the create response returns success. Re-check the offer's ACTUAL linked
        # tasks and re-create any still missing (up to 3 passes).
        for attempt in range(1, 4):
            current = offer_linked_titles(oid)
            missing = [t for t in tasks if t["title"] not in current]
            if not missing:
                break
            log(f"[{oid}] reconcile pass {attempt}: {len(missing)} missing, recreating")
            for t in missing:
                assignee = poc_map.get(t["poc"])
                if not assignee:
                    continue
                due_iso = iso_due(day0, t["day"])
                tid = create_task_with_retry(t["title"], due_iso, assignee, oid)
                if not tid:
                    failed_here += 1
                time.sleep(SLEEP_BETWEEN_TASKS_S)

        # Final actual count in CRM.
        try:
            d = gql(
                """query($oid:ID!){
                  opportunities(filter:{id:{eq:$oid}}){
                    edges{node{taskTargets{totalCount}}}
                  }
                }""",
                {"oid": oid},
            )
            actual = d["opportunities"]["edges"][0]["node"]["taskTargets"]["totalCount"]
        except Exception:
            actual = len(offer_linked_titles(oid))
        log(f"[{oid}] FINAL actual linked tasks in CRM = {actual} (expected {len(tasks)})")

        # Only mark processed if EVERY task for this offer is linked (full set).
        if failed_here == 0 and actual == len(tasks):
            state["processed"][oid] = {
                "generatedAt": datetime.now(IST).isoformat(),
                "transactionType": txn,
                "tasksCreated": actual,
            }
        else:
            errors.append(f"[{oid}] partial generation (actual {actual}/{len(tasks)}); will retry next run.")

        generated_total += actual

    save_state(state)

    if errors:
        print("ERRORS:")
        for er in errors:
            print("  " + er)
        print(f"Tasks created this run: {generated_total}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("FATAL: " + str(e))
        traceback.print_exc()
        sys.exit(2)
