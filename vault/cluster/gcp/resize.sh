#!/usr/bin/env bash
# Give one participant VM a new machine type and bring its node back as a fresh participant.
# Usage: resize.sh <vm> <machine-type> <role> <heap>, for example resize.sh vault-holders n2-standard-8 holders 16g
# The participant database is reset, so it rejoins with a new identity. Every benchmark run
# allocates fresh parties, so nothing is lost.
set -euo pipefail
source "$(dirname "$0")/env.sh"
vm="$1"; type="$2"; role="$3"; heap="$4"
on "$vm" "sudo systemctl stop canton-node 2>/dev/null; true"
# The boot script runs again on start, so give the VM the current, rerunnable one.
gc compute instances add-metadata "$vm" --zone "$ZONE" --metadata-from-file startup-script=/vault/cluster/gcp/provision.sh --quiet
gc compute instances stop "$vm" --zone "$ZONE" --quiet
gc compute instances set-machine-type "$vm" --zone "$ZONE" --machine-type "$type" --quiet
gc compute instances start "$vm" --zone "$ZONE" --quiet
until on "$vm" "systemctl is-active --quiet postgresql && test -f /opt/bench/provisioned" 2>/dev/null; do sleep 10; done
on "$vm" "sudo -u postgres dropdb participant && sudo -u postgres createdb -O canton participant && rm -f /opt/bench/node.out"
SYNC_IP="$(internal_ip vault-sync)"
on "$vm" "sudo systemd-run --unit=canton-node --property=LimitNOFILE=65536 --working-directory=/opt/bench \
  --setenv=CANTON=/opt/bench/bin/canton --setenv=LOG_DIR=/opt/bench --setenv=SYNC_HOST=$SYNC_IP --setenv=HEAP=$heap \
  bash -c 'vault/cluster/canton/run-node.sh $role ${STORAGE} > /opt/bench/node.out 2>&1'"
until on "$vm" "grep -q 'PARTICIPANT READY' /opt/bench/node.out" 2>/dev/null; do sleep 10; done
echo "$vm is now $type and connected"
