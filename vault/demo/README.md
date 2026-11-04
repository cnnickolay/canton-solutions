# Demo

A local dashboard for the vault. It starts a fresh sandbox, sets up the vault with two real CIP-0112 token registries, funds Alice, Bob, Carol and the sponsor with 1,000 USD each, and shows the book, wallets, open orders, batches and a readable ledger feed.

```sh
./demo/start.sh
```

Run it from the `vault` folder, then open <http://127.0.0.1:8088>. It needs Python 3.10 or later, `dpm` and Linux `setsid`. Ports 18801 to 18806 and 8088 must be free (`DEMO_LEDGER_PORT` and `DEMO_PORT` move them). Ctrl-C stops the dashboard and its sandbox. Every start is a fresh vault.

## What you can do

1. Place deposit and redeem orders with an off-ledger estimate, a dust deposit, or a sponsor top-up.
2. Run one batch, or start the batcher so it runs every two seconds.
3. Halt and resume, revoke roles, and hand the batcher or warden role to a new party.
4. Donate USD straight to the custodian to see that the price does not move.
5. Withdraw an expired order.
6. Add holders one at a time or 5 or 20 at once, and start synthetic load up to five orders a second.
7. Check balances: the reserve must equal managed assets and all share holdings, locked or not, must equal issued shares.

Every action is journaled before it is sent. If a response is lost, the outcome is read from the completion stream and nothing is sent twice. A party with an unresolved action is blocked until Reconcile finds the outcome.

## Checks

```sh
batcher/.venv/bin/python -m unittest discover -s demo
batcher/.venv/bin/python demo/check_live.py      # against a freshly started demo
batcher/.venv/bin/python demo/check_recovery.py  # against the demo ledger
```

`check_live.py` drives the dashboard end to end: deposits, top-up, dust, donation, halt, redeem, expiry, both role handovers, 20 new holders depositing in one batch, and load with the batcher running. `check_recovery.py` loses the response of one real command and checks it was submitted once.

## Console

`demo/console.py` reads a running demo and never submits anything.

```sh
batcher/.venv/bin/python demo/console.py price
batcher/.venv/bin/python demo/console.py acs Alice
batcher/.venv/bin/python demo/console.py tx last-batch
batcher/.venv/bin/python demo/console.py tx last-batch --full
```
