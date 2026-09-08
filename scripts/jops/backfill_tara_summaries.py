#!/usr/bin/env python3
"""One-time backfill: regenerate Tara conversation summaries using the seller prompt.
Reads conversation text from rawMessage or entireChatBlocknote, calls LLM, updates CRM.
"""
import json, os, subprocess, sys, time
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5))
WORKSPACE = "workspace_1l3urgumjmspnjxohclmfz6fx"
TABLE = WORKSPACE + "._communication"
OPENROUTER_API_KEY = ""
env_file = "/root/.hermes/profiles/operator/.env"
if os.path.exists(env_file):
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                OPENROUTER_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")

SELLER_PROMPT = (
    "You are summarising a WhatsApp conversation between Tara, an AI seller-onboarding assistant "
    "for Jumbo Homes, and a homeowner who wants to SELL/list their home on the platform. Tara does "
    "NOT schedule visits — she collects property details, answers seller questions, and moves "
    "sellers through onboarding. Write a crisp 2-sentence summary. Sentence 1: what onboarding "
    "stage the seller is at (new lead / providing property details / discussing listing & pricing "
    "/ documents & formalities / proposal stage / stalled or unresponsive). Sentence 2: the 2-3 "
    "most important concrete facts (building/society name, configuration, expected price, "
    "timeline to sell, objections). If a proposal was accepted, begin with '✅ Proposal accepted'. "
    "NEVER mention visits. No filler, no preamble."
)

def esc_sql(s):
    return s.replace("'", "''")

def run_sql(sql):
    cmd = ["docker", "exec", "-i", "twenty-db-1", "psql", "-U", "twenty", "-d", "default", "-t", "-A", "-F", "|"]
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

def extract_text_from_blocknote(bn_json):
    """Extract plain text from the blocknote JSON."""
    try:
        data = json.loads(bn_json) if isinstance(bn_json, str) else bn_json
        texts = []
        if isinstance(data, list):
            for block in data:
                if isinstance(block, dict):
                    for child in block.get("children", []):
                        txt = child.get("text", "")
                        if txt:
                            texts.append(txt)
        elif isinstance(data, dict):
            for block in data.get("content", []):
                for item in block.get("content", []):
                    txt = item.get("text", "")
                    if txt:
                        texts.append(txt)
        return "\n---\n".join(texts)
    except:
        return str(bn_json)[:3000]

def gen_summary(text):
    if not OPENROUTER_API_KEY or not text:
        return ""
    if len(text) > 3000:
        text = text[:3000]

    import urllib.request
    payload = json.dumps({
        "model": "openai/gpt-4o-mini",
        "messages": [{"role": "user", "content": f"{SELLER_PROMPT}\n\nConversation:\n{text}"}],
        "max_tokens": 120
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=payload,
        headers={"Authorization": "Bearer " + OPENROUTER_API_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read())
            return d["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  [LLM ERR] {e}", file=sys.stderr)
        return ""

def compute_duration(raw_text):
    """Extract duration from rawMessage timestamps."""
    import re
    timestamps = re.findall(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', raw_text)
    if len(timestamps) < 2:
        return None
    first = datetime.strptime(timestamps[0], "%Y-%m-%d %H:%M:%S")
    last = datetime.strptime(timestamps[-1], "%Y-%m-%d %H:%M:%S")
    return round((last - first).total_seconds(), 1)

def main():
    print("=" * 60)
    print(f"Tara summary backfill - {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST")
    print("=" * 60)

    # Fetch all Tara records
    rows = run_sql(
        f"SELECT id::text, \"rawMessage\"::text, \"entireChatBlocknote\"::text, "
        f"name FROM {TABLE} "
        f"WHERE \"deletedAt\" IS NULL AND \"communicationType\"='WHATSAPP' "
        f"AND name LIKE '%x Tara%';"
    )
    if not rows:
        print("No Tara records found!")
        return

    count = 0
    success = 0
    skip = 0

    for line in rows.split("\n"):
        if not line or "|" not in line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        rid = parts[0].strip()
        raw = parts[1].strip()
        bn = parts[2].strip()
        name = parts[3].strip()[:60]
        count += 1

        # Extract conversation text
        text = None
        if raw and len(raw) > 10 and raw != "None":
            text = raw
        elif bn and len(bn) > 10 and bn != "None":
            text = extract_text_from_blocknote(bn)

        if not text or len(text) < 20:
            print(f"  [{count}] {name} — SKIP (no text)")
            skip += 1
            continue

        # Duration from rawMessage
        dur = compute_duration(text) if raw else None

        # Generate summary
        print(f"  [{count}] {name} — generating...", end=" ", flush=True)
        summary = gen_summary(text)
        time.sleep(0.3)

        if not summary:
            print("SKIP (empty summary)")
            skip += 1
            continue

        esum = esc_sql(summary[:255])
        updates = [f"summary = '{esum}'"]
        if dur:
            updates.append(f"duration = {dur}")
        updates_str = ", ".join(updates)

        res = run_sql(
            f"UPDATE {TABLE} SET {updates_str} WHERE id = '{rid}' RETURNING id;"
        )
        if "ERROR" in res:
            print(f"ERROR: {res[:60]}")
        else:
            print(f"OK: {summary[:80]}")
            success += 1

    print("\n" + "=" * 60)
    print(f"DONE: {success} updated, {skip} skipped (of {count} total)")
    print("=" * 60)

if __name__ == "__main__":
    main()