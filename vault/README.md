# Sharded batched vault

A tokenized vault on Canton, in Daml. It holds one CIP-0112 asset and issues CIP-0112 shares against it, following ERC-4626 for deposit and redeem and for rounding in the vault's favour. Holders place orders, and batchers execute many orders per transaction. The book is split into shards, so many batches can commit at the same time. The totals live on ledger, and the ledger computes every price from them.

## Why sharded batching

A vault on a ledger has one hard problem: every deposit and redemption changes the same totals. Put the totals in one contract and every operation has to consume and recreate it, so operations queue behind each other however much hardware is available.

1. **Batching** removes the per-operation write. Holders place orders that do not touch the book, and a batcher settles many of them in one transaction that writes the book once.
2. **Netting** makes each batch cheap. A batch settles all its orders with one CIP-0112 settlement per token registry, not two settlements per order.
3. **Sharding** removes the last single writer. The book is split into K shard contracts, and each batch consumes only its own shard, so K batchers commit in parallel.

What that gives you:

1. **Horizontal scalability.** Throughput grows with the number of shards until the hardware is busy, and the hardware can grow too. Shards share no hot contract, so adding shards adds parallel batches, and adding cores or participants gives them room to run. On a three-node network, 8 shards settled 3.9 times as many orders per second as one batched ledger, and the limit was a small participant's CPU, not the design.
2. **One price, computed on ledger.** Every shard prices from one shared, read-only price board that holds the totals. Nobody supplies a price from off ledger, and every order in a generation gets the same price whichever shard runs it.
3. **No contention on the hot path.** Placing an order never touches a shard, and batches on different shards never touch each other. Only a fold, which adds the shards back into the board and rebalances the reserves, touches all of them, and folds take under a second.
4. **Isolation of failures.** A batch that fails, for example because an order was withdrawn at the last moment, only holds up its own shard.
5. **Safety does not depend on routing.** The batcher decides which shard gets which order, but the ledger enforces price, minimums, deadlines and reserves on every shard, so bad routing costs speed, never money.

The repository also keeps the **single-ledger batched vault** (`packages/vault`): the same orders and roles with one book contract and one settlement per order. It is simpler, and it is the baseline the benchmark compares against.

## Documents

1. [docs/sharding.md](docs/sharding.md): the sharded vault, its rules and sequence diagrams.
2. [docs/flows.md](docs/flows.md): orders, roles and flows, with the single-ledger batched vault as the reference.
3. [cluster/RESULTS.md](cluster/RESULTS.md): the three-node benchmark, method and raw data.

## Layout

```text
packages/token/           a small CIP-0112 token, used for the shares and as the test underlying
packages/vault-math/      price arithmetic with exact rounding
packages/vault-sharded/   the sharded batched vault: ShardedManager, PriceBoard, Shard, netted batches and folds
packages/vault/           the single-ledger batched vault, plus the roles, settlement helpers and errors both use
test/vault-sharded-test/  Daml Script tests for the sharded batched vault
test/vault-test/          Daml Script tests for the single-ledger vault and the token
batcher/                  reference batcher service for the single-ledger vault, its unit tests and a sandbox run
demo/                     local dashboard, console and checks for the single-ledger vault
cluster/                  three-node Canton benchmark: configs, GCP scripts, driver and results
dars/                     vendored Splice token standard DARs
docs/                     design and flows
```

## Build and test

Daml SDK 3.5.8, Daml-LF 2.1. Run from this folder.

```sh
dpm build --all
DAML_PACKAGE=test/vault-sharded-test dpm test
DAML_PACKAGE=test/vault-test dpm test
```

The sharded suite has 17 tests: parallel shards, folds and reserve splits, top-ups, large redemptions, deferrals, leg id rules, halting and roles. The single-ledger suite has 32: rounding, batch pricing against a sequential model, halting, roles and handovers, the token itself, and every rejection the vault makes.

```sh
python3 -m venv batcher/.venv
batcher/.venv/bin/pip install -r batcher/requirements.txt
(cd batcher && .venv/bin/python -m unittest test_runner)
```

For the sandbox run and the demo see [batcher/README.md](batcher/README.md) and [demo/README.md](demo/README.md). For the cluster benchmark see [cluster/README.md](cluster/README.md).

## Measured

Three-node Canton network on Google Cloud, 4,000 orders per run, batches of 200, mean of three runs:

| Version | Orders/s | vs single ledger |
|---|---:|---:|
| Single-ledger batched vault | 52.9 | 1.00 |
| Sharded, 1 shard | 70.1 | 1.33 |
| Sharded, 4 shards | 189.5 | 3.58 |
| Sharded, 8 shards | 206.7 | 3.91 |

Details, batch times and CPU per node are in [cluster/RESULTS.md](cluster/RESULTS.md).

## Dependencies

Only the Splice Token Standard V2 API packages and their utilities, Apache-2.0. The token, roles and halt switch are part of this repo. Sources and hashes are in [dars/README.md](dars/README.md).

## Status

Not audited. The custodian party holds the reserve and signs the vault contracts, so it is trusted. Run it as a party controlled by several owners for real use.
