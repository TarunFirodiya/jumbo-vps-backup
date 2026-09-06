#!/usr/bin/env python3
"""
Kapso Conversation Sync — Agent-aware version (v2.0, Sep 2026)
Fetches Kapso WhatsApp conversations via CLI, detects which agent (Ananya buyer / Tara seller)
the conversation belongs to, generates agent-specific LLM summaries, and upserts into Twenty CRM
via direct SQL with proper enquiry/seller/property linking.

Usage: python3 kapso-sync.py
"""
import json, os, subprocess, sys, time, uuid
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5))
WORKSPACE = "workspace_1l3urgumjmspnjxohclmfz6fx"
TABLE = WORKSPACE + "._communication"
KAPSO_PROJECT_ID = "6c8c7064-840f-436d-8d28-89c8e1751052"
KAPSO_INBOX_URL = "https://inbox.kapso.ai/projects/" + KAPSO_PROJECT_ID
EMOJI = chr(0x1f4ac)

KAPSO_CLI = "/root/.hermes/node/bin/kapso"
CONFIG_PATH = "/opt/jops/kapso_agent_config.json"

# Agent-specific summary prompts keyed by agent name
SUMMARY_PROMPTS = {
    "ananya": (
        "Write a 5-6 word headline for this WhatsApp conversation between a real estate agent "
        "and a potential home buyer. Focus on: budget, location, BHK requirement, visit status. "
        "Rules: NO filler words. Headline style. If visit scheduled, start with '✅ visit scheduled,'"
    ),
    "tara": (
        "Write a 5-6 word headline for this WhatsApp conversation between a real estate agent "
        "and a home seller. Focus on: property details, timeline to sell, price expectations, "
        "proposal status. Rules: NO filler words. Headline style. If proposal accepted, "
        "start with '✅ proposal accepted,'"
    ),
}

# Load agent config
with open(CONFIG_PATH) as f:
    AGENT_CONFIG = json.load(f)

AGENT_MAP = AGENT_CONFIG["phone_number_id_to_agent"]
AGENT_DETAILS = AGENT_CONFIG["agent_phone_numbers"]
DEFAULT_AGENT = AGENT_CONFIG.get("default_agent", "ananya")

# Load OPENROUTER_API_KEY
OPENROUTER_API_KEY = ""
env_file = "/root/.hermes/profiles/operator/.env"
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                OPENROUTER_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")


def esc_sql(s):
    return s.replace("'", "''")


def run_sql(sql):
    """Execute SQL in twenty-db-1 Docker container via stdin pipe."""
    try:
        cmd = ["docker", "exec", "-i", "twenty-db-1", "psql", "-U", "twenty",
               "-d", "default", "-t", "-A", "-F", "|"]
        r = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    except Exception as e:
        print(f"    [SQL ERROR] {e}", file=sys.stderr)
        return ""


def kapso_cli(args, page=1, per_page=100):
    """Run kapso CLI command and return parsed JSON."""
    env = os.environ.copy()
    env["KAPSO_API_KEY"] = open("/opt/jops/kapso-api-key.txt").read().strip()
    env["KAPSO_PROJECT_ID"] = KAPSO_PROJECT_ID

    cmd = [KAPSO_CLI] + args + ["--output", "json", "--per-page", str(per_page)]
    if page > 1:
        cmd += ["--page", str(page)]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
        if r.returncode == 0:
            return json.loads(r.stdout)
        else:
            print(f"    [CLI ERR] {r.stderr[:200]}", file=sys.stderr)
            return None
    except Exception as e:
        print(f"    [CLI ERR] {e}", file=sys.stderr)
        return None


def kapso_cli_paginated(args):
    """Run kapso CLI with auto-pagination."""
    all_items = []
    page = 1
    while True:
        data = kapso_cli(args, page=page)
        if not data or "data" not in data:
            break
        items = data["data"]
        all_items.extend(items)

        total = data.get("paging", {}).get("total", len(items))
        if len(all_items) >= total or len(items) < 100:
            break
        page += 1
        time.sleep(0.3)
    return all_items


def fetch_conversation_messages(conv_id):
    """Fetch all messages for a conversation via CLI."""
    all_msgs = []
    after = None
    while True:
        args = ["whatsapp", "messages", "list", "--conversation", conv_id,
                "--per-page", "100", "--output", "json"]
        if after:
            args += ["--after", after]

        env = os.environ.copy()
        env["KAPSO_API_KEY"] = open("/opt/jops/kapso-api-key.txt").read().strip()
        env["KAPSO_PROJECT_ID"] = KAPSO_PROJECT_ID

        try:
            r = subprocess.run([KAPSO_CLI] + args, capture_output=True, text=True,
                               timeout=60, env=env)
            if r.returncode != 0:
                break
            data = json.loads(r.stdout)
            if not data or "data" not in data:
                break
            items = data["data"]
            all_msgs.extend(items)
            after = data.get("paging", {}).get("cursors", {}).get("after")
            if not after or len(items) < 100:
                break
        except Exception as e:
            print(f"    [FETCH ERR] {e}", file=sys.stderr)
            break

    return all_msgs


def detect_agent(conv):
    """Detect which agent a Kapso conversation belongs to by phone_number_id.

    Returns (agent_name, agent_detail_dict). Falls back to default_agent.
    """
    phone_number_id = conv.get("phone_number_id", "")
    agent_key = AGENT_MAP.get(phone_number_id, DEFAULT_AGENT)
    detail = AGENT_DETAILS.get(phone_number_id, AGENT_DETAILS.get(
        next(k for k in AGENT_DETAILS if AGENT_DETAILS[k]["agent"] == agent_key),
        AGENT_DETAILS[list(AGENT_DETAILS.keys())[0]]
    ))
    return agent_key, detail


def find_person_by_phone(phone):
    """Find CRM person by phone, trying multiple formats.

    Tries: 10-digit, 11-digit (0-prefix), 12-digit (91-prefix), +91-prefix.
    Returns (id, first_name, last_name) tuple or None.
    """
    norm = "".join(c for c in phone if c.isdigit())
    variants = set()

    if len(norm) == 12 and norm.startswith("91"):
        variants.add(norm)         # 911234567890
        variants.add(norm[2:])     # 1234567890
        variants.add("+91" + norm[2:])  # +911234567890
    elif len(norm) > 10:
        variants.add(norm)          # full digits
        variants.add(norm[-10:])    # last 10
        variants.add("+91" + norm[-10:])
    elif len(norm) == 11 and norm.startswith("0"):
        variants.add(norm)
        variants.add(norm[1:])
        variants.add("+91" + norm[1:])
    elif len(norm) == 10:
        variants.add(norm)
        variants.add("91" + norm)
        variants.add("+91" + norm)
    else:
        variants.add(norm)

    for v in variants:
        row = run_sql(
            f'SELECT id, "nameFirstName", "nameLastName" FROM {WORKSPACE}.person '
            f"WHERE \"phonesPrimaryPhoneNumber\" = '{v}' LIMIT 1;"
        )
        if row:
            pp = row.split("|")
            return (pp[0], pp[1] if len(pp) > 1 else "", pp[2] if len(pp) > 2 else "")
    return None


def find_enquiry(person_id):
    """Find latest active enquiry for a person (used by Ananya/buyer pipeline)."""
    row = run_sql(
        f'SELECT id FROM {WORKSPACE}._enquiry '
        f"WHERE \"personId\" = '{person_id}' AND \"deletedAt\" IS NULL "
        f'ORDER BY "createdAt" DESC LIMIT 1;'
    )
    return row.split("|")[0] if row else None


def find_seller_and_property(person_id):
    """Find seller and linked property for a person (used by Tara/seller pipeline).

    Returns dict with sellerId, propertyId, propertyName or None.
    """
    row = run_sql(
        f'SELECT s.id, p.id, p.name FROM {WORKSPACE}._seller s '
        f'LEFT JOIN {WORKSPACE}._property p ON p."sellerId" = s.id AND p."deletedAt" IS NULL '
        f"WHERE s.\"personId\" = '{person_id}' AND s.\"deletedAt\" IS NULL "
        f'ORDER BY p."createdAt" DESC LIMIT 1;'
    )
    if row:
        parts = row.split("|")
        return {"sellerId": parts[0],
                "propertyId": parts[1] if len(parts) > 1 and parts[1] else None,
                "propertyName": parts[2] if len(parts) > 2 and parts[2] else None}
    return None


def gen_summary(msgs, agent_detail):
    """Generate headline-style summary via OpenRouter, agent-specific prompt.

    Rules:
    - 5-6 words max, headline style, NO filler
    - Buyer prompt: budget, location, BHK, visit status
    - Seller prompt: property details, timeline, price, proposal status
    - Emoji signal: 💬 (10+ msgs), 🔵 (3-9 msgs), ⚪ (1-2 msgs)
    """
    if not OPENROUTER_API_KEY or not msgs:
        return ""

    raw = fmt_msgs(msgs[:50])
    if len(raw) > 3000:
        raw = raw[:3000]

    msg_count = len(msgs)
    if msg_count >= 10:
        length_emoji = "💬"
    elif msg_count >= 3:
        length_emoji = "🔵"
    else:
        length_emoji = "⚪"

    prompt_text = SUMMARY_PROMPTS.get(agent_detail["agent"],
        SUMMARY_PROMPTS["ananya"]
    )

    import urllib.request
    payload = json.dumps({
        "model": "openai/gpt-4o-mini",
        "messages": [{"role": "user", "content":
            f"{prompt_text}\n\n{length_emoji} Conversation ({msg_count} msgs):\n" + raw
        }],
        "max_tokens": 40
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=payload,
        headers={"Authorization": "Bearer " + OPENROUTER_API_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read())
            return d["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"    [LLM ERR] {e}", file=sys.stderr)
        return ""


def fmt_msgs(msgs):
    """Format Kapso message list into rawMessage string (plain text)."""
    lines = []
    for m in msgs:
        ts = int(m.get("timestamp", 0))
        dt = datetime.fromtimestamp(ts, tz=IST)
        d = m.get("kapso", {}).get("direction", "unknown").upper()
        c = m.get("kapso", {}).get("content", "") or m.get("text", {}).get("body", "")
        if isinstance(c, dict):
            c = str(c.get("body", c))
        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{ts_str}] {d}: {c}")
    return "\n---\n".join(lines)


def fmt_msgs_prosemirror(msgs):
    """Format Kapso message list into ProseMirror bare array for entireChatBlocknote.

    CRITICAL: Must use bare array format, NOT {"type":"doc","content":[...]} wrapper.
    The doc-wrapper format renders completely empty in CRM UI.
    """
    paragraphs = []
    for m in msgs:
        ts = int(m.get("timestamp", 0))
        dt = datetime.fromtimestamp(ts, tz=IST)
        d = m.get("kapso", {}).get("direction", "unknown").upper()
        c = m.get("kapso", {}).get("content", "") or m.get("text", {}).get("body", "")
        if isinstance(c, dict):
            c = str(c.get("body", c))
        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts_str}] {d}: {c}"
        paragraphs.append({
            "id": str(uuid.uuid4()),
            "type": "paragraph",
            "props": {"backgroundColor": "default", "textColor": "default", "textAlignment": "left"},
            "content": [],
            "children": [{"text": line}]
        })
    return json.dumps(paragraphs)


def process_one(conv):
    """Process one Kapso conversation -> CRM, agent-aware."""
    cid = conv["id"]
    phone = conv.get("phone_number", "")
    contact = conv.get("contact_name", "") or conv.get("kapso", {}).get("contact_name", "")
    mc = conv.get("kapso", {}).get("messages_count", 0)

    if not phone or mc == 0:
        return "skip", "no phone or 0 msgs"

    # Detect which agent this conversation belongs to
    agent_name, agent_detail = detect_agent(conv)
    agent_label = agent_detail["label"]
    agent_dir = agent_detail.get("direction", "INBOUND")
    assigned_id = agent_detail["assignedAgentId"]

    print(f"  Agent: {agent_label} (direction={agent_dir}, assignee={assigned_id[:8]}...)")

    print(f"  Fetching messages for {cid}...", flush=True)
    msgs = fetch_conversation_messages(cid)
    if not msgs:
        return "skip", "no messages fetched"

    # Generate ProseMirror JSON for entireChatBlocknote (rich text field)
    prosemirror = fmt_msgs_prosemirror(msgs)
    if len(prosemirror) > 50000:
        prosemirror = prosemirror[:48000] + ']'

    # Timestamp from last message
    last_ts = max(int(m.get("timestamp", 0)) for m in msgs)
    last_dt = datetime.fromtimestamp(last_ts, tz=IST)
    ts_sql = last_dt.strftime("%Y-%m-%d %H:%M:%S")
    date_sql = last_dt.strftime("%Y-%m-%d")
    date_fmt = last_dt.strftime("%d %b")

    # Find person by phone (multiple formats)
    person = find_person_by_phone(phone)
    if not person:
        return "skip", f"no person for phone {phone}"

    pid, pf, pl = person
    pname = (pf + " " + pl).strip()
    # Fallback: use Kapso contact name when CRM person name is empty
    if not pname and contact:
        pname = contact

    # Agent-specific linking
    linked_enquiry_id = linked_seller_id = linked_property_id = None
    if agent_name == "ananya":
        # For Ananya (buyer): link to latest enquiry
        linked_enquiry_id = find_enquiry(pid)
        if linked_enquiry_id:
            print(f"  Linked to enquiry: {linked_enquiry_id[:8]}...")
    elif agent_name == "tara":
        # For Tara (seller): link to seller + property
        seller_data = find_seller_and_property(pid)
        if seller_data:
            linked_seller_id = seller_data["sellerId"]
            linked_property_id = seller_data["propertyId"]
            prop_name = seller_data.get("propertyName", "")
            print(f"  Linked to seller: {linked_seller_id[:8]}..., property: {prop_name or 'none'}")

    # LLM headline summary (agent-specific prompt)
    summary = gen_summary(msgs, agent_detail)
    time.sleep(0.3)

    cname = f"{EMOJI} {pname} x {agent_label} - {date_fmt}"
    ename = esc_sql(cname)
    esum = esc_sql(summary[:255])
    eprosemirror = esc_sql(prosemirror)
    emoji_esc = esc_sql(EMOJI)

    # Call link with conversation_id for direct chat opening
    call_link = f"{KAPSO_INBOX_URL}?conversation_id={cid}"
    ecall_link = esc_sql(call_link)

    # Check existing by personId + date + agent name
    exist = run_sql(
        f"SELECT id FROM {TABLE} WHERE \"personId\" = '{pid}' "
        f"AND \"communicationType\" = 'WHATSAPP' AND direction = 'INBOUND' "
        f"AND \"deletedAt\" IS NULL AND DATE(timestamp) = '{date_sql}' "
        f"AND name LIKE '{emoji_esc}%' AND name LIKE '%x {agent_label} -%' "
        f'ORDER BY "updatedAt" DESC LIMIT 1;'
    )

    # Build field assignments including optional links
    def build_updates():
        parts = [
            f"\"entireChatBlocknote\" = '{eprosemirror}'",
            f"summary = '{esum}'",
            f'"updatedAt" = NOW()',
            f"name = '{ename}'",
            f"timestamp = '{ts_sql}'::timestamptz",
            f'"callLinkPrimaryLinkUrl" = \'{ecall_link}\'',
            f"\"callLinkPrimaryLinkLabel\" = 'Open in Kapso'",
            f"\"assignedagentId\" = '{assigned_id}'",
        ]
        if linked_enquiry_id:
            parts.append(f"\"enquiryId\" = '{linked_enquiry_id}'")
        if linked_seller_id:
            parts.append(f"\"sellerId\" = '{linked_seller_id}'")
        if linked_property_id:
            parts.append(f"\"propertyId\" = '{linked_property_id}'")
        return ", ".join(parts)

    def build_insert_columns():
        cols = [
            "id", "name", "\"communicationType\"", "direction", "summary",
            "\"entireChatBlocknote\"", "timestamp", "\"personId\"", "\"createdBySource\"",
            "\"createdAt\"", "\"updatedAt\"", "position",
            "\"callLinkPrimaryLinkUrl\"", "\"callLinkPrimaryLinkLabel\"", "\"assignedagentId\""
        ]
        vals = [
            f"'{nid}'", f"'{ename}'", "'WHATSAPP'", f"'{agent_dir}'",
            f"'{esum}'", f"'{eprosemirror}'",
            f"'{ts_sql}'::timestamptz", f"'{pid}'", "'API'",
            "NOW()", "NOW()", "0",
            f"'{ecall_link}'", "'Open in Kapso'", f"'{assigned_id}'"
        ]
        if linked_enquiry_id:
            cols.append("\"enquiryId\"")
            vals.append(f"'{linked_enquiry_id}'")
        if linked_seller_id:
            cols.append("\"sellerId\"")
            vals.append(f"'{linked_seller_id}'")
        if linked_property_id:
            cols.append("\"propertyId\"")
            vals.append(f"'{linked_property_id}'")
        return ", ".join(cols), ", ".join(vals)

    if exist:
        rid = exist.split("|")[0]
        updates = build_updates()
        res = run_sql(
            f"UPDATE {TABLE} SET {updates} WHERE id = '{rid}' RETURNING id;"
        )
        if "ERROR" in res:
            return "error", res[:80]
        return "updated", f"{pname} | {agent_label} | {len(msgs)} msgs | {summary[:50]}"
    else:
        nid = str(uuid.uuid4())
        cols, vals = build_insert_columns()
        res = run_sql(
            f"INSERT INTO {TABLE} ({cols}) VALUES ({vals}) RETURNING id;"
        )
        if "ERROR" in res:
            return "error", res[:80]
        return "created", f"{pname} | {agent_label} | {len(msgs)} msgs | {summary[:50]}"


def main():
    print("=" * 60)
    print(f"Kapso Sync - {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST")
    print(f"Agents configured: {', '.join(d['agent'] + ' (' + d['display_phone'] + ')' for d in AGENT_DETAILS.values())}")
    print("=" * 60)

    print("\nFetching active conversations via kapso CLI...", flush=True)
    active_convs = kapso_cli_paginated(["whatsapp", "conversations", "list", "--status", "active"])
    print(f"Active conversations: {len(active_convs)}")

    print("Fetching recent ended conversations...", flush=True)
    ended_convs = kapso_cli_paginated(["whatsapp", "conversations", "list", "--status", "ended"])
    ended_convs = [c for c in ended_convs if c.get("kapso", {}).get("messages_count", 0) > 0]
    print(f"Ended conversations with messages: {len(ended_convs)}")

    active_phones = {c.get("phone_number", "") for c in active_convs}
    ended_convs = [c for c in ended_convs if c.get("phone_number", "") not in active_phones]
    print(f"After dedup: {len(ended_convs)} ended to process")

    all_convs = active_convs + ended_convs
    print(f"Total to sync: {len(all_convs)}")

    counts = {"created": 0, "updated": 0, "skipped": 0, "error": 0}
    agent_counts = {}
    for i, conv in enumerate(all_convs):
        cid = conv["id"]
        phone = conv.get("phone_number", "")
        contact = conv.get("contact_name", "") or conv.get("kapso", {}).get("contact_name", "")
        mc = conv.get("kapso", {}).get("messages_count", 0)
        agent_name, _ = detect_agent(conv)
        print(f"\n[{i+1}/{len(all_convs)}] {contact} ({phone}) | {mc} msgs | agent={agent_name}")

        try:
            status, detail = process_one(conv)
            # Normalize "skip" -> "skipped" for consistent summary key
            if status == "skip":
                status = "skipped"
            counts[status] = counts.get(status, 0) + 1
            agent_counts[agent_name] = agent_counts.get(agent_name, 0) + 1
            print(f"  -> {status.upper()}: {detail}")
        except Exception as e:
            counts["error"] += 1
            print(f"  -> ERROR: {e}")

        time.sleep(0.5)

    print("\n" + "=" * 60)
    print("SYNC COMPLETE")
    print(f"  Total: {len(all_convs)}")
    print(f"  Created: {counts['created']}")
    print(f"  Updated: {counts['updated']}")
    print(f"  Skipped: {counts['skipped']}")
    print(f"  Errors:  {counts['error']}")
    print(f"  Agent breakdown: {agent_counts}")
    print("=" * 60)


if __name__ == "__main__":
    main()