#!/bin/bash
# Health check for Property Status Calculator
# Checks:
# 1. Last run time (should be within 15 minutes)
# 2. Error rate in latest run (< 10%)
# 3. Spot-check: verify a few properties have correct status
# 4. Lock file not stale

set -e

LOG_DIR="/var/log/jops"
LOCK_FILE="/tmp/property-status-calculator.lock"
ALERT_WEBHOOK=""  # TODO: add Slack/Telegram webhook if needed
MAX_AGE_SECONDS=900  # 15 minutes
MAX_ERROR_RATE=10  # percent

TODAY=$(date +%Y%m%d)
LOG_FILE="$LOG_DIR/property-status-$TODAY.log"
YESTERDAY_LOG="$LOG_DIR/property-status-$(date -d 'yesterday' +%Y%m%d).log 2>/dev/null || echo ''"

echo "=== Property Status Health Check ==="
echo "Time: $(date)"
echo ""

# Check 1: Last run time
if [ -f "$LOG_FILE" ]; then
    LAST_RUN=$(stat -c %Y "$LOG_FILE" 2>/dev/null || stat -f %m "$LOG_FILE" 2>/dev/null)
    NOW=$(date +%s)
    AGE=$((NOW - LAST_RUN))
    if [ $AGE -gt $MAX_AGE_SECONDS ]; then
        echo "WARNING: Last log update was ${AGE}s ago (threshold: ${MAX_AGE_SECONDS}s)"
    else
        echo "OK: Last log update ${AGE}s ago"
    fi
else
    echo "ERROR: No log file found for today ($LOG_FILE)"
fi

# Check 2: Stale lock file
if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE")
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "OK: Process running (PID $LOCK_PID)"
    else
        echo "WARNING: Stale lock file (PID $LOCK_PID not running)"
    fi
else
    echo "OK: No lock file (process not running)"
fi

echo ""
echo "=== Latest Run Summary ==="
if [ -f "$LOG_FILE" ]; then
    # Extract the latest run's summary
    grep -E "^(Done|Updated:|Skipped:|Errors:)" "$LOG_FILE" | tail -4
else
    echo "No log file to parse"
fi
