#!/bin/bash
# Property Status Calculator cron wrapper
# Runs the Python script and logs output with rotation

SCRIPT_DIR="/opt/jops"
LOG_DIR="/var/log/jops"
LOCK_FILE="/tmp/property-status-calculator.lock"
MAX_LOG_SIZE=5242880  # 5MB

mkdir -p "$LOG_DIR"

# Prevent overlapping runs
if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE")
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "$(date): Already running (PID $LOCK_PID), skipping."
        exit 0
    else
        echo "$(date): Stale lock file, removing."
        rm -f "$LOCK_FILE"
    fi
fi

echo $$ > "$LOCK_FILE"

LOG_FILE="$LOG_DIR/property-status-$(date +%Y%m%d).log"
echo "$(date): Starting property status calculator..." >> "$LOG_FILE"

python3 -u "$SCRIPT_DIR/property-status-calculator.py" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "$(date): Finished with exit code $EXIT_CODE" >> "$LOG_FILE"

# Rotate log if too large
if [ -f "$LOG_FILE" ] && [ $(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null || echo 0) -gt $MAX_LOG_SIZE ]; then
    mv "$LOG_FILE" "$LOG_FILE.old"
fi

rm -f "$LOCK_FILE"
exit $EXIT_CODE
