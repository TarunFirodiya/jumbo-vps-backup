import json, urllib.request, datetime

API_KEY = open('/root/.twenty/api_key.txt').read().strip()
URL = "http://localhost:3000/graphql"

# ids resolved from CRM
OFFERS = [
    "9b1e069e-62e7-475b-a6a0-45cd48821074",   # J1681 Vrushabadri
    "dd550618-9eab-4493-8259-159c7ff66c09",   # J933 Godrej Splendour
    "e466dcb8-63d1-4704-8c36-4bd2cdcfd5e3",   # J2281 Prestige Park
    "e4805a8b-6151-42a6-aaee-3b3a8634b99c",   # J-1367 Stanford Omkar
    "fb059bd0-5181-48c3-90cb-22260a11db4d",   # J751 Sobha Dream
]
TAT = json.load(open("/opt/jops/tat_tasks.json"))


def gql(q, v=None):
    body = json.dumps({"query": q, "variables": v or {}}).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def get_offer(oid):
    d = gql("""query($o:ID!){
      opportunities(filter:{id:{eq:$o}}){
        edges{node{transactionType taskTargets{edges{node{id taskId task{title}}}}}}
      }
    }""", {"o": oid})
    edges = d.get("data", {}).get("opportunities", {}).get("edges") or []
    if not edges:
        return None, []
    node = edges[0]["node"]
    return node.get("transactionType"), node["taskTargets"]["edges"]


def delete_task(tid):
    gql("mutation($id:ID!){deleteTask(id:$id){id}}", {"id": tid})


def delete_target(tgt):
    gql("mutation($id:ID!){deleteTaskTarget(id:$id){id}}", {"id": tgt})


backup = {"run_at": datetime.datetime.now().isoformat(), "offers": {}}
total_deleted = 0

for oid in OFFERS:
    try:
        txn, edges = get_offer(oid)
    except Exception as ex:
        print(f"!! {oid[:8]}: query failed: {ex}")
        continue
    if txn not in TAT:
        print(f"!! {oid[:8]}: txn={txn} not in template, skipping")
        continue
    expected = set(t["title"] for t in TAT[txn])

    seen = {}
    to_delete = []
    untitled = []
    for e in edges:
        node = e["node"]
        title = (node.get("task") or {}).get("title")
        tid = node.get("taskId")
        tgt = node.get("id")
        if tid is None or tgt is None:
            continue
        if title is None:
            untitled.append((tgt, tid, title))
            continue
        if title not in expected:
            # stray (old title variant) -> delete
            to_delete.append((tgt, tid, title))
        elif title in seen:
            to_delete.append((tgt, tid, title))
        else:
            seen[title] = tid

    all_del = to_delete + untitled
    backup["offers"][oid] = {
        "txn": txn,
        "before_count": len(edges),
        "expected": len(expected),
        "deleted": [{"target": t, "task": i, "title": ti} for (t, i, ti) in all_del],
    }
    for tgt, tid, title in all_del:
        try:
            delete_target(tgt)
            delete_task(tid)
            total_deleted += 1
        except Exception as ex:
            print(f"  !! delete failed {title} ({tid}): {ex}")
    # verify
    try:
        _, after = get_offer(oid)
        final_titles = set((e["node"].get("task") or {}).get("title") for e in after
                           if (e["node"].get("task") or {}).get("title"))
        print(f"{oid[:8]} {txn}: before={len(edges)} after={len(after)} expected={len(expected)} "
              f"unique_after={len(final_titles)} deleted={len(all_del)}")
    except Exception as ex:
        print(f"{oid[:8]}: verify failed: {ex}")

json.dump(backup, open("/opt/jops/dedup_backup_2026-07-08.json", "w"), indent=2)
print(f"\nTOTAL DELETED: {total_deleted}")
print("Backup: /opt/jops/dedup_backup_2026-07-08.json")
