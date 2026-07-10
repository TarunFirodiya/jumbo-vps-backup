#!/usr/bin/env python3
"""
Twenty CRM Schema Change Monitor
Runs detect-schema-drift.sh logic + information_schema snapshot comparison.
Outputs JSON result to stdout for cron job processing.
"""
import subprocess
import json
import os
import sys
from datetime import datetime, timezone, timedelta

WORKSPACE = "workspace_1l3urgumjmspnjxohclmfz6fx"
STATE_DIR = "/opt/jops/schema-snapshots"
CURRENT_FILE = os.path.join(STATE_DIR, "twenty_schema_current.json")
PREVIOUS_FILE = os.path.join(STATE_DIR, "twenty_schema_previous.json")

def get_db_password():
    # Primary: ephemeral file written by cron wrapper
    try:
        with open("/tmp/_dbpass.txt") as f:
            pw = f.read().strip()
            if pw:
                return pw
    except FileNotFoundError:
        pass
    # Fallback: read directly from Twenty's .env (same source as .sh wrapper)
    try:
        with open("/opt/twenty/.env") as f:
            for line in f:
                if line.startswith("PG_DATABASE_PASSWORD"):
                    return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    raise RuntimeError("Could not locate Twenty DB password in /tmp/_dbpass.txt or /opt/twenty/.env")

def run_sql(password, query):
    import subprocess
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    result = subprocess.run(
        ["docker", "exec", "-e", f"PGPASSWORD={password}", 
         "twenty-db-1", "psql", "-U", "twenty", "-d", "default", 
         "-t", "-A", "-F", "|", "-c", query],
        capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()

def get_schema_snapshot(password):
    """Get full schema of all custom tables as dict."""
    query = f"""
    SELECT table_name, column_name, data_type, is_nullable
    FROM information_schema.columns 
    WHERE table_schema = '{WORKSPACE}' 
        AND table_name LIKE '\\_%'
    ORDER BY table_name, ordinal_position;
    """
    output = run_sql(password, query)
    tables = {}
    for line in output.split('\n'):
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) < 4:
            continue
        table, col, dtype, nullable = parts[0], parts[1], parts[2], parts[3]
        if table not in tables:
            tables[table] = {}
        tables[table][col] = {"type": dtype, "nullable": nullable}
    return tables

def get_field_metadata(password):
    """Get all active custom fields from fieldMetadata."""
    query = """
    SELECT fm."name", fm."type", om."nameSingular"
    FROM core."fieldMetadata" fm
    JOIN core."objectMetadata" om ON fm."objectMetadataId" = om."id"
    WHERE fm."type" NOT IN ('UUID', 'TEXT', 'NUMBER', 'BOOLEAN', 'DATE_TIME', 'RELATION', 'ACTOR', 'SELECT', 'MULTI_SELECT', 'RICH_TEXT', 'CURRENCY', 'EMAIL', 'PHONE', 'URL', 'DATE')
      AND fm."isActive" = true
      AND om."isSystem" = false
    ORDER BY om."nameSingular", fm."name";
    """
    output = run_sql(password, query)
    fields = []
    for line in output.split('\n'):
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) >= 3:
            fields.append({"name": parts[0], "type": parts[1], "object": parts[2]})
    return fields

def detect_drift(password):
    """Run the existing detect-schema-drift.sh logic via SQL."""
    query = f"""
    SELECT DISTINCT ON (om."nameSingular")
        om."nameSingular" as table_name,
        fm."name" as field_name,
        fm."type" as field_type
    FROM core."fieldMetadata" fm
    JOIN core."objectMetadata" om ON fm."objectMetadataId" = om."id"
    WHERE fm."type" IN ('RELATION', 'ACTOR')
      AND fm."isActive" = true
      AND om."isSystem" = false
    ORDER BY om."nameSingular", fm."name";
    """
    output = run_sql(password, query)
    drift = []
    
    for line in output.split('\n'):
        if not line.strip():
            continue
        parts = line.split('|')
        if len(parts) < 3:
            continue
        table, field, ftype = parts[0], parts[1], parts[2]
        pg_table = f"_{table}"
        
        # Check if column exists
        if ftype == "RELATION":
            check_query = f"SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='{WORKSPACE}' AND table_name='{pg_table}' AND column_name='{field}Id';"
            result = run_sql(password, check_query)
            if result.strip() == "0":
                drift.append({"table": pg_table, "column": f"{field}Id", "type": "RELATION", "issue": "missing"})
        elif ftype == "ACTOR":
            for suffix in ["Source", "WorkspaceMemberId"]:
                col = f"{field}{suffix}"
                check_query = f"SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='{WORKSPACE}' AND table_name='{pg_table}' AND column_name='{col}';"
                result = run_sql(password, check_query)
                if result.strip() == "0":
                    drift.append({"table": pg_table, "column": col, "type": "ACTOR", "issue": "missing"})
    
    return drift

def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    
    # Get password
    password = get_db_password()
    
    # Get current schema
    current = get_schema_snapshot(password)
    
    # Load previous
    previous = {}
    if os.path.exists(CURRENT_FILE):
        with open(CURRENT_FILE) as f:
            previous = json.load(f)
        os.rename(CURRENT_FILE, PREVIOUS_FILE)
    
    # Save current
    with open(CURRENT_FILE, 'w') as f:
        json.dump(current, f, indent=2)
    
    # Detect changes
    changes = []
    
    if previous:
        all_tables = set(list(previous.keys()) + list(current.keys()))
        for table in sorted(all_tables):
            prev_cols = set(previous.get(table, {}).keys())
            curr_cols = set(current.get(table, {}).keys())
            
            # New columns
            for col in sorted(curr_cols - prev_cols):
                changes.append({
                    "type": "COLUMN_ADDED",
                    "table": table,
                    "column": col,
                    "details": current[table][col]
                })
            
            # Removed columns (unusual, flag it)
            for col in sorted(prev_cols - curr_cols):
                changes.append({
                    "type": "COLUMN_REMOVED",
                    "table": table,
                    "column": col,
                    "details": previous[table][col]
                })
            
            # Type changes
            for col in sorted(prev_cols & curr_cols):
                if previous[table][col]["type"] != current[table][col]["type"]:
                    changes.append({
                        "type": "COLUMN_TYPE_CHANGED",
                        "table": table,
                        "column": col,
                        "from": previous[table][col]["type"],
                        "to": current[table][col]["type"]
                    })
        
        # New tables
        for table in sorted(set(current.keys()) - set(previous.keys())):
            if table not in [c["table"] for c in changes if c["type"] == "COLUMN_ADDED"]:
                changes.append({"type": "TABLE_ADDED", "table": table})
    
    # Also run fieldMetadata drift check (filter known system patterns)
    SYSTEM_RELATIONS = {"attachmentsId", "assigneeId", "assignedAgentId", "createdByWorkspaceMemberId", "accountOwnerId"}
    fm_drift = detect_drift(password)
    for d in fm_drift:
        col_name = d["column"]
        # Skip known system fields that Twenty manages internally
        if col_name in SYSTEM_RELATIONS:
            continue
        changes.append({
            "type": "FIELD_METADATA_DRIFT",
            **d
        })
    
    # Output result
    ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    
    result = {
        "timestamp": ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "tables_scanned": len(current),
        "columns_scanned": sum(len(v) for v in current.values()),
        "changes_detected": len(changes),
        "changes": changes,
        "status": "CHANGED" if changes else "NO_CHANGE"
    }
    
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
