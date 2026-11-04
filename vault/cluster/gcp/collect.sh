#!/usr/bin/env bash
# Copy results and CPU logs from the VMs into vault/cluster/results/<date>.
set -euo pipefail
source "$(dirname "$0")/env.sh"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$VAULT/cluster/results/$stamp"
gc compute scp "vault-sync:/opt/bench/runs.jsonl" "vault-sync:/opt/bench/bench.log" "/vault/cluster/results/$stamp/" --zone "$ZONE" --quiet
for vm in vault-sync vault-custodian vault-holders; do
  gc compute scp "$vm:/opt/bench/vmstat.log" "/vault/cluster/results/$stamp/vmstat-$vm.log" --zone "$ZONE" --quiet
done
echo "results in vault/cluster/results/$stamp"
