#!/bin/bash
# JUM-700: ingest seller leads from #temp-growth into CRM (every 10 min)
# Writes detailed log to /opt/jops/temp_growth_ingest.log
set -o pipefail
cd /opt/jops

LOG=/opt/jops/temp_growth_ingest.log
echo "=== $(date -Iseconds) START ===" >> "$LOG"
python3 ingest_temp_growth.py --live --limit 50 >> "$LOG" 2>&1
RC=$?
echo "=== $(date -Iseconds) EXIT=$RC ===" >> "$LOG"
# Keep only last 2000 lines in the log
tail -2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit "$RC"
