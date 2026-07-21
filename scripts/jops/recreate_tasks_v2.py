#!/usr/bin/env python3
"""
recreate_tasks_v2.py  -- Jumbo Homes  (2026-07-20, Tarun-approved v2)

Soft-deleted the 859 TAT tasks linked to RESALE/ASSIGNMENT offers at stages
TOKEN_PAID / AFS_MOU_SIGNED / TERM_SHEET_SIGNED (4 untyped offers excluded).
This script RE-CREATES them from the same templates as the live generator
(RESALE=25, ASSIGNMENT=21), with the ONLY change being the assignee:
  - legalCounselId when present on the offer, else NULL (unassigned).
Due dates:
  - anchored on offer.bookingDate (Sat working, Sun skip, 18:00 IST)
  - omitted entirely when bookingDate is NULL.

Mechanics: hardened GraphQL two-step link (createTask -> createTaskTarget ->
updateTaskTarget), gql_strict + verify, LIMIT_REACHED -> 65s retry, state-file
resume. Re-runnable: each offer only processed once (full-coverage guard).
"""
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone

LOG_PATH = "/opt/jops/recreate_tasks_v2.log"
API_KEY_PATH = "/root/.twenty/api_key.txt"
STATE_FILE = "/opt/jops/recreate_tasks_v2.state.json"

SCHEMA = "workspace_1l3urgumjmspnjxohclmfz6fx"
SLEEP_BETWEEN_TASKS_S = 1.5
IST = timezone(timedelta(hours=5, minutes=30))

API_KEY = open(API_KEY_PATH).read().strip()
GRAPHQL_URL = "http://localhost:3000/graphql"

# (day, title) -- from the deal-process sheet TAT tabs (POC stripped; assignee =
# Legal Counsel per v2). RESALE = 25, ASSIGNMENT = 21.
RESALE = [
    (1,  'Token Payment +Whatsapp Grp Creation'),
    (2,  'E- khata to be checked if its open'),
    (2,  'Deal Term Sheet Drafting & Signing'),
    (2,  'Safebuy Payment Collection'),
    (3,  'Legal Due Diligence to be initiated'),
    (5,  'Bank to be selected'),
    (9,  'Legal Due Diligence to be completed'),
    (10, 'Token Release'),
    (10, 'AFS Draft to be shared'),
    (11, 'Penny test to be completed'),
    (13, 'Estamp Procurement'),
    (13, 'Stamp duty + Estamp charges collection'),
    (15, 'AFS Signing'),
    (15, 'Jumbo Fee Collection from the Seller'),
    (16, 'Sale deed to be shared on group'),
    (21, 'Bank Legal Evaluation'),
    (25, 'Bank Technical Evaluation'),
    (30, 'ODV'),
    (35, 'Docket signing'),
    (36, 'TDS Payment'),
    (37, 'SRO Slot booking'),
    (40, 'Sale deed Registration'),
    (55, 'E Khata Transfer'),
    (56, 'Property Tax Name Change'),
    (70, 'BESCOM'),
]
ASSIGNMENT = [
    (1,  'Token Payment +Whatsapp Grp Creation'),
    (2,  'Deal Term Sheet Drafting & Signing'),
    (2,  'Safebuy Payment Collection'),
    (3,  'Legal Due Diligence to be initiated'),
    (5,  'Bank to be selected'),
    (9,  'Legal Due Diligence to be completed'),
    (10, 'Token Release'),
    (10, 'MOU draft to be shared on group'),
    (11, 'MOU Draft to be confirmed with the builder'),
    (13, 'Penny test to be completed'),
    (15, 'Stamp duty + Estamp charges collection'),
    (15, 'Estamp Procurement'),
    (15, 'MOU Signing'),
    (16, 'Jumbo Fee Collection from the Seller'),
    (21, 'Assignment Agrement Initiation with the builder'),
    (25, 'Bank Legal Evaluation'),
    (27, 'Bank Technical Evaluation'),
    (35, 'Docket signing'),
    (36, 'TDS Payment'),
    (37, 'Assignment Agrement to be signed'),
    (42, 'Loan Disbursement'),
]


def log(msg):
    line = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST") + " " + msg
    print(line)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def gql(query, variables=None, retries=8):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        req = urllib.request.Request(
            GRAPHQL_URL, data=data,
            headers={"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read())
        except Exception as ex:
            if attempt < retries - 1:
                time.sleep(65); continue
            raise
        if "errors" in result:
            errs = str(result["errors"])
            if "LIMIT_REACHED" in errs:
                log("  rate-limit hit, sleeping 65s")
                time.sleep(65); continue
            raise Exception("GraphQL error: " + errs)
        if result.get("data") is None:
            if attempt < retries - 1:
                time.sleep(65); continue
            raise Exception("GraphQL returned null data (rate limited)")
        return result["data"]
    raise Exception("GraphQL exhausted retries")


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
            data = {"title": title, "status": "TODO"}
            if due_iso:
                data["dueAt"] = due_iso
            if assignee:
                data["assigneeId"] = assignee
            ct = gql_strict(
                "mutation CreateTask($data: TaskCreateInput!) { createTask(data: $data) { id } }",
                {"data": data}, node_path=["createTask", "id"])
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
    log(f"  [FAIL] create '{title}' after {max_retries} attempts: {last_err}")
    return None


def offer_linked_titles(offer_id):
    q = """query($oid:ID!){
      opportunities(filter:{id:{eq:$oid}}){
        edges{ node{ taskTargets{ edges{ node{ task{ id title assignee{ id } } } } } } }
      }
    }"""
    d = gql(q, {"oid": offer_id})
    edges = d["opportunities"]["edges"]
    if not edges:
        raise RuntimeError(f"offer {offer_id} not returned by read")
    titles = set()
    for e in edges[0]["node"]["taskTargets"]["edges"]:
        tk = e["node"].get("task")
        if tk and tk.get("title"):
            titles.add(tk["title"])
    return titles


def working_day(start_date, n):
    d = start_date
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() != 6:
            added += 1
    return d


def iso_due(anchor, n):
    due = working_day(anchor, n)
    dt = datetime(due.year, due.month, due.day, 18, 0, 0, tzinfo=IST)
    return dt.isoformat()


def fetch_offers():
    """Pull the 36 in-scope offers + legalCounsel via SQL (reliable)."""
    sql = (
        "SELECT o.id, o.name, o.\"transactionType\"::text, o.\"bookingDate\", o.\"legalCounselId\" "
        f"FROM \"{SCHEMA}\".\"opportunity\" o "
        "WHERE o.\"deletedAt\" IS NULL "
        "AND o.stage IN ('TOKEN_PAID','AFS_MOU_SIGNED','TERM_SHEET_SIGNED') "
        "AND o.\"transactionType\" IN ('RESALE','ASSIGNMENT') "
        "ORDER BY o.stage, o.\"transactionType\", o.name;"
    )
    with open("/tmp/_offers_q.sql", "w") as f:
        f.write(sql)
    subprocess.run(["docker", "cp", "/tmp/_offers_q.sql", "twenty-db-1:/tmp/_offers_q.sql"], check=True)
    out = subprocess.run(
        ["docker", "exec", "twenty-db-1", "psql", "-U", "twenty", "-d", "default", "-t", "-A", "-F", "|", "-f", "/tmp/_offers_q.sql"],
        capture_output=True, text=True, timeout=60)
    offers = []
    for line in out.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        oid, name, tt, bd, lc = parts[0], parts[1], parts[2], parts[3], parts[4]
        if bd == "\\N" or bd == "":
            bd = None
        if lc == "\\N" or lc == "":
            lc = None
        offers.append({"id": oid, "name": name, "tt": tt, "bookingDate": bd, "legalCounselId": lc})
    return offers


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


def main():
    state = load_state()
    offers = fetch_offers()
    log(f"Fetched {len(offers)} in-scope offers")
    total_created = 0
    errors = []

    for o in offers:
        oid = o["id"]
        if oid in state.get("processed", {}):
            log(f"[{oid}] {o['name']} already processed; skip")
            continue
        tt = o["tt"]
        tasks = RESALE if tt == "RESALE" else ASSIGNMENT
        assignee = o["legalCounselId"]  # None -> unassigned per v2
        anchor = None
        if o["bookingDate"]:
            try:
                anchor = datetime.strptime(o["bookingDate"], "%Y-%m-%d").date()
            except Exception:
                anchor = None
        log(f"[{oid}] {o['name']} tt={tt} lc={'SET' if assignee else 'none'} bd={'SET' if anchor else 'none'} tasks={len(tasks)}")
        try:
            linked = offer_linked_titles(oid)
        except Exception as e:
            log(f"[{oid}] SKIP: linked-title read failed ({e})")
            errors.append(f"[{oid}] read failed; skipped")
            continue
        created_here = 0
        for day, title in tasks:
            if title in linked:
                continue
            due_iso = iso_due(anchor, day) if anchor else None
            tid = create_task_with_retry(title, due_iso, assignee, oid)
            if not tid:
                errors.append(f"[{oid}] create failed '{title}'")
                continue
            linked.add(title)
            created_here += 1
            time.sleep(SLEEP_BETWEEN_TASKS_S)
        # reconciliation
        for attempt in range(1, 4):
            try:
                current = offer_linked_titles(oid)
            except Exception as e:
                log(f"[{oid}] reconcile read failed ({e}); stop reconcile")
                break
            missing = [t for _, t in tasks if t not in current]
            if not missing:
                break
            log(f"[{oid}] reconcile pass {attempt}: {len(missing)} missing")
            for day, title in tasks:
                if title not in missing:
                    continue
                due_iso = iso_due(anchor, day) if anchor else None
                create_task_with_retry(title, due_iso, assignee, oid)
                time.sleep(SLEEP_BETWEEN_TASKS_S)
        final = offer_linked_titles(oid)
        missing = [t for _, t in tasks if t not in final]
        actual = len(tasks) - len(missing)
        log(f"[{oid}] FINAL {actual}/{len(tasks)} (created this run {created_here})")
        if not missing:
            state.setdefault("processed", {})[oid] = {
                "generatedAt": datetime.now(IST).isoformat(),
                "transactionType": tt,
                "tasksCreated": created_here,
                "assignee": assignee,
            }
        else:
            errors.append(f"[{oid}] partial {actual}/{len(tasks)}")
        total_created += created_here

    save_state(state)
    log(f"DONE. Created this run: {total_created}")
    if errors:
        log("ERRORS:")
        for er in errors:
            log("  " + er)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("FATAL: " + str(e))
        traceback.print_exc()
        raise
