#!/bin/bash
# Property Status Calculator — SQL-based
# Runs every 10 min via cron. Only updates rows that actually changed.
# New-process cutoff: serialNumber >= 2502. Properties below the cutoff are
# intentionally left untouched by the new proposal gate.
# Priority for accepted proposals: Sold > Offboarded > On Hold > Live > Inspection Pending > Catalogue Pending

LOG_FILE="/opt/jops/property-status-calculator.log"
LOCK_FILE="/opt/jops/property-status-calculator.lock"
SCHEMA="workspace_1l3urgumjmspnjxohclmfz6fx"

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

SQL="
SET search_path TO ${SCHEMA}, public;

WITH computed AS (
    SELECT
        p.id,
        CASE
            WHEN p."propertyStatus" = 'DRAFT'::"_property_propertyStatus_enum" THEN 'DRAFT'::"_property_propertyStatus_enum"  -- draft gate owns promotion (draft_promotion.py)
            WHEN p."propertyStatus" IS NULL THEN 'DRAFT'::"_property_propertyStatus_enum"  -- new listings from any creator start as DRAFT (Tarun, Aug 30 2026)
            WHEN p."serialNumber" >= 2502
                 AND p.\"proposalAcceptedOn\" IS NULL
            THEN 'PROPOSAL_SENT'::\"_property_propertyStatus_enum\"
            WHEN EXISTS (
                SELECT 1 FROM opportunity o
                WHERE o.\"propertyNewId\" = p.id
                AND o."stage"::text IN ('TOKEN_PAID','TERM_SHEET_SIGNED','AFS_MOU_SIGNED','SALE_DEED_REGISTERED_AA_SIGNED')
                AND o.\"deletedAt\" IS NULL
            ) THEN 'SOLD'::\"_property_propertyStatus_enum\"
            WHEN p.\"offboarding\" = true THEN 'OFFBOARDED'::\"_property_propertyStatus_enum\"
            WHEN p.\"onHold\" = true THEN 'ON_HOLD'::\"_property_propertyStatus_enum\"
            WHEN p.\"jumboUrl\" IS NOT NULL AND p.\"jumboUrl\" != '' THEN 'LIVE'::\"_property_propertyStatus_enum\"
            WHEN NOT EXISTS (
                SELECT 1 FROM \"_propertyInspection\" pi
                WHERE pi.\"propertyId\" = p.id
                AND pi.\"deletedAt\" IS NULL
            ) THEN 'INSPECTION_PENDING'::\"_property_propertyStatus_enum\"
            ELSE 'CATALOGUE_PENDING'::\"_property_propertyStatus_enum\"
        END AS new_status,
        p.\"propertyStatus\" AS old_status
    FROM \"_property\" p
    WHERE p.\"deletedAt\" IS NULL
),
to_update AS (
    SELECT id, new_status
    FROM computed
    WHERE old_status IS NULL OR old_status != new_status
)
UPDATE \"_property\" p
SET 
    \"propertyStatus\" = tu.new_status,
    \"updatedAt\" = NOW()
FROM to_update tu
WHERE p.id = tu.id;
"

RESULT=$(docker exec twenty-db-1 psql -U twenty -d default -t -A -c "$SQL" 2>&1)
EXIT_CODE=$?
UPDATED=$(echo "$RESULT" | grep -oP 'UPDATE \K\d+' | tail -1)

if [ -z "$UPDATED" ]; then
    MSG="$(date -Iseconds) ERROR: $RESULT"
    echo "$MSG" >> "$LOG_FILE"
    echo "$MSG"
    rm -f "$LOCK_FILE"
    exit 1
fi

# Get current distribution
DIST=$(docker exec twenty-db-1 psql -U twenty -d default -t -A -c "
SET search_path TO ${SCHEMA}, public;
SELECT string_agg(\"propertyStatus\"::text || ':' || cnt, ', ' ORDER BY cnt DESC)
FROM (
    SELECT \"propertyStatus\"::text, COUNT(*) as cnt
    FROM \"_property\" WHERE \"deletedAt\" IS NULL
    GROUP BY \"propertyStatus\"
) sub;
" 2>&1 | tail -1)

MSG="$(date -Iseconds) OK updated=${UPDATED} | ${DIST}"
echo "$MSG" >> "$LOG_FILE"
echo "$MSG"

# Cleanup lock
rm -f "$LOCK_FILE"
