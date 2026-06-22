#!/bin/bash
# Zone Allocation — Enquiry Agent Assignment
# Runs via cron every 10 min. Idempotent: only touches enquiries with no assignedAgentId.
# Chain: Enquiry → Building → Zone → workspaceMember (via assignedZoneId)

LOG_FILE="/opt/jops/zone-assign-enquiries.log"
LOCK_FILE="/opt/jops/zone-assign-enquiries.lock"
SCRIPT="/root/.twenty/jumbo-migration/scripts/zone-allocation/assign_enquiries.py"

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

echo "$(date -Iseconds) START assign_enquiries.py" >> "$LOG_FILE"
OUTPUT=$(python3 "$SCRIPT" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "$(date -Iseconds) ERROR (exit=$EXIT_CODE): $OUTPUT" >> "$LOG_FILE"
    rm -f "$LOCK_FILE"
    exit 1
fi

# Extract key stats
ASSIGNED=$(echo "$OUTPUT" | grep -oP 'Assignments:\s+\K\d+' | head -1)
SKIPPED=$(echo "$OUTPUT" | grep -oP 'Skipped:\s+\K\d+' | head -1)
TOTAL_WITH=$(echo "$OUTPUT" | grep -oP 'Total enquiries with agent now: \K\d+')

echo "$(date -Iseconds) OK assigned=${ASSIGNED:-?} skipped=${SKIPPED:-?} total_with_agent=${TOTAL_WITH:-?}" >> "$LOG_FILE"

# Keep log tail manageable
tail -200 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE"

rm -f "$LOCK_FILE"
