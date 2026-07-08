#!/bin/bash
# Wrapper: run the Callyzer->Drive->CRM recording sync.
# cd /tmp so gws --upload (which restricts to cwd) can read staged files.
cd /tmp
exec python3 /opt/jops/callyzer_recording_sync.py "$@"
