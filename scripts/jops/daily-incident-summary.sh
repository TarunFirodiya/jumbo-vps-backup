#!/bin/bash
# Daily incident/downtime summary for Jumbo VPS (read-only, safe on weekends)
STATE_DIR=/opt/jops/twenty-incidents
INC_LOG=$STATE_DIR/incident-log.jsonl
LAST=$(cat $STATE_DIR/.summary_last_epoch 2>/dev/null || echo 0)
NOW=$(date +%s)
python3 - "$INC_LOG" "$LAST" "$NOW" << 'PYEOF'
import json, sys, collections
log, last, now = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
ev = [json.loads(l) for l in open(log) if l.strip()]
new = [e for e in ev if e.get('epoch',0) > last]
if not new:
    print('OK: no incidents since last summary')
    sys.exit(0)
by = collections.Counter(e.get('event') for e in new)
rec = [e for e in new if e.get('event')=='auto_recovery']
fatal = sum(int(e.get('fatal_signatures',0)) for e in rec)
qt = sum(int(e.get('query_timeouts',0)) for e in rec)
down = sum(int(e.get('down_secs_approx',0)) for e in rec)
lines = [f"Incidents since last summary ({len(new)} events): {dict(by)}"]
for e in rec:
    lines.append(f"  {e['ts']}: auto_recovery fatal={e.get('fatal_signatures')} qtimeout={e.get('query_timeouts')} recovered={e.get('recovered')}")
lines.append(f"Totals: {len(rec)} crashes recovered | {down}s downtime | {fatal} fatal signatures | {qt} query timeouts")
print('\n'.join(lines))
PYEOF
echo $NOW > $STATE_DIR/.summary_last_epoch
