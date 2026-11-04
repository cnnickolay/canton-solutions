#!/usr/bin/env bash
# Create the three VMs, install everything and start the Canton network.
# Every VM deletes itself after 6 hours even if down.sh is never run.
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$GCLOUD_HOME/ssh"
export TRANSFER_DIR="$GCLOUD_HOME/transfer"; rm -rf "$TRANSFER_DIR"; mkdir -p "$TRANSFER_DIR"
echo "packing Canton and the vault"
tar -C "$CANTON_HOME" -czf "$TRANSFER_DIR/canton.tgz" bin lib
(cd "$VAULT/.." && tar --exclude='.daml/build' --exclude='.venv' --exclude='__pycache__' --exclude='bench' \
  -czf "$TRANSFER_DIR/vault.tgz" vault)
cp "$HERE/provision.sh" "$TRANSFER_DIR/"
create_all() {  # create every VM in zone $1, or none
  local zone="$1" ok=1
  for node in "${NODES[@]}"; do
    name="${node%%:*}"; type="${node##*:}"
    gc compute instances create "$name" --zone "$zone" --machine-type "$type" \
      --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
      --boot-disk-size 50GB --boot-disk-type pd-balanced \
      --max-run-duration 6h --instance-termination-action DELETE \
      --metadata-from-file startup-script=/transfer/provision.sh --quiet > "$TRANSFER_DIR/create-$name.log" 2>&1 &
  done
  wait
  for node in "${NODES[@]}"; do
    name="${node%%:*}"
    grep -q "ERROR" "$TRANSFER_DIR/create-$name.log" && { ok=0; echo "$name: $(grep -o 'message: [^.]*' "$TRANSFER_DIR/create-$name.log" | head -1)"; }
  done
  if [ "$ok" = 0 ]; then
    for node in "${NODES[@]}"; do gc compute instances delete "${node%%:*}" --zone "$zone" --quiet > /dev/null 2>&1 || true; done
    return 1
  fi
}
ZONE=""
for custodian_type in ${CUSTODIAN_TYPES:-c2d-standard-16 n2d-standard-16 n2-standard-16}; do
  NODES=("vault-sync:n2-standard-4" "vault-custodian:$custodian_type" "vault-holders:n2-standard-4")
  for zone in "${ZONES[@]}"; do
    echo "trying $zone with a $custodian_type custodian"
    if create_all "$zone"; then ZONE="$zone"; break 2; fi
  done
done
[ -n "$ZONE" ] || { echo "no zone had capacity for all VMs" >&2; exit 1; }
echo "$ZONE" > "$GCLOUD_HOME/zone"
echo "created in $ZONE, custodian on $custodian_type"
echo "waiting for provisioning"
for node in "${NODES[@]}"; do
  name="${node%%:*}"
  until on "$name" "test -f /opt/bench/provisioned" 2>/dev/null; do sleep 15; done
  echo "$name provisioned"
done
for node in "${NODES[@]}"; do
  name="${node%%:*}"
  ( gc compute scp /transfer/canton.tgz /transfer/vault.tgz "$name":/opt/bench/ --zone "$ZONE" --quiet \
    && on "$name" "cd /opt/bench && tar xzf canton.tgz && tar xzf vault.tgz" && echo "$name loaded" ) &
done
wait
SYNC_IP="$(internal_ip vault-sync)"
# Each node runs as a transient systemd unit, so the ssh session returns at once.
node() {  # node <vm> <role> <heap>
  on "$1" "sudo systemd-run --unit=canton-node --property=LimitNOFILE=65536 --working-directory=/opt/bench \
    --setenv=CANTON=/opt/bench/bin/canton --setenv=LOG_DIR=/opt/bench --setenv=SYNC_HOST=$SYNC_IP --setenv=HEAP=$3 \
    bash -c 'vault/cluster/canton/run-node.sh $2 $STORAGE > /opt/bench/node.out 2>&1'"
}
node vault-sync sync 8g
node vault-custodian custodian 40g
node vault-holders holders 8g
for vm in vault-custodian vault-holders; do
  until on "$vm" "grep -q 'PARTICIPANT READY' /opt/bench/node.out" 2>/dev/null; do sleep 10; done
  echo "$vm connected to the synchronizer"
done
on vault-sync "cd /opt/bench && python3 -m venv venv && venv/bin/pip -q install websocket-client==1.9.0"
rm -rf "$TRANSFER_DIR"
echo "cluster up. Next: gcp/run.sh"
