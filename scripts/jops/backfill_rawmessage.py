#!/usr/bin/env python3
"""Backfill rawMessage from existing entireChatBlocknote for ALL WhatsApp comms.
Processes in batches to avoid overloading the DB. Idempotent — only touches
records where rawMessage IS NULL.
"""
import json, os, subprocess, sys, time

WORKSPACE = "workspace_1l3urgumjmspnjxohclmfz6fx"
TABLE = WORKSPACE + "._communication"
BATCH = 200

def esc_sql(s):
    return s.replace("'", "''")

def run_sql(sql):
    cmd = ["docker", "exec", "-i", "twenty-db-1", "psql", "-U", "twenty", "-d", "default", "-t", "-A", "-F", "|"]
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=60)
    return r.stdout.strip()

def extract_text_from_blocknote(bn):
    """Extract readable conversation text from blocknote JSON."""
    try:
        data = json.loads(bn) if isinstance(bn, str) else bn
    except (json.JSONDecodeError, TypeError):
        return str(bn)[:10000]

    texts = []

    def walk(node):
        if isinstance(node, dict):
            # Primary: children[].text (our format)
            children = node.get("children")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        t = child.get("text")
                        if t and isinstance(t, str) and len(t) > 2:
                            texts.append(t)
            # Secondary: content[].content[].text (doc-wrapper format)
            content = node.get("content")
            if isinstance(content, list):
                for item in content:
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return "\n---\n".join(texts) if texts else str(data)[:10000]

def main():
    print(f"rawMessage backfill starting...")
    total = 0
    offset = 0
    
    while True:
        # Fetch batch of records without rawMessage but with blocknote
        rows = run_sql(
            f"SELECT id::text, \"entireChatBlocknote\"::text FROM {TABLE} "
            f"WHERE \"deletedAt\" IS NULL AND \"communicationType\"='WHATSAPP' "
            f"AND \"rawMessage\" IS NULL AND \"entireChatBlocknote\" IS NOT NULL "
            f"AND length(\"entireChatBlocknote\"::text) > 10 "
            f"ORDER BY \"updatedAt\" DESC "
            f"LIMIT {BATCH};"
        )
        if not rows:
            break
        
        batch_ids = []
        for line in rows.split("\n"):
            if not line or "|" not in line:
                continue
            parts = line.split("|", 1)
            if len(parts) < 2:
                continue
            rid = parts[0].strip()
            bn_text = parts[1].strip()
            
            text = extract_text_from_blocknote(bn_text)
            if not text or len(text) < 10:
                continue
            
            et = esc_sql(text)
            # Also compute duration
            import re
            timestamps = re.findall(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', text)
            duration = None
            if len(timestamps) >= 2:
                from datetime import datetime
                first = datetime.strptime(timestamps[0], "%Y-%m-%d %H:%M:%S")
                last = datetime.strptime(timestamps[-1], "%Y-%m-%d %H:%M:%S")
                duration = round((last - first).total_seconds(), 1)
            
            dur_sql = f"duration = {duration}" if duration else ""
            raw_sql = f"\"rawMessage\" = '{et}'"
            
            sql = f"UPDATE {TABLE} SET {raw_sql}"
            if dur_sql:
                sql += f", {dur_sql}"
            sql += f" WHERE id = '{rid}';"
            
            res = run_sql(sql)
            batch_ids.append(rid)
        
        count = len(batch_ids)
        total += count
        offset += count
        print(f"  Batch: {count} records ({total} total)")
        
        if count < BATCH:
            break
    
    print(f"\nDONE: {total} records backfilled with rawMessage")

if __name__ == "__main__":
    main()