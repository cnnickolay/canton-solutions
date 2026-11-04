# Batcher

Reference batcher for the JSON Ledger API v2. It reads open orders, picks up to 200 that are not within 60 seconds of their deadline and whose allocation is still active, and runs them through `VaultManager_RunBatch`. The ledger decides the order and the prices.

## Run the tests

```sh
python3 -m venv batcher/.venv
batcher/.venv/bin/pip install -r batcher/requirements.txt
cd batcher && .venv/bin/python -m unittest test_runner
```

## Run against a ledger

Write a config file.

```json
{
  "url": "http://127.0.0.1:18703",
  "user_id": "vault-batcher",
  "custodian": "<custodian party>",
  "batcher": "<batcher party>",
  "manager": "<VaultManager contract id>",
  "batch_role": "<batch VaultRole contract id>",
  "read_as": ["<underlying admin party>"],
  "deadline_margin": 60,
  "max_orders": 200,
  "poll_seconds": 2,
  "retries": 3,
  "completion_timeout": 90
}
```

```sh
batcher/.venv/bin/python batcher/runner.py --config batcher.json --journal batcher.journal.json
```

1. Set `LEDGER_TOKEN` if the ledger needs auth. The user needs `actAs` for the batcher and `readAs` for the custodian and every `read_as` party.
2. The batch role must be active, given by the custodian to the batcher, and name `batch` on this manager. A wrong role stops the batcher before it submits anything.
3. Each submission is saved to the journal before it is sent. An unknown outcome is reconciled from the completion stream first, also after a restart. Never delete a journal that still has a pending entry.
4. Orders a batch deferred are skipped until the ledger changes. This memory is in process only.
5. On `LOCAL_VERDICT_LOCKED_CONTRACTS` or `CONTRACT_NOT_FOUND` it reads the ledger again and retries with the orders still open. Any other error stops it.
6. While the vault is halted it waits.

## Sandbox run

`sandbox_run.py` runs the whole life cycle against a local sandbox: three deposits in capped batches with a lost response recovered, a role handover, a redemption and top-up across a halt, an order left alone near its deadline, and a deferred zero-share deposit. It checks after every batch that the book equals the real tokens.

```sh
dpm build --all
dpm sandbox --json-api-port 18703 --dar packages/vault/.daml/dist/canton-vault-0.1.0.dar
batcher/.venv/bin/python batcher/sandbox_run.py --url http://127.0.0.1:18703
```
