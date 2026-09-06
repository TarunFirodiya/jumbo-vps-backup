#!/bin/bash
# Seller lead zone-agent allocator -- cron wrapper
# Runs /opt/jops/seller-agent-allocator.py --live, stays silent on success.
# Exits 0 even on empty result so cron stays quiet when nothing to do.

output=$(python3 /opt/jops/seller-agent-allocator.py --live 2>&1)
exit_code=$?
echo "$output"
exit $exit_code