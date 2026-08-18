#!/bin/bash
# Buyer Zone Assignment — Agent Assignment via Latest Enquiry/Visit
# Runs via cron. Idempotent: only touches buyers with no assignedAgentId.
# Logic: Buyer → latest enquiry → building → zone → workspaceMember (zone leader)
#        Fallback: Buyer → latest visit → property → building → zone → workspaceMember
#        Fallback: Buyer → latest visit → building → zone → workspaceMember

LOG_FILE="/opt/jops/zone-assign-buyers.log"
LOCK_FILE="/opt/jops/zone-assign-buyers.lock"
SCHEMA="workspace_1l3urgumjmspnjxohclmfz6fx"

# Prevent overlapping runs
if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE")
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "$(date -Iseconds) SKIP: another instance running (PID $LOCK_PID)" >> "$LOG_FILE"
        exit 0
    else
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"

echo "$(date -Iseconds) START zone-assign-buyers" >> "$LOG_FILE"

SQL="
SET search_path TO ${SCHEMA}, public;

-- Resolve zone for each buyer via their latest enquiry or visit
WITH buyer_zone_from_enquiry AS (
    SELECT DISTINCT ON (b.id)
        b.id as buyer_id,
        COALESCE(eb.\"zoneId\", pb.\"zoneId\") as zone_id
    FROM \"_buyer\" b
    JOIN \"_enquiry\" e ON e.\"buyerId\" = b.id AND e.\"deletedAt\" IS NULL
    LEFT JOIN \"_building\" eb ON e.\"buildingId\" = eb.id
    LEFT JOIN \"_property\" p ON e.\"propertyId\" = p.id
    LEFT JOIN \"_building\" pb ON p.\"buildingId\" = pb.id
    WHERE b.\"deletedAt\" IS NULL
      AND b.\"assignedAgentId\" IS NULL
      AND COALESCE(eb.\"zoneId\", pb.\"zoneId\") IS NOT NULL
    ORDER BY b.id, e.\"createdAt\" DESC
),
buyer_zone_from_visit AS (
    SELECT DISTINCT ON (b.id)
        b.id as buyer_id,
        COALESCE(vb.\"zoneId\", vpb.\"zoneId\") as zone_id
    FROM \"_buyer\" b
    JOIN \"_visit\" v ON v.\"buyerProfileId\" = b.id AND v.\"deletedAt\" IS NULL
    LEFT JOIN \"_building\" vb ON v.\"buildingId\" = vb.id
    LEFT JOIN \"_property\" vp ON v.\"propertyId\" = vp.id
    LEFT JOIN \"_building\" vpb ON vp.\"buildingId\" = vpb.id
    WHERE b.\"deletedAt\" IS NULL
      AND b.\"assignedAgentId\" IS NULL
      AND COALESCE(vb.\"zoneId\", vpb.\"zoneId\") IS NOT NULL
      -- Only consider visits for buyers NOT already resolved via enquiry
      AND b.id NOT IN (SELECT buyer_id FROM buyer_zone_from_enquiry)
    ORDER BY b.id, v.\"createdAt\" DESC
),
buyer_zone AS (
    SELECT buyer_id, zone_id FROM buyer_zone_from_enquiry
    UNION ALL
    SELECT buyer_id, zone_id FROM buyer_zone_from_visit
),
to_update AS (
    SELECT bz.buyer_id, bz.zone_id, wm.id as agent_id
    FROM buyer_zone bz
    JOIN \"workspaceMember\" wm ON wm.\"assignedZoneId\" = bz.zone_id
    WHERE wm.\"deletedAt\" IS NULL
)
UPDATE \"_buyer\" b
SET
    \"assignedAgentId\" = tu.agent_id,
    \"updatedAt\" = NOW()
FROM to_update tu
WHERE b.id = tu.buyer_id;
"

RESULT=$(docker exec twenty-db-1 psql -U twenty -d default -t -A -c "$SQL" 2>&1)
EXIT_CODE=$?
UPDATED=$(echo "$RESULT" | grep -oP 'UPDATE \K\d+' | tail -1)

if [ -z "$UPDATED" ] || [ "$EXIT_CODE" -ne 0 ]; then
    echo "$(date -Iseconds) ERROR (exit=$EXIT_CODE): $RESULT" >> "$LOG_FILE"
    rm -f "$LOCK_FILE"
    exit 1
fi

# Get distribution by zone
DIST=$(docker exec twenty-db-1 psql -U twenty -d default -t -A -c "
SET search_path TO ${SCHEMA}, public;
SELECT string_agg(za.name || ':' || cnt, ', ' ORDER BY cnt DESC)
FROM (
    SELECT wm.\"assignedZoneId\", COUNT(*) as cnt
    FROM \"_buyer\" b
    JOIN \"workspaceMember\" wm ON b.\"assignedAgentId\" = wm.id
    WHERE b.\"deletedAt\" IS NULL
    GROUP BY wm.\"assignedZoneId\"
) sub
JOIN \"_zoneallocation\" za ON za.id = sub.\"assignedZoneId\"
WHERE za.\"deletedAt\" IS NULL;
" 2>&1 | tail -1)

# Count remaining unassigned buyers (with enquiries/visits but no zone)
REMAINING=$(docker exec twenty-db-1 psql -U twenty -d default -t -A -c "
SET search_path TO ${SCHEMA}, public;
SELECT COUNT(DISTINCT b.id)
FROM \"_buyer\" b
LEFT JOIN \"_enquiry\" e ON e.\"buyerId\" = b.id AND e.\"deletedAt\" IS NULL
LEFT JOIN \"_visit\" v ON v.\"buyerProfileId\" = b.id AND v.\"deletedAt\" IS NULL
WHERE b.\"deletedAt\" IS NULL
  AND b.\"assignedAgentId\" IS NULL
  AND (e.id IS NOT NULL OR v.id IS NULL);
" 2>&1 | tail -1)

TOTAL_WITH=$(docker exec twenty-db-1 psql -U twenty -d default -t -A -c "
SET search_path TO ${SCHEMA}, public;
SELECT COUNT(*) FROM \"_buyer\" WHERE \"deletedAt\" IS NULL AND \"assignedAgentId\" IS NOT NULL;
" 2>&1 | tail -1)

echo "$(date -Iseconds) OK updated=${UPDATED} total_with_agent=${TOTAL_WITH} remaining_with_enquiries=${REMAINING} | ${DIST}" >> "$LOG_FILE"

# Keep log tail manageable
tail -200 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"

rm -f "$LOCK_FILE"
