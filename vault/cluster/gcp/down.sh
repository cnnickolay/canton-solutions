#!/usr/bin/env bash
# Delete the VMs and their disks. Nothing is left running or stored, so nothing costs money.
# The project, the budget alert and these scripts stay, so up.sh brings it all back.
set -euo pipefail
source "$(dirname "$0")/env.sh"
names=()
for node in "${NODES[@]}"; do names+=("${node%%:*}"); done
gc compute instances delete "${names[@]}" --zone "$ZONE" --quiet || true
gc compute instances list --format='value(name)'
gc compute disks list --format='value(name)'
echo "down. Anything listed above still exists."
