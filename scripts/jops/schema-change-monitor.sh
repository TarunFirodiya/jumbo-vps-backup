#!/bin/bash
# schema-change-monitor.sh
# Watches Twenty CRM for schema-level changes (new/renamed/dropped columns)
# Notifications sent via hermes CLI to Slack channel

set -euo pipefail

WORKSPACE="workspace_1l3urgumjmspnjxohclmfz6fx"
STATE_DIR="/opt/jops/schema-snapshots"
TIMESTAMP=$(date +%Y-%m-%d_%H:%M:%S)
CURRENT_FILE="$STATE_DIR/twenty_schema_current.sql"
PREVIOUS_FILE="$STATE_DIR/twenty_schema_previous.sql"
DIFF_FILE="$STATE_DIR/twenty_schema_diff_$TIMESTAMP.txt"

mkdir -p "$STATE_DIR"

# Backup previous snapshot
if [ -f "$CURRENT_FILE" ]; then
    cp "$CURRENT_FILE" "$PREVIOUS_FILE"
fi

PGPASSWORD=$(grep PG_DATABASE_PASSWORD /opt/twenty/.env | head -1 | cut -d= -f2)

# Dump full schema of all custom tables
docker exec -e "PGPASSWORD=$PGPASSWORD" twenty-db-1 psql -U twenty -d default -c "
SELECT 
    table_name,
    column_name,
    data_type,
    is_nullable,
    column_default
FROM information_schema.columns 
WHERE table_schema = '$WORKSPACE' 
    AND table_name LIKE '\_%'
ORDER BY table_name, ordinal_position;
" > "$CURRENT_FILE" 2>/dev/null

# Compare if previous exists
if [ -f "$PREVIOUS_FILE" ]; then
    DIFF=$(diff "$PREVIOUS_FILE" "$CURRENT_FILE" || true)
    if [ -n "$DIFF" ]; then
        echo "$DIFF" > "$DIFF_FILE"
        # Output change summary for notification
    else > /dev/null
        echo "NO_CHANGE"
    fi
else
    echo "BASELINE"
fi
