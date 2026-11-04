"""Reference batcher. Picks open orders and runs them through VaultManager_RunBatch.

Every submission is written to a journal before it is sent. If the outcome is
unknown, for example after a timeout or a restart, the journal entry is
reconciled from the completion stream before anything new is submitted.
"""
import argparse
import datetime as dt
import fcntl
import json
import logging
import os
from pathlib import Path
import random
import time
from urllib.parse import urlencode
import uuid

import websocket

from jsonapi import ApiError, JsonApi, exercise_cmd

LOG = logging.getLogger('batcher')
MANAGER_MODULE = 'CantonSolutions.Vault.Manager'
ROLES_MODULE = 'CantonSolutions.Vault.Roles'
NO_ARGS = {'context': {'values': {}}, 'meta': {'values': {}}}
HALTED = 'canton-solutions/vault-halted'
RETRYABLE = {'LOCAL_VERDICT_LOCKED_CONTRACTS', 'CONTRACT_NOT_FOUND'}
ORDER_KINDS = ('Deposit', 'Redeem', 'TopUp')
ID_KEYS = ('error_id', 'errorId', 'reason', 'errorCode', 'code')


def error_ids(error):
    """Structured error ids anywhere in an API error body. Messages are never parsed."""
    found = set()
    pending = [error.body]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ID_KEYS and isinstance(item, str):
                    found.add(item)
                elif isinstance(item, (dict, list)):
                    pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)
    return found


def parse_time(text):
    return dt.datetime.fromisoformat(text.replace('Z', '+00:00'))


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


class Unresolved(RuntimeError):
    """A journaled submission has no known outcome yet. Nothing new is sent."""


class Journal:
    """One durable record of the last submission."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def write(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        scratch = self.path.with_suffix('.tmp')
        with scratch.open('w') as out:
            json.dump(record, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        scratch.replace(self.path)
        folder = os.open(self.path.parent, os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)


class BatchRunner:
    def __init__(self, api, config, journal_path):
        if not config.get('batch_role'):
            raise ValueError('Set batch_role to the id of a batch role from the custodian')
        self.api = api
        self.config = config
        self.journal = Journal(journal_path)
        self.margin = float(config.get('deadline_margin', 60))
        self.max_orders = int(config.get('max_orders', 200))
        self.retries = int(config.get('retries', 3))
        if self.margin < 0 or self.max_orders < 1 or self.retries < 0:
            raise ValueError('deadline_margin, max_orders and retries must be sensible')
        self.readers = list(dict.fromkeys(
            [config['custodian'], config['batcher'], *config.get('read_as', [])]))
        # Orders a batch deferred, valid only while the ledger id stays the same.
        self.deferred_at = None
        self.deferred = set()
        self.unread_report = None

    def identity(self):
        return {'url': self.api.url, 'custodian': self.config['custodian'],
                'batcher': self.config['batcher'], 'user_id': self.config['user_id'],
                'manager': self.config.get('manager')}

    # Submission

    def reconcile(self, record):
        """Wait for the journaled command's completion and record it."""
        if record.get('identity') != self.identity():
            raise RuntimeError('The journal belongs to a different configuration')
        stream = self.api.socket('/v2/commands/completions', {
            'userId': self.config['user_id'], 'parties': [self.config['batcher']],
            'beginExclusive': record['offset']}, timeout=1)
        give_up = time.monotonic() + self.config.get('completion_timeout', 90)
        try:
            while time.monotonic() < give_up:
                try:
                    raw = stream.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    break
                row = json.loads(raw)
                if 'completionResponse' not in row:
                    raise Unresolved(f'Completion stream error: {row}')
                completion = row['completionResponse'].get('Completion', {}).get('value')
                if completion and completion.get('commandId') == record['command_id']:
                    record.update(pending=False, completion=completion)
                    self.journal.write(record)
                    if completion.get('status', {}).get('code', 0):
                        raise ApiError(409, completion)
                    return completion
        finally:
            stream.close()
        raise Unresolved(f"No outcome yet for {record['command_id']}. Keep the journal and retry.")

    def submit(self, command):
        record = {'pending': True, 'identity': self.identity(), 'offset': self.api.offset(),
                  'command_id': uuid.uuid4().hex, 'submission_id': uuid.uuid4().hex}
        record['body'] = {'userId': self.config['user_id'], 'commandId': record['command_id'],
                          'submissionId': record['submission_id'], 'actAs': [self.config['batcher']],
                          'readAs': self.readers, 'commands': [command]}
        self.journal.write(record)
        try:
            result = self.api.post('/v2/commands/submit-and-wait', record['body'])
        except ApiError as error:
            if error.status < 500 and error.status not in (408, 429):
                record.update(pending=False, rejection=error.body)
                self.journal.write(record)
                raise
        except (OSError, TimeoutError):
            pass
        else:
            record.update(pending=False, result=result)
            self.journal.write(record)
            return result
        return self.reconcile(record)

    # Reading the ledger

    def is_template(self, event, name):
        return event['templateId'].endswith(f':{MANAGER_MODULE}:{name}')

    def check_role(self, events, manager):
        role = next((e for e in events if e['contractId'] == self.config['batch_role']
                     and e['templateId'].endswith(f':{ROLES_MODULE}:VaultRole')), None)
        if role is None:
            raise RuntimeError('The batch role is not active or not visible')
        terms = role['createArgument']
        if (terms['custodian'] != self.config['custodian'] or terms['grantee'] != self.config['batcher']
                or terms['vault'] != manager or terms['permission'] != 'batch'):
            raise RuntimeError('The batch role does not match the custodian, batcher and manager')

    def view(self):
        """Return the manager, its ledger and the candidate orders, oldest first.
        A candidate is `(placedAt, cid, kind)`."""
        events = self.api.contracts(self.readers)
        managers = [e for e in events if self.is_template(e, 'VaultManager')
                    and e['createArgument']['terms']['custodian'] == self.config['custodian']
                    and self.config.get('manager') in (None, e['contractId'])]
        if len(managers) != 1:
            raise RuntimeError('Expected exactly one matching VaultManager. Set manager in the config.')
        manager = managers[0]
        self.check_role(events, manager['contractId'])
        ledgers = [e for e in events if self.is_template(e, 'VaultLedger')
                   and e['createArgument']['manager'] == manager['contractId']]
        if len(ledgers) != 1:
            raise RuntimeError('Expected exactly one active VaultLedger for the manager')
        cutoff = utc_now() + dt.timedelta(seconds=self.margin)
        live = {e['contractId'] for e in events}
        orders = []
        for event in events:
            kind = next((k for k in ORDER_KINDS if self.is_template(event, k + 'Order')), None)
            if kind is None:
                continue
            spec = event['createArgument']['spec']
            if spec['manager'] != manager['contractId'] or spec['custodian'] != self.config['custodian']:
                continue
            if parse_time(spec['deadline']) <= cutoff or spec['allocation'] not in live:
                continue
            orders.append((spec['placedAt'], event['contractId'], kind, spec['allocation']))
        # Two orders on one allocation cannot both execute. Keep the oldest.
        orders.sort()
        used, candidates = set(), []
        for placed_at, cid, kind, allocation in orders:
            if allocation not in used:
                used.add(allocation)
                candidates.append((placed_at, cid, kind))
        return manager, ledgers[0], candidates

    def note_deferred(self, result, command):
        """Read the batch report and remember what it deferred, tied to the input ledger."""
        self.unread_report = (result, command)
        query = urlencode({'parties': self.readers}, doseq=True)
        tree = self.api.post(f"/v2/updates/transaction-tree-by-id/{result['updateId']}?{query}")['transaction']
        exercise = command['ExerciseCommand']
        node = next(e['ExercisedTreeEvent']['value'] for e in tree['eventsById'].values()
                    if 'ExercisedTreeEvent' in e
                    and e['ExercisedTreeEvent']['value']['choice'] == 'VaultManager_RunBatch'
                    and e['ExercisedTreeEvent']['value']['contractId'] == exercise['contractId'])
        ledger = exercise['choiceArgument']['ledger']
        if self.deferred_at != ledger:
            self.deferred_at, self.deferred = ledger, set()
        self.deferred.update(ref['value'] for ref in node['exerciseResult']['deferred'])
        self.unread_report = None

    # One round

    def tick(self):
        """Do one round. Returns a status dict: executed, idle, halted or reconciled."""
        if self.unread_report is not None:
            self.note_deferred(*self.unread_report)
        record = self.journal.read()
        if record and record.get('pending'):
            try:
                result = self.reconcile(record)
            except ApiError as error:
                if HALTED in error_ids(error):
                    return {'status': 'halted'}
                if not self.retryable(error):
                    raise
            else:
                commands = record.get('body', {}).get('commands', [])
                if commands:
                    self.note_deferred(result, commands[0])
                return {'status': 'reconciled', 'result': result}
        chosen = None
        for attempt in range(self.retries + 1):
            manager, ledger, candidates = self.view()
            if ledger['createArgument'].get('halted', False):
                return {'status': 'halted'}
            if self.deferred_at != ledger['contractId']:
                self.deferred_at, self.deferred = ledger['contractId'], set()
            candidates = [c for c in candidates if c[1] not in self.deferred]
            if chosen is not None:
                candidates = [c for c in candidates if c[1] in chosen]
            candidates = candidates[:self.max_orders]
            if not candidates:
                return {'status': 'idle'}
            chosen = {c[1] for c in candidates}
            command = exercise_cmd(manager['templateId'], manager['contractId'], 'VaultManager_RunBatch', {
                'ledger': ledger['contractId'],
                'orders': [{'tag': kind + 'Ref', 'value': cid} for _, cid, kind in candidates],
                'underlyingArgs': self.config.get('underlying_args', NO_ARGS),
                'authorization': {'actor': self.config['batcher'], 'role': self.config['batch_role']}})
            try:
                result = self.submit(command)
            except ApiError as error:
                if HALTED in error_ids(error):
                    return {'status': 'halted'}
                if not self.retryable(error) or attempt == self.retries:
                    raise
                LOG.warning('Contention, reading the ledger again (attempt %s)', attempt + 1)
                time.sleep(random.uniform(.05, .5))
                continue
            self.note_deferred(result, command)
            return {'status': 'executed', 'orders': len(candidates), 'result': result}
        raise AssertionError('unreachable')

    @staticmethod
    def retryable(error):
        return bool(error_ids(error) & RETRYABLE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--once', action='store_true', help='run one round and exit')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = json.loads(Path(args.config).read_text())
    api = JsonApi(config['url'], os.environ.get('LEDGER_TOKEN'))
    Path(args.journal).parent.mkdir(parents=True, exist_ok=True)
    with open(args.journal + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = BatchRunner(api, config, args.journal)
        while True:
            try:
                LOG.info('%s', json.dumps(runner.tick()))
            except Unresolved as error:
                LOG.warning('%s', error)
                if args.once:
                    raise
            if args.once:
                break
            time.sleep(config.get('poll_seconds', 2))


if __name__ == '__main__':
    main()
