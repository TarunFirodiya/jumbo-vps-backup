#!/usr/bin/env python3
"""
Kapso Conversation Sync — canonical reference copy.
Fetches Kapso WhatsApp conversations via CLI, generates LLM summaries,
and upserts into Twenty CRM via direct SQL.

This is the canonical script. The production copy lives at /tmp/kapso_sync_cli.py.
Copy this to /tmp and edit there for testing.

Usage: python3 kapso_sync_cli.py
"""
import json, os, subprocess, sys, time, uuid
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5))
WORKSPACE = "workspace_1l3urgumjmspnjxohclmfz6fx"
TABLE = WORKSPACE + "._communication"
KAPSO_PROJECT_ID = "6c8c7064-840f-436d-8d28-89c8e1751052"
KAPSO_INBOX_URL = "https://inbox.kapso.ai/projects/" + KAPSO_PROJECT_ID
EMOJI = chr(0x1f4ac)
AASHISH_ID = "404bdd9e-04c6-4ec6-a913-c9d98ab07c92"

KAPSO_CLI = "/root/.hermes/node/bin/kapso"

# Load OPENROUTER_API_KEY
env_file = "/root/.hermes/profiles/operator/.env"
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                OPENROUTER_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")


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
            r = subprocess.run(["kapso"] + args, capture_output=True, text=True,
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


def gen_summary(msgs):
    """Generate headline-style summary via OpenRouter.

    Rules (Tarun's preference, June 2026):
    - 5-6 words max, headline style, NO filler
    - Visit scheduled: start with "✅ visit scheduled," then context
    - Emoji signal: 💬 (10+ msgs), 🔵 (3-9 msgs), ⚪ (1-2 msgs)
    - Crisp, punchy headlines — NOT paragraph summaries
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

    import urllib.request
    payload = json.dumps({
        "model": "openai/gpt-4o-mini",
        "messages": [{"role": "user", "content":
            "Write a 5-6 word headline for this WhatsApp conversation between a real estate agent and buyer. "
            "Rules:\n"
            "- NO filler words like 'The potential buyer is interested in'\n"
            "- Headline style, crisp, punchy\n"
            "- If a visit was scheduled, start with '✅ visit scheduled,' then add context\n\n"
            f"{length_emoji} Conversation ({msg_count} msgs):\n" + raw
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
    """Format Kapso message list into ProseMirror JSON for entireChatBlocknote."""
    doc = {"type": "doc", "content": []}
    for m in msgs:
        ts = int(m.get("timestamp", 0))
        dt = datetime.fromtimestamp(ts, tz=IST)
        d = m.get("kapso", {}).get("direction", "unknown").upper()
        c = m.get("kapso", {}).get("content", "") or m.get("text", {}).get("body", "")
        if isinstance(c, dict):
            c = str(c.get("body", c))
        ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts_str}] {d}: {c}"
        doc["content"].append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
    return json.dumps(doc)


def esc_sql(s):
    return s.replace("'", "''")


def process_one(conv):
    """Process one Kapso conversation -> CRM"""
    cid = conv["id"]
    phone = conv.get("phone_number", "")
    contact = conv.get("contact_name", "") or conv.get("kapso", {}).get("contact_name", "")
    mc = conv.get("kapso", {}).get("messages_count", 0)

    if not phone or mc == 0:
        return "skip", "no phone or 0 msgs"

    print(f"  Fetching messages for {cid}...", flush=True)
    msgs = fetch_conversation_messages(cid)
    if not msgs:
        return "skip", "no messages fetched"

    # Generate ProseMirror JSON for entireChatBlocknote (rich text field)
    prosemirror = fmt_msgs_prosemirror(msgs)
    if len(prosemirror) > 50000:
        prosemirror = prosemirror[:48000] + '"}'

    # Timestamp from last message
    last_ts = max(int(m.get("timestamp", 0)) for m in msgs)
    last_dt = datetime.fromtimestamp(last_ts, tz=IST)
    ts_sql = last_dt.strftime("%Y-%m-%d %H:%M:%S")
    date_sql = last_dt.strftime("%Y-%m-%d")
    date_fmt = last_dt.strftime("%d %b")

    # Normalize phone
    norm = "".join(c for c in phone if c.isdigit())
    if len(norm) == 12 and norm.startswith("91"):
        norm = norm[2:]
    elif len(norm) > 10:
        norm = norm[-10:]

    # Find person by 10-digit phone
    prow = run_sql(f'SELECT id, "nameFirstName", "nameLastName" FROM {WORKSPACE}.person '
                   f'WHERE "phonesPrimaryPhoneNumber" = \'{norm}\' LIMIT 1;')
    if not prow:
        return "skip", f"no person for phone {norm} (original: {phone})"

    pp = prow.split("|")
    pid = pp[0]
    pf = pp[1] if len(pp) > 1 else ""
    pl = pp[2] if len(pp) > 2 else ""
    pname = (pf + " " + pl).strip()

    # LLM headline summary
    summary = gen_summary(msgs)
    time.sleep(0.3)

    cname = f"{EMOJI} {pname} x Ananya - {date_fmt}"
    ename = esc_sql(cname)
    esum = esc_sql(summary[:255])
    eprosemirror = esc_sql(prosemirror)
    emoji_esc = esc_sql(EMOJI)

    # Call link with conversation_id for direct chat opening
    call_link = f"{KAPSO_INBOX_URL}?conversation_id={cid}"
    ecall_link = esc_sql(call_link)

    # Check existing
    exist = run_sql(
        f'SELECT id FROM {TABLE} WHERE "personId" = \'{pid}\' '
        f'AND "communicationType" = \'WHATSAPP\' AND direction = \'INBOUND\' '
        f'AND "deletedAt" IS NULL AND DATE(timestamp) = \'{date_sql}\' '
        f'AND name LIKE \'{emoji_esc}%\' ORDER BY "updatedAt" DESC LIMIT 1;'
    )

    if exist:
        rid = exist.split("|")[0]
        res = run_sql(
            f'UPDATE {TABLE} SET "entireChatBlocknote" = \'{eprosemirror}\', summary = \'{esum}\', '
            f'"updatedAt" = NOW(), name = \'{ename}\', timestamp = \'{ts_sql}\'::timestamptz, '
            f'"callLinkPrimaryLinkUrl" = \'{ecall_link}\', "callLinkPrimaryLinkLabel" = \'Open in Kapso\', '
            f'"assignedagentId" = \'{AASHISH_ID}\' '
            f"WHERE id = '{rid}' RETURNING id;"
        )
        if "ERROR" in res:
            return "error", res[:80]
        return "updated", f"{pname} | {len(msgs)} msgs | {summary[:50]}"
    else:
        nid = str(uuid.uuid4())
        res = run_sql(
            f'INSERT INTO {TABLE} (id, name, "communicationType", direction, summary, '
            f'"entireChatBlocknote", timestamp, "personId", "createdBySource", "createdAt", "updatedAt", position, '
            f'"callLinkPrimaryLinkUrl", "callLinkPrimaryLinkLabel", "assignedagentId") '
            f"VALUES ('{nid}', '{ename}', 'WHATSAPP', 'INBOUND', '{esum}', '{eprosemirror}', "
            f"'{ts_sql}'::timestamptz, '{pid}', 'API', NOW(), NOW(), 0, '{ecall_link}', 'Open in Kapso', '{AASHISH_ID}') RETURNING id;"
        )
        if "ERROR" in res:
            return "error", res[:80]
        return "created", f"{pname} | {len(msgs)} msgs | {summary[:50]}"


def main():
    print("=" * 60)
    print(f"Kapso Sync - {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST")
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
    for i, conv in enumerate(all_convs):
        cid = conv["id"]
        phone = conv.get("phone_number", "")
        contact = conv.get("contact_name", "") or conv.get("kapso", {}).get("contact_name", "")
        mc = conv.get("kapso", {}).get("messages_count", 0)
        print(f"\n[{i+1}/{len(all_convs)}] {contact} ({phone}) | {mc} msgs")

        try:
            status, detail = process_one(conv)
            counts[status] = counts.get(status, 0) + 1
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
    print("=" * 60)


if __name__ == "__main__":
    main()
