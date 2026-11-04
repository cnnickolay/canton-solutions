# Cluster benchmark

Measures how fast the sharded batched vault and the single-ledger batched vault settle a backlog of orders on a real Canton network, not a single sandbox.

## Topology

| VM | Machine | Runs |
|---|---|---|
| `vault-sync` | n2-standard-4 (4 vCPU, 16 GB) | BFT sequencer and mediator, plus the benchmark driver |
| `vault-custodian` | c2d-standard-16 (16 vCPU, 64 GB) | Participant hosting the custodian, the batcher, the registrar and the sponsor |
| `vault-holders` | n2-standard-4 (4 vCPU, 16 GB) | Participant hosting the holders |

1. Canton open source 3.5.15. Every node stores its data in a local Postgres.
2. Holders place their orders from their own participant. They see the vault and token contracts through explicit disclosure, like real users of a remote vault.
3. Every batch is submitted on the custodian's participant and confirmed by both participants, because both custodian and holders sign parts of it.

## What is measured

1. Setup, not timed: fresh parties and token registries, a vault, holders funded through mint offers, every holder seeded with shares, then a top-up so the price is not one.
2. Backlog, not timed: `--orders` orders, half deposits of 1 USD and half redemptions of 0.5 vUSD, placed by all holders in parallel. Placement rate is recorded.
3. Timed: settling the whole backlog.
   1. Batched: one batcher runs batches of 200 back to back.
   2. Sharded: one batcher worker per shard, each running batches of 200 on its own shard at the same time. Orders are split over the shards by a hash of their contract id.
4. Then, for the sharded vault, one timed fold.
5. Every run ends with a balance check: the book must equal the real token holdings.
6. Every VM logs `vmstat` every 5 seconds, so CPU use can be lined up with each run's start and end times.

## Run it

The scripts drive `gcloud` through its Docker image. `GCLOUD_HOME` must point at a private folder with `config/` (a logged-in gcloud config) and `ssh/`.

```sh
export GCLOUD_HOME=/path/to/private/gcloud-home
dpm build --all
vault/cluster/gcp/up.sh         # about 10 minutes: VMs, Java, Postgres, Canton, network up
vault/cluster/gcp/run.sh 3      # batched and sharded K=1,2,4,8,16,32, three repeats each
vault/cluster/gcp/down.sh       # delete everything
```

1. `check.py` proves the network is a real cluster before benchmarking: separate participants, a remote party, and a transaction both participants confirm.
2. `gcp/resize.sh` gives a participant VM a new machine type and brings it back as a fresh participant.
3. `report.py` turns `runs.jsonl` and the `vmstat` logs into the tables in RESULTS.md.

## Parking

`down.sh` deletes the VMs and their disks, so nothing is left that costs money. Every VM is also created with a 6 hour limit after which GCP deletes it by itself. The project, the budget alert and these scripts stay, and `up.sh` rebuilds the whole network from scratch.

## Results

`collect.sh` copies `runs.jsonl`, the driver log and each VM's `vmstat` log into `results/<time>/`. The findings are written up in [RESULTS.md](RESULTS.md).
