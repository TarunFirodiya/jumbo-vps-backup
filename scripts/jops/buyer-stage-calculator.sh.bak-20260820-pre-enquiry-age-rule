#!/bin/bash
# Buyer Stage Calculator - SQL-based
# Replaces the Python GraphQL version to eliminate ~1,680 API calls/day
# 
# Business logic (same as buyer_stage_calculator.py):
#   - Use the most recent event (latest enquiry or visit) as the signal:
#       - If latest event is a visit: <=30d ACTIVE_VISITOR, <=90d AT_RISK_VISITOR, else INACTIVE
#       - If latest event is an enquiry: <=7d FRESH_LEAD, buyer age 8-30d AT_RISK_LEAD, else INACTIVE
#       - Last enquiry <= 7 days ago → FRESH_LEAD
#       - Buyer age 8-30 days        → AT_RISK_LEAD
#       - Otherwise                  → INACTIVE
#   - If buyer has no visits and no enquiries → INACTIVE
#
# Only updates rows where the stage actually changed.

LOG_FILE="/opt/jops/buyer-stage-calculator.log"
LOCK_FILE="/opt/jops/buyer-stage-calculator.lock"
SCHEMA="workspace_1l3urgumjmspnjxohclmfz6fx"
PGPASS_FILE="/root/.twenty/.pgpass"

# Prevent overlapping runs
if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE")
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        MSG="$(date -Iseconds) SKIP: another instance running (PID $LOCK_PID)"
        echo "$MSG" >> "$LOG_FILE"
        echo "$MSG"
        exit 0
    else
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"

# Read password from .env
DBPASS=$(grep '^PG_DATABASE_PASSWORD=' /opt/twenty/.env | head -1 | sed 's/^PG_DATABASE_PASSWORD=//')

SQL="
SET search_path TO ${SCHEMA}, public;

WITH buyer_data AS (
    SELECT
        b.id,
        b.\"leadStage\" AS old_stage,
        b.\"createdAt\" AS buyer_created_at,
        (SELECT MAX(e.\"createdAt\") FROM _enquiry e WHERE e.\"buyerId\" = b.id AND e.\"deletedAt\" IS NULL) AS latest_enquiry_at,
        (SELECT MAX(v.\"createdAt\") FROM _visit v WHERE v.\"buyerProfileId\" = b.id AND v.\"deletedAt\" IS NULL) AS latest_visit_at
    FROM _buyer b
    WHERE b.\"deletedAt\" IS NULL
),
computed AS (
    SELECT
        id,
        old_stage,
        CASE
            WHEN latest_visit_at IS NOT NULL
                 AND (latest_enquiry_at IS NULL OR latest_visit_at >= latest_enquiry_at) THEN
                CASE
                    WHEN EXTRACT(EPOCH FROM (NOW() - latest_visit_at)) / 86400 <= 30 THEN 'ACTIVE_VISITOR'::\"_buyer_leadStage_enum\"
                    WHEN EXTRACT(EPOCH FROM (NOW() - latest_visit_at)) / 86400 <= 90 THEN 'AT_RISK_VISITOR'::\"_buyer_leadStage_enum\"
                    ELSE 'INACTIVE'::\"_buyer_leadStage_enum\"
                END
            WHEN latest_enquiry_at IS NOT NULL THEN
                CASE
                    WHEN EXTRACT(EPOCH FROM (NOW() - latest_enquiry_at)) / 86400 <= 7 THEN 'FRESH_LEAD'::\"_buyer_leadStage_enum\"
                    WHEN EXTRACT(EPOCH FROM (NOW() - buyer_created_at)) / 86400 BETWEEN 8 AND 30 THEN 'AT_RISK_LEAD'::\"_buyer_leadStage_enum\"
                    ELSE 'INACTIVE'::\"_buyer_leadStage_enum\"
                END
            ELSE 'INACTIVE'::\"_buyer_leadStage_enum\"
        END AS new_stage
    FROM buyer_data
),
to_update AS (
    SELECT id, new_stage
    FROM computed
    WHERE old_stage IS NULL OR old_stage != new_stage
)
UPDATE _buyer b
SET
    \"leadStage\" = tu.new_stage,
    \"updatedAt\" = NOW()
FROM to_update tu
WHERE b.id = tu.id;
"

RESULT=$(PGPASSWORD="${DBPASS}" docker exec -i twenty-db-1 psql -U twenty -d default -t -A -c "$SQL" 2>&1)
EXIT_CODE=$?
UPDATED=$(echo "$RESULT" | grep -oP 'UPDATE \K\d+' | tail -1)

if [ -z "$UPDATED" ]; then
    MSG="$(date -Iseconds) ERROR: $RESULT"
    echo "$MSG" >> "$LOG_FILE"
    echo "$MSG"
    rm -f "$LOCK_FILE"
    exit 1
fi

# Get current stage distribution
DIST=$(PGPASSWORD="${DBPASS}" docker exec -i twenty-db-1 psql -U twenty -d default -t -A -c "
SET search_path TO ${SCHEMA}, public;
SELECT string_agg(\"leadStage\"::text || ':' || cnt, ', ' ORDER BY cnt DESC)
FROM (
    SELECT \"leadStage\"::text, COUNT(*) as cnt
    FROM _buyer WHERE \"deletedAt\" IS NULL
    GROUP BY \"leadStage\"
) sub;
" 2>&1 | tail -1)

# Get total buyer count
TOTAL=$(PGPASSWORD="${DBPASS}" docker exec -i twenty-db-1 psql -U twenty -d default -t -A -c "
SET search_path TO ${SCHEMA}, public;
SELECT COUNT(*) FROM _buyer WHERE \"deletedAt\" IS NULL;
" 2>&1 | tail -1)

MSG="$(date -Iseconds) OK total=${TOTAL} updated=${UPDATED} | ${DIST}"
echo "$MSG" >> "$LOG_FILE"
echo "$MSG"

# Cleanup lock
rm -f "$LOCK_FILE"
