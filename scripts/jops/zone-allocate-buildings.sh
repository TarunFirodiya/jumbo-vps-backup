#!/bin/bash
# Zone Allocation — Building Zone Assignment
# Runs via cron every 30 min. Idempotent: only touches buildings with no zoneId.
# Uses ray-casting point-in-polygon against _zoneallocation.geoFence.

LOG_FILE="/opt/jops/zone-allocate-buildings.log"
LOCK_FILE="/opt/jops/zone-allocate-buildings.lock"
SCRIPT="/root/.twenty/jumbo-migration/scripts/zone-allocation/allocate_zones.py"

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

echo "$(date -Iseconds) START allocate_zones.py" >> "$LOG_FILE"
OUTPUT=$(python3 "$SCRIPT" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "$(date -Iseconds) ERROR (exit=$EXIT_CODE): $OUTPUT" >> "$LOG_FILE"
    rm -f "$LOCK_FILE"
    exit 1
fi

# Extract key stats
ALLOCATED=$(echo "$OUTPUT" | grep -oP 'Allocated:\s+\K\d+')
UNALLOCATED=$(echo "$OUTPUT" | grep -oP 'Unallocated:\s+\K\d+')
TOTAL_WITH_ZONE=$(echo "$OUTPUT" | grep -oP 'Total buildings with zone: \K\d+')

echo "$(date -Iseconds) OK allocated=${ALLOCATED:-?} unallocated=${UNALLOCATED:-?} total_with_zone=${TOTAL_WITH_ZONE:-?}" >> "$LOG_FILE"

# Keep log tail manageable
tail -200 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"

rm -f "$LOCK_FILE"
