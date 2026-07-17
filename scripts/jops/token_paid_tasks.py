#!/usr/bin/env python3
"""
token_paid_tasks.py  -- Jumbo Homes  (REWRITE 2026-07-16)

When an Offer (opportunity) reaches the TOKEN_PAID stage with a transactionType
of RESALE or ASSIGNMENT, automatically create the full standardized TAT task set
from the deal-process sheet (RESALE=25 tasks, ASSIGNMENT=21 tasks), linked to the
offer and assigned to the responsible POC.

Corrected vs the old version (which used stale 21/18 combo templates, anchored
due dates on today, and only touched offers <30 days old):
  - Templates match the live sheet ("resale process TAT" / "Assignment Process TAT").
  - Due dates anchored on offer.bookingDate (Tarun rule 2026-07-10).
    Saturday IS a working day; only Sunday skipped. Due time 18:00 IST.
  - Acts on ANY RESALE/ASSIGNMENT offer at TOKEN_PAID (with bookingDate set)
    lacking the full task set -- not just <30-day-old ones.
  - Add-only + idempotent: never deletes; skips offers already fully generated
    (tracked in STATE_FILE). Safe to re-run.

Triggered by Hermes cron `token-paid-task-generator` via token_paid_tasks.sh.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

LOG_PATH = "/opt/jops/token_paid_tasks.log"
API_KEY_PATH = "/root/.twenty/api_key.txt"
STATE_FILE = "/opt/jops/token_paid_tasks.state.json"

SLEEP_BETWEEN_TASKS_S = 1.5
IST = timezone(timedelta(hours=5, minutes=30))

API_KEY = open(API_KEY_PATH).read().strip()
GRAPHQL_URL = "http://localhost:3000/graphql"

POC = {
    'Harish':     'c744aa41-ef42-4d2c-81af-bda0d71aeeca',
    'Puja':       '5cd4520c-52d8-4c98-9cc0-232ae767192b',
    'Ramswaroop': '8e1fdcfe-8db6-4e50-a7ab-d820f9b95c96',
    'Rohith':     'e897ff71-fcc8-4a10-ba07-9c02080a6a80',
}

# (day, title, poc) -- from the deal-process sheet TAT tabs.
RESALE = [
    (1,  'Token Payment +Whatsapp Grp Creation', 'Harish'),
    (2,  'E- khata to be checked if its open', 'Rohith'),
    (2,  'Deal Term Sheet Drafting & Signing', 'Puja'),
    (2,  'Safebuy Payment Collection', 'Puja'),
    (3,  'Legal Due Diligence to be initiated', 'Puja'),
    (5,  'Bank to be selected', 'Puja'),
    (9,  'Legal Due Diligence to be completed', 'Puja'),
    (10, 'Token Release', 'Puja'),
    (10, 'AFS Draft to be shared', 'Ramswaroop'),
    (11, 'Penny test to be completed', 'Puja'),
    (13, 'Estamp Procurement', 'Rohith'),
    (13, 'Stamp duty + Estamp charges collection', 'Puja'),
    (15, 'AFS Signing', 'Ramswaroop'),
    (15, 'Jumbo Fee Collection from the Seller', 'Puja'),
    (16, 'Sale deed to be shared on group', 'Ramswaroop'),
    (21, 'Bank Legal Evaluation', 'Rohith'),
    (25, 'Bank Technical Evaluation', 'Rohith'),
    (30, 'ODV', 'Rohith'),
    (35, 'Docket signing', 'Rohith'),
    (36, 'TDS Payment', 'Harish'),
    (37, 'SRO Slot booking', 'Rohith'),
    (40, 'Sale deed Registration', 'Rohith'),
    (55, 'E Khata Transfer', 'Rohith'),
    (56, 'Property Tax Name Change', 'Rohith'),
    (70, 'BESCOM', 'Rohith'),
]
ASSIGNMENT = [
    (1,  'Token Payment +Whatsapp Grp Creation', 'Harish'),
    (2,  'Deal Term Sheet Drafting & Signing', 'Puja'),
    (2,  'Safebuy Payment Collection', 'Puja'),
    (3,  'Legal Due Diligence to be initiated', 'Puja'),
    (5,  'Bank to be selected', 'Puja'),
    (9,  'Legal Due Diligence to be completed', 'Puja'),
    (10, 'Token Release', 'Puja'),
    (10, 'MOU draft to be shared on group', 'Ramswaroop'),
    (11, 'MOU Draft to be confirmed with the builder', 'Ramswaroop'),
    (13, 'Penny test to be completed', 'Puja'),
    (15, 'Stamp duty + Estamp charges collection', 'Puja'),
    (15, 'Estamp Procurement', 'Rohith'),
    (15, 'MOU Signing', 'Ramswaroop'),
    (16, 'Jumbo Fee Collection from the Seller', 'Puja'),
    (21, 'Assignment Agrement Initiation with the builder', 'Ramswaroop'),
    (25, 'Bank Legal Evaluation', 'Rohith'),
    (27, 'Bank Technical Evaluation', 'Rohith'),
    (35, 'Docket signing', 'Rohith'),
    (36, 'TDS Payment', 'Harish'),
    (37, 'Assignment Agrement to be signed', 'Ramswaroop'),
    (42, 'Loan Disbursement', 'Rohith'),
]


def log(msg):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


def gql(query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        GRAPHQL_URL, data=data,
        headers={"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=30)
    result = json.loads(resp.read())
    if "errors" in result:
        raise Exception("GraphQL error: " + str(result["errors"]))
    if result.get("data") is None:
        raise Exception("GraphQL returned null data (rate limited)")
    return result["data"]


def gql_strict(query, variables=None, node_path=None):
    data = gql(query, variables)
    if node_path:
        cur = data
        for k in node_path:
            cur = (cur or {}).get(k)
        if cur is None or (isinstance(cur, dict) and cur.get("id") is None):
            raise Exception(f"Expected non-null node at {node_path}, got: {data}")
    return data


def verify_task_exists(task_id):
    q = "query($id:ID!){tasks(filter:{id:{eq:$id}}){totalCount}}"
    try:
        d = gql(q, {"id": task_id})
        return (d.get("tasks") or {}).get("totalCount", 0) > 0
    except Exception:
        return False


def create_task_with_retry(title, due_iso, assignee, oid, max_retries=3):
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            ct = gql_strict(
                "mutation CreateTask($data: TaskCreateInput!) { createTask(data: $data) { id } }",
                {"data": {"title": title, "dueAt": due_iso, "status": "TODO", "assigneeId": assignee}},
                node_path=["createTask", "id"],
            )
            task_id = ct["createTask"]["id"]
            tt = gql_strict(
                "mutation CreateTarget($data: TaskTargetCreateInput!) { createTaskTarget(data: $data) { id } }",
                {"data": {}}, node_path=["createTaskTarget", "id"])
            target_id = tt["createTaskTarget"]["id"]
            gql_strict(
                "mutation UpdateTarget($id: ID!, $data: TaskTargetUpdateInput!) { updateTaskTarget(id: $id, data: $data) { id } }",
                {"id": target_id, "data": {"taskId": task_id, "targetOpportunityId": oid}},
                node_path=["updateTaskTarget", "id"])
            if verify_task_exists(task_id):
                return task_id
            last_err = "task not found after create (phantom id)"
        except Exception as ex:
            last_err = str(ex)
        time.sleep(3 * attempt)
    log(f"[{oid}] FAILED to create task '{title}' after {max_retries} attempts: {last_err}")
    return None


def offer_linked_titles(offer_id):
    """Return the set of task TITLES linked to this offer.

    SAFETY: under rate-limit pressure this nested read can return a partial or
    empty result. If the query fails or returns no edges at all, we raise instead
    of returning an empty set -- an empty set would make the caller believe every
    task is missing and re-create duplicates (phantom-ID bug, 2026-07-16: J-441
    got 5-6x copies). Callers must treat an empty result from a *successful* read
    (offer genuinely has zero tasks) as legitimate, but a *failed/partial* read as
    fatal for that offer.
    """
    q = """query($oid:ID!){
      opportunities(filter:{id:{eq:$oid}}){
        edges{ node{ taskTargets{ edges{ node{ task{ id title assignee{ id } } } } } } }
      }
    }"""
    d = gql(q, {"oid": offer_id})  # raises on error/rate-limit/null-data
    edges = d["opportunities"]["edges"]
    if not edges:
        # Offer not found -- treat as fatal for this run (don't blast tasks).
        raise RuntimeError(f"offer {offer_id} not returned by read")
    titles = set()
    for e in edges[0]["node"]["taskTargets"]["edges"]:
        tk = e["node"].get("task")
        if tk and tk.get("title"):
            titles.add(tk["title"])
    return titles


def offer_booking_date(offer_id):
    q = """query($oid:ID!){ opportunities(filter:{id:{eq:$oid}}){ edges{node{bookingDate}}}}"""
    try:
        return gql(q, {"oid": offer_id})["opportunities"]["edges"][0]["node"].get("bookingDate")
    except Exception:
        return None


def working_day(start_date, n):
    d = start_date
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() != 6:  # Sunday only skipped; Saturdays are working days
            added += 1
    return d


def iso_due(anchor, n):
    due = working_day(anchor, n)
    dt = datetime(due.year, due.month, due.day, 18, 0, 0, tzinfo=IST)
    return dt.isoformat()


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed": {}}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def find_token_paid():
    q = """query{
      opportunities(filter:{and:[
        {or:[{transactionType:{eq:RESALE}},{transactionType:{eq:ASSIGNMENT}}]},
        {stage:{eq:TOKEN_PAID}}
      ]}){ edges{ node{ id name transactionType bookingDate } } }
    }"""
    return [(e["node"]["id"], e["node"]["transactionType"], e["node"].get("bookingDate"))
            for e in gql(q)["opportunities"]["edges"]]


def main():
    state = load_state()
    test_offer = None
    if "--test-offer" in sys.argv:
        idx = sys.argv.index("--test-offer")
        if idx + 1 < len(sys.argv):
            test_offer = sys.argv[idx + 1]

    found = find_token_paid()
    log(f"Found {len(found)} TOKEN_PAID RESALE/ASSIGNMENT offers")

    pending = []
    for oid, txn, bd in found:
        if oid in state.get("processed", {}):
            continue
        if test_offer and oid != test_offer:
            continue
        if not bd:
            log(f"[{oid}] no bookingDate; skipping (will retry when dated)")
            continue
        pending.append((oid, txn, bd))

    log(f"pending={len(pending)} offers to process")
    errors = []
    generated_total = 0

    for oid, txn, bd in pending:
        tasks = RESALE if txn == "RESALE" else ASSIGNMENT
        anchor = datetime.strptime(bd, "%Y-%m-%d").date()
        try:
            linked = offer_linked_titles(oid)
        except Exception as e:
            # Read failed (rate-limit / partial) -- DO NOT re-create. Retry next run.
            log(f"[{oid}] SKIP: linked-title read failed ({e}); will retry next run")
            errors.append(f"[{oid}] read failed; skipped to avoid duplicates")
            continue
        failed_here = 0
        log(f"[{oid}] txn={txn} tasks={len(tasks)} already_linked={len(linked)}")

        for day, title, poc in tasks:
            assignee = POC.get(poc)
            if not assignee:
                errors.append(f"[{oid}] Unknown POC '{poc}' for '{title}'")
                failed_here += 1
                continue
            if title in linked:
                log(f"[{oid}] dedup skip: {title}")
                continue
            due_iso = iso_due(anchor, day)
            tid = create_task_with_retry(title, due_iso, assignee, oid)
            if not tid:
                errors.append(f"[{oid}] createTask failed for '{title}'")
                failed_here += 1
                continue
            linked.add(title)
            log(f"[{oid}] created task day={day} '{title}'")
            time.sleep(SLEEP_BETWEEN_TASKS_S)

        for attempt in range(1, 4):
            try:
                current = offer_linked_titles(oid)
            except Exception as e:
                log(f"[{oid}] reconcile read failed ({e}); stopping reconcile")
                break
            missing = [t for _, t, _ in tasks if t not in current]
            if not missing:
                break
            log(f"[{oid}] reconcile pass {attempt}: {len(missing)} missing")
            for day, title, poc in tasks:
                if title not in missing:
                    continue
                assignee = POC.get(poc)
                if not assignee:
                    continue
                due_iso = iso_due(anchor, day)
                create_task_with_retry(title, due_iso, assignee, oid)
                time.sleep(SLEEP_BETWEEN_TASKS_S)

        final = offer_linked_titles(oid)
        missing = [t for _, t, _ in tasks if t not in final]
        actual = len(tasks) - len(missing)
        log(f"[{oid}] FINAL linked={actual}/{len(tasks)}")

        if not missing:
            state.setdefault("processed", {})[oid] = {
                "generatedAt": datetime.now(IST).isoformat(),
                "transactionType": txn,
                "tasksCreated": actual,
            }
        else:
            errors.append(f"[{oid}] partial ({actual}/{len(tasks)}); retry next run")
        generated_total += actual

    save_state(state)
    if errors:
        print("ERRORS:")
        for er in errors:
            print("  " + er)
        print(f"Tasks created this run: {generated_total}")
        sys.exit(1)
    else:
        log(f"All done. Tasks created this run: {generated_total}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("FATAL: " + str(e))
        traceback.print_exc()
        sys.exit(2)
