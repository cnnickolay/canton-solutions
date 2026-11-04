#!/usr/bin/env bash
# Run the benchmark matrix from the synchronizer VM and copy the results home.
# Usage: run.sh [repeats]   Each run uses fresh parties and a fresh vault.
set -euo pipefail
source "$(dirname "$0")/env.sh"
repeats="${1:-3}"
CUSTODIAN="http://$(internal_ip vault-custodian):5013"
HOLDERS="http://$(internal_ip vault-holders):5013"
ORDERS="${ORDERS:-4000}"
bench="cd /opt/bench && venv/bin/python vault/cluster/bench.py --custodian $CUSTODIAN --holders $HOLDERS --orders $ORDERS --out /opt/bench/runs.jsonl"
for r in $(seq 1 "$repeats"); do
  on vault-sync "$bench --mode batched --label batched-r$r >> /opt/bench/bench.log 2>&1"
  for k in 1 2 4 8 16 32; do
    on vault-sync "$bench --mode sharded --shards $k --label sharded-k$k-r$r >> /opt/bench/bench.log 2>&1"
  done
done
"$HERE/collect.sh"
