"""The demo's vault on a local sandbox, and every action the dashboard can take."""
from decimal import Decimal
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlencode
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'batcher'))
import websocket  # noqa: E402
from jsonapi import ApiError, JsonApi, exercise_cmd  # noqa: E402,F401
from runner import NO_ARGS, BatchRunner, Unresolved  # noqa: E402
from sandbox_run import Sandbox, account, iso  # noqa: E402

TICK = Decimal('0.0000000001')
ORDER_TEMPLATES = ('DepositOrder', 'RedeemOrder', 'TopUpOrder')


def amount_text(value):
    """Validate a non-negative amount with at most ten decimals."""
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != number.quantize(TICK):
        raise ValueError('Amounts must be zero or more, with at most ten decimals')
    return format(number, 'f')


class DemoVault(Sandbox):
    """A sandbox vault whose actions are journaled, so a lost response is never resent."""

    def __init__(self, api, folder, emit):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.emit = emit
        self.names = {}
        self.locks = {}
        self.unresolved = {}
        self.names_lock = threading.RLock()
        self.batch_lock = threading.RLock()
        self.autorun = False
        self.loading = False
        self.load_settings = {}
        self.start_offset = api.offset()
        super().__init__(api)
        labels = ['Custodian', 'Registrar', 'Batcher 1', 'Sponsor', 'Warden 1', 'Alice', 'Bob', 'Carol']
        for party, label in zip([self.custodian, self.registrar, self.batcher, self.sponsor,
                                 self.warden, *self.holders], labels):
            self.names[party] = label
        self.config = {'user_id': 'demo', 'custodian': self.custodian, 'batcher': self.batcher,
                       'manager': self.manager, 'batch_role': self.batch_role,
                       'read_as': [self.registrar], 'max_orders': 200, 'deadline_margin': 60}
        self.runner = self.new_runner()
        for party in [*self.holders, self.sponsor]:
            self.give_usd(party, '1000')

    # Parties

    def name(self, party):
        return self.names.get(party, (party or 'standard account').split('::')[0])

    def party(self, hint):
        party = super().party(hint)
        with self.names_lock:
            self.names[party] = hint
            self.locks[party] = threading.RLock()
        return party

    def all_parties(self):
        with self.names_lock:
            return list(self.names)

    # Journaled submission

    def submit(self, party, command, also=()):
        """Submit as `party`, plus any `also` parties that must act with it."""
        with self.locks[party]:
            if party in self.unresolved:
                raise Unresolved('An earlier action of this party has no known outcome. Reconcile first.')
            command_id = uuid.uuid4().hex
            body = {'userId': 'demo', 'commandId': command_id, 'submissionId': uuid.uuid4().hex,
                    'actAs': [party, *also], 'readAs': [self.custodian, self.registrar], 'commands': [command]}
            record = {'offset': self.api.offset(), 'body': body, 'pending': True}
            path = self.folder / f'{command_id}.json'
            path.write_text(json.dumps(record))
            try:
                tree = self.api.post('/v2/commands/submit-and-wait-for-transaction-tree', body)['transactionTree']
            except (ApiError, OSError, TimeoutError) as error:
                if isinstance(error, ApiError) and error.status < 500 and error.status not in (408, 429):
                    record.update(pending=False, error=str(error))
                    path.write_text(json.dumps(record))
                    raise
                self.unresolved[party] = (path, record)
                self.emit('error', 'No response from the ledger. Checking the completion stream.', command=command_id)
                return self.reconcile(party)
            record.update(pending=False, updateId=tree['updateId'])
            path.write_text(json.dumps(record))
            return tree

    def reconcile(self, party):
        """Find the outcome of the party's unresolved command and return its tree."""
        with self.locks[party]:
            if party not in self.unresolved:
                return None
            path, record = self.unresolved[party]
            command_id = record['body']['commandId']
            stream = self.api.socket('/v2/commands/completions', {
                'userId': 'demo', 'parties': [party], 'beginExclusive': record['offset']}, timeout=1)
            give_up = time.monotonic() + 60
            try:
                while time.monotonic() < give_up:
                    try:
                        raw = stream.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        break
                    completion = json.loads(raw).get('completionResponse', {}).get('Completion', {}).get('value')
                    if not completion or completion.get('commandId') != command_id:
                        continue
                    record.update(pending=False, completion=completion)
                    path.write_text(json.dumps(record))
                    del self.unresolved[party]
                    if completion.get('status', {}).get('code', 0):
                        raise ApiError(409, completion)
                    query = urlencode({'parties': self.all_parties()}, doseq=True)
                    return self.api.post(f"/v2/updates/transaction-tree-by-id/{completion['updateId']}?{query}")['transaction']
            finally:
                stream.close()
            raise Unresolved('Still no outcome. The party stays blocked until Reconcile finds one.')

    # Orders and ledger

    def order(self, party, kind, amount, minimum='0', lifetime=500):
        with self.locks[party]:
            amount, minimum = amount_text(amount), amount_text(minimum)
            if Decimal(amount) <= 0 or not 5 <= int(lifetime) <= 550:
                raise ValueError('The amount must be positive and the deadline 5 to 550 seconds away')
            return self.place(party, kind, amount, int(lifetime), minimum)

    def ledger_event(self):
        return next(e for e in self.api.contracts([self.custodian])
                    if e['templateId'] == self.vault('VaultLedger')
                    and e['createArgument']['manager'] == self.manager)

    def new_runner(self):
        journal = self.folder / f"batcher-{self.batcher.split('::')[0]}.json"
        return BatchRunner(self.api, self.config, journal)

    def run_batch(self):
        with self.batch_lock:
            started = time.monotonic()
            result = self.runner.tick()
            if result['status'] not in ('idle', 'halted'):
                self.emit('batch', f"Batcher {result['status']} in {time.monotonic() - started:.2f}s, "
                                   f"{result.get('orders', '?')} orders sent")
            return result

    def halt(self, flag):
        with self.batch_lock:
            choice, role = ('VaultManager_Halt', self.halt_role) if flag else ('VaultManager_Resume', self.resume_role)
            self.submit(self.warden, exercise_cmd(self.vault('VaultManager'), self.manager, choice, {
                'ledger': self.ledger_event()['contractId'],
                'authorization': {'actor': self.warden, 'role': role}}))

    def revoke(self, cid):
        live = {e['contractId']: e for e in self.api.contracts([self.custodian])}
        if cid not in live:
            return
        if not live[cid]['templateId'].endswith(':VaultRole'):
            raise ValueError('That contract is not a role')
        self.submit(self.custodian, exercise_cmd(live[cid]['templateId'], cid, 'VaultRole_Revoke', {}))

    def replace(self, role):
        """Give a role to a fresh party and revoke the old ones. The manager stays the same."""
        with self.batch_lock:
            record = self.runner.journal.read()
            if record and record.get('pending'):
                self.runner.reconcile(record)
            actor = self.party(role.lower())
            self.names[actor] = f"{role} {sum(n.startswith(role) for n in self.names.values()) + 1}"
            if role == 'Batcher':
                role = self.role(actor, 'batch')
                self.revoke(self.batch_role)
                self.batcher, self.batch_role = actor, role
                self.config.update(batcher=actor, batch_role=role)
                self.runner = self.new_runner()
            else:
                halt, resume = self.role(actor, 'halt'), self.role(actor, 'resume')
                self.revoke(self.halt_role)
                self.revoke(self.resume_role)
                self.warden, self.halt_role, self.resume_role = actor, halt, resume

    def withdraw(self, cid):
        order = next(e for e in self.api.contracts([self.custodian]) if e['contractId'] == cid)
        spec = order['createArgument']['spec']
        owner = spec.get('holder') or spec['sponsor']
        template = order['templateId']
        self.submit(owner, exercise_cmd(template, cid, template.split(':')[-1] + '_Withdraw', {'extraArgs': NO_ARGS}))

    def donate(self, party, amount):
        """Send USD straight to the custodian, outside the vault. It must not move the price."""
        with self.locks[party]:
            transfer = {'sender': account(party), 'receiver': account(self.custodian),
                        'amount': amount_text(amount), 'instrumentId': {'admin': self.registrar, 'id': 'USD'},
                        'requestedAt': iso(), 'executeBefore': iso(300),
                        'inputHoldingCids': self.free_holdings(party, {'admin': self.registrar, 'id': 'USD'}),
                        'meta': {'values': {}}}
            interface = f'{self.transfer_pkg}:Splice.Api.Token.TransferInstructionV2'
            # A one-step transfer: sender and receiver act together.
            self.submit(party, exercise_cmd(f'{interface}:TransferFactory', self.underlying_rules,
                                            'TransferFactory_Transfer',
                                            {'transfer': transfer, 'actors': [party, self.custodian],
                                             'extraArgs': NO_ARGS}), also=[self.custodian])
            self.emit('state', f'{amount} USD sent straight to the custodian. The book does not change.')

    # Snapshot for the dashboard

    def snapshot(self):
        offset = self.api.offset()
        events = self.api.contracts(self.all_parties(), offset)
        ledgers = [e for e in events if e['templateId'] == self.vault('VaultLedger')
                   and e['createArgument']['manager'] == self.manager]
        if len(ledgers) != 1:
            raise RuntimeError('Expected exactly one vault ledger')
        current = ledgers[0]
        book = current['createArgument']['book']
        managed, issued = Decimal(book['managedAssets']), Decimal(book['issuedShares'])
        price = managed / issued if issued else Decimal(1)
        usd = {'admin': self.registrar, 'id': 'USD'}
        vusd = {'admin': self.custodian, 'id': 'vUSD'}
        wallets = {p: {'party': p, 'name': self.name(p), 'usd': Decimal(0), 'vusd': Decimal(0),
                       'lockedUsd': Decimal(0), 'lockedVusd': Decimal(0)} for p in self.all_parties()}
        holdings, orders, roles = {}, [], []
        for event in events:
            name, data = event['templateId'].split(':')[-1], event['createArgument']
            if name == 'TokenHolding':
                holding = data
                holdings[event['contractId']] = holding
                owner = holding['owner']
                instrument = {'admin': holding['admin'], 'id': holding['instrument']}
                key = 'usd' if instrument == usd else 'vusd' if instrument == vusd else None
                if owner in wallets and key:
                    slot = ('locked' + key.capitalize()) if holding.get('lock') else key
                    wallets[owner][slot] += Decimal(holding['amount'])
            elif name in ORDER_TEMPLATES and data['spec']['manager'] == self.manager:
                spec = data['spec']
                orders.append(dict(spec, cid=event['contractId'], kind=name.removesuffix('Order'),
                                   name=self.name(spec.get('holder') or spec.get('sponsor'))))
            elif name == 'VaultRole' and data['vault'] == self.manager:
                roles.append({'cid': event['contractId'], 'name': self.name(data['grantee']),
                              'permission': data['permission']})
        reserved = sum((Decimal(holdings[cid]['amount']) for cid in book['reserve']), Decimal(0))
        custody = sum((Decimal(h['amount']) for h in holdings.values()
                       if h['owner'] == self.custodian and h['admin'] == self.registrar and h['instrument'] == 'USD'),
                      Decimal(0))
        supply = sum((Decimal(h['amount']) for h in holdings.values()
                      if h['admin'] == self.custodian and h['instrument'] == 'vUSD'), Decimal(0))
        people = [dict(w, isHolder=p in self.holders, worth=(w['vusd'] + w['lockedVusd']) * price)
                  for p, w in wallets.items() if p in self.holders or p == self.sponsor]
        return {
            'ledgerUrl': self.api.url, 'offset': offset, 'ledgerCid': current['contractId'],
            'managedAssets': book['managedAssets'], 'issuedShares': book['issuedShares'],
            'price': str(price), 'halted': current['createArgument']['halted'],
            'orders': sorted(orders, key=lambda o: o['placedAt']), 'wallets': people, 'roles': roles,
            'autorun': self.autorun, 'loading': self.loading, 'loadSettings': self.load_settings,
            'balance': {'ok': reserved == managed and supply == issued, 'reserve': str(reserved),
                        'supply': str(supply), 'custody': str(custody), 'donations': str(custody - reserved)},
            'unresolved': [self.name(p) for p in self.unresolved], 'at': time.time()}

    # Dashboard actions

    def act(self, data):
        action = data['action']
        if action == 'wallets':
            count = min(20, max(1, int(data.get('count', 1))))
            for _ in range(count):
                label = str(data.get('name') or 'Holder').strip()[:40]
                party = self.party(label)
                self.names[party] = label if count == 1 else f'{label} {len(self.holders) + 1}'
                self.holders.append(party)
                self.give_usd(party, amount_text(data.get('amount', '1000')))
        elif action in ('deposit', 'redeem', 'dust'):
            if data['party'] not in self.holders:
                raise ValueError('Pick one of the demo holders')
            kind = 'Redeem' if action == 'redeem' else 'Deposit'
            size = '0.0000000001' if action == 'dust' else data['amount']
            self.order(data['party'], kind, size, data.get('minimum', '0'), data.get('lifetime', 500))
        elif action == 'topup':
            self.order(self.sponsor, 'TopUp', data['amount'])
        elif action == 'run':
            return self.run_batch()
        elif action == 'autorun':
            self.autorun = bool(data['enabled'])
        elif action in ('halt', 'resume'):
            self.halt(action == 'halt')
        elif action == 'revoke':
            self.revoke(data['cid'])
        elif action == 'replace':
            if data['role'] not in ('Batcher', 'Warden'):
                raise ValueError('Unknown role')
            self.replace(data['role'])
        elif action == 'withdraw':
            self.withdraw(data['cid'])
        elif action == 'donate':
            if data['party'] not in self.holders:
                raise ValueError('Pick one of the demo holders')
            self.donate(data['party'], data['amount'])
        elif action == 'check':
            balance = self.snapshot()['balance']
            self.emit('state' if balance['ok'] else 'error',
                      f"Balance check {'PASS' if balance['ok'] else 'FAIL'}. Reserve {balance['reserve']}, "
                      f"shares {balance['supply']}, ignored donations {balance['donations']}.")
            return balance
        elif action == 'reconcile':
            for party in list(self.unresolved):
                self.reconcile(party)
        elif action == 'load':
            self.set_load(data)
        else:
            raise ValueError('Unknown action')
        return {'ok': True}

    def set_load(self, data):
        if not data['enabled']:
            self.loading = False
            return
        settings = {
            'rate': min(5.0, max(.2, float(data.get('rate', 1)))),
            'active': min(len(self.holders), max(1, int(data.get('active', 3)))),
            'mix': min(100.0, max(0.0, float(data.get('mix', 70)))),
            'low': amount_text(data.get('low', '1')),
            'high': amount_text(data.get('high', '5')),
            'topUpEvery': max(0.0, float(data.get('topUpEvery', 0))),
            'topUpAmount': amount_text(data.get('topUpAmount', '1')),
        }
        if Decimal(settings['low']) <= 0 or Decimal(settings['high']) < Decimal(settings['low']):
            raise ValueError('The load amount range is not valid')
        self.load_settings, self.loading = settings, True
