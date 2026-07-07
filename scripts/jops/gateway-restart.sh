#!/bin/bash
# One-shot gateway restart — runs outside the gateway process
sleep 2
systemctl --user restart hermes-gateway
echo "Gateway restart triggered"