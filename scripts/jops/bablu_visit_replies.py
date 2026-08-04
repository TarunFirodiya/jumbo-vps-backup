#!/usr/bin/env python3
"""Apply emoji reactions on Bablu visit alerts to Twenty Visit status."""
import json, os, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path

CHANNEL = "C09AN156SRM"
STATE = Path("/opt/jops/bablu_visit_replies_state.json")
MAP = Path("/opt/jops/visit_slack_map.json")
ENV = Path("/root/.hermes/profiles/bablu/.env")
API_KEY = Path("/root/.twenty/api_key.txt")
SLACK = "https://slack.com/api/"
CRM = "http://127.0.0.1:3000/graphql"
BOT = "U0BBGL8FP7Z"
REACTION_STATUS = {
    "white_check_mark": "CONFIRMED",
    "heavy_check_mark": "COMPLETED",
    "x": "CANCELLED",
    "no_entry_sign": "CANCELLED",
}


def env(name):
    for line in ENV.read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return ""


def slack(method, payload):
    req = urllib.request.Request(
        SLACK + method,
        data=urllib.parse.urlencode(payload).encode(),
        headers={"Authorization": "Bearer " + env("SLACK_BOT_TOKEN"),
                 "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    if not data.get("ok"):
        raise RuntimeError("slack:" + method + ":" + str(data.get("error", "unknown")))
    return data


def gql(query, variables=None):
    req = urllib.request.Request(
        CRM,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": "Bearer " + API_KEY.read_text().strip(),
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    if data.get("errors"):
        raise RuntimeError("crm_graphql")
    return data.get("data") or {}


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    os.replace(tmp, STATE)


def member_for_user(user_id, cache):
    if user_id in cache:
        return cache[user_id]
    user = slack("users.info", {"user": user_id}).get("user") or {}
    profile = user.get("profile") or {}
    email = (profile.get("email") or "").strip().lower()
    slack_names = {str(user.get("name") or "").strip().lower(),
                   str(user.get("real_name") or "").strip().lower(),
                   str(profile.get("real_name") or "").strip().lower()}
    query = """query { workspaceMembers(first: 100) { edges { node { id userEmail name { firstName lastName } } } } }"""
    edges = (gql(query).get("workspaceMembers") or {}).get("edges") or []
    matches = []
    for edge in edges:
        node = edge.get("node") or {}
        name = node.get("name") or {}
        first = str(name.get("firstName") or "").strip().lower()
        last = str(name.get("lastName") or "").strip().lower()
        full = (first + " " + last).strip()
        if email and str(node.get("userEmail") or "").strip().lower() == email:
            matches.append(node)
        elif slack_names.intersection({first, full}):
            matches.append(node)
    cache[user_id] = matches[0] if len(matches) == 1 else None
    return cache[user_id]


def update_visit(visit_id, status, member_id):
    query = """mutation($id: ID!, $input: VisitUpdateInput!) { updateVisit(id: $id, data: $input) { id status } }"""
    result = gql(query, {"id": visit_id, "input": {"status": status, "visitAgentId": member_id}})
    return (result.get("updateVisit") or {}).get("id") == visit_id


def main():
    state = read_json(STATE, {"replies": {}})
    seen = state.setdefault("replies", {})
    mapping = read_json(MAP, {})
    history = slack("conversations.history", {"channel": CHANNEL, "limit": 100}).get("messages") or []
    cache = {}
    changed = 0
    for root in history:
        root_ts = root.get("ts")
        visit_id = (mapping.get(root_ts) or {}).get("visit_id")
        if not root_ts or not visit_id:
            continue
        for reaction in root.get("reactions") or []:
            status = REACTION_STATUS.get(reaction.get("name"))
            if not status:
                continue
            for user_id in reaction.get("users") or []:
                if user_id == BOT:
                    continue
                event_key = root_ts + ":" + reaction["name"] + ":" + user_id
                if event_key in seen:
                    continue
                member = member_for_user(user_id, cache)
                if not member:
                    seen[event_key] = "unmapped"
                    continue
                try:
                    if not update_visit(visit_id, status, member["id"]):
                        raise RuntimeError("crm_update_not_verified")
                    # Success reaction is best-effort. Missing reactions:write
                    # must never undo a verified CRM update.
                    try:
                        slack("reactions.add", {"channel": CHANNEL, "timestamp": root_ts, "name": "white_check_mark"})
                    except Exception as reaction_error:
                        print("VISIT_REACTION_WARNING visit=%s reason=%s" % (visit_id, str(reaction_error)), file=sys.stderr)
                    seen[event_key] = "updated:" + status
                    changed += 1
                except Exception as error:
                    print("VISIT_REACTION_ERROR visit=%s status=%s reason=%s" % (visit_id, status, type(error).__name__), file=sys.stderr)
    if len(seen) > 5000:
        for key in list(seen)[:-4000]:
            del seen[key]
    save_state(state)
    if changed:
        print("VISIT_REACTIONS_UPDATED=%d" % changed)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("VISIT_REACTION_ERROR reason=%s" % type(error).__name__, file=sys.stderr)
        sys.exit(1)

if False:
    subprocess.run([])
