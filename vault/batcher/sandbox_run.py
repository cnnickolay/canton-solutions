"""Run the whole vault life cycle with the batcher against a local sandbox.

The sandbox must already be running with the vault DAR loaded. The script makes
fresh parties and fresh token registries, so it never touches anything else.
"""
import argparse
import datetime as dt
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import uuid
import zipfile

from jsonapi import ApiError, JsonApi, create_cmd, exercise_cmd
from runner import MANAGER_MODULE, NO_ARGS, BatchRunner

ROOT = Path(__file__).resolve().parents[1]
DARS = ROOT / 'dars'
BURN = {'owner': None, 'provider': None, 'id': 'cip-112/burn'}


def package_id(dar):
    """Main package id of a DAR, read from its manifest."""
    with zipfile.ZipFile(dar) as archive:
        manifest = archive.read('META-INF/MANIFEST.MF').decode()
    manifest = manifest.replace('\r\n ', '').replace('\n ', '')
    main = next(line.split(': ', 1)[1] for line in manifest.splitlines() if line.startswith('Main-Dalf: '))
    return Path(main).stem.rsplit('-', 1)[1]


def account(party):
    return {'owner': party, 'provider': None, 'id': ''}


def iso(seconds_from_now=0):
    moment = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds_from_now)
    return moment.isoformat().replace('+00:00', 'Z')


def micros(seconds):
    return {'microseconds': str(seconds * 1_000_000)}


def say(message):
    print(message, flush=True)


class Sandbox:
    """Parties, registries and a vault on the sandbox, plus helpers to act as them."""

    def __init__(self, api):
        self.api = api
        self.vault_pkg = package_id(ROOT / 'packages/vault/.daml/dist/canton-vault-0.1.0.dar')
        self.token_pkg = package_id(ROOT / 'packages/token/.daml/dist/canton-token-0.1.0.dar')
        self.allocation_pkg = package_id(DARS / 'splice-api-token-allocation-instruction-v2-1.0.0.dar')
        self.transfer_pkg = package_id(DARS / 'splice-api-token-transfer-instruction-v2-1.0.0.dar')
        self.custodian = self.party('custodian')
        self.registrar = self.party('registrar')
        self.batcher = self.party('batcher')
        self.sponsor = self.party('sponsor')
        self.warden = self.party('warden')
        self.holders = [self.party('holder') for _ in range(3)]
        self.underlying_rules = self.token_registry(self.registrar)
        self.share_rules = self.token_registry(self.custodian)
        terms = {'custodian': self.custodian, 'sponsor': self.sponsor,
                 'underlying': {'admin': self.registrar, 'id': 'USD'},
                 'underlyingAllocator': self.underlying_rules, 'underlyingSettler': self.underlying_rules,
                 'shareRegistry': self.share_rules, 'shareId': 'vUSD',
                 'orderLifetime': micros(600), 'underlyingLifetime': micros(3600)}
        self.manager = self.created(
            self.submit(self.custodian, create_cmd(self.vault('VaultManager'), {'terms': terms})), 'VaultManager')
        self.submit(self.custodian, exercise_cmd(self.vault('VaultManager'), self.manager, 'VaultManager_Open', {}))
        self.batch_role = self.role(self.batcher, 'batch')
        self.halt_role = self.role(self.warden, 'halt')
        self.resume_role = self.role(self.warden, 'resume')

    # Plumbing

    def vault(self, name):
        return f'{self.vault_pkg}:{MANAGER_MODULE}:{name}'

    def token(self, name):
        return f'{self.token_pkg}:CantonSolutions.Token:{name}'

    def role_template(self):
        return f'{self.vault_pkg}:CantonSolutions.Vault.Roles:VaultRole'

    def party(self, hint):
        body = {'partyIdHint': f'vault-{hint}-{uuid.uuid4().hex[:8]}'}
        return self.api.post('/v2/parties', body)['partyDetails']['party']

    def submit(self, party, command):
        return self.submit_as([party], command)

    def submit_as(self, parties, command):
        return self.api.post('/v2/commands/submit-and-wait-for-transaction-tree', {
            'userId': 'sandbox-run', 'commandId': uuid.uuid4().hex, 'actAs': parties,
            'readAs': [self.custodian, self.registrar], 'commands': [command]})['transactionTree']

    @staticmethod
    def created(tree, name):
        return next(e['CreatedTreeEvent']['value']['contractId'] for e in tree['eventsById'].values()
                    if 'CreatedTreeEvent' in e
                    and e['CreatedTreeEvent']['value']['templateId'].endswith(':' + name))

    def wait_until_visible(self, cid, party):
        give_up = time.monotonic() + 30
        while cid not in {e['contractId'] for e in self.api.contracts([party])}:
            if time.monotonic() > give_up:
                raise RuntimeError(f'{cid} not visible to {party} after 30 seconds')
            time.sleep(.05)

    # Setup

    def token_registry(self, admin):
        return self.created(self.submit(admin, create_cmd(self.token('TokenRules'),
                                                          {'admin': admin, 'maxLifetime': micros(3600)})), 'TokenRules')

    def role(self, actor, permission):
        argument = {'custodian': self.custodian, 'grantee': actor, 'vault': self.manager, 'permission': permission}
        return self.created(self.submit(self.custodian, create_cmd(self.role_template(), argument)), 'VaultRole')

    def revoke(self, role):
        self.submit(self.custodian, exercise_cmd(self.role_template(), role, 'VaultRole_Revoke', {}))

    def give_usd(self, party, amount='100'):
        """Mint by creating the holding with the registrar and the owner together."""
        self.submit_as([self.registrar, party], create_cmd(self.token('TokenHolding'), {
            'admin': self.registrar, 'owner': party, 'instrument': 'USD', 'amount': amount, 'lock': None}))

    # Orders

    def free_holdings(self, party, instrument):
        return [e['contractId'] for e in self.api.contracts([party])
                if e['templateId'].endswith(':TokenHolding')
                and e['createArgument']['owner'] == party
                and e['createArgument']['admin'] == instrument['admin']
                and e['createArgument']['instrument'] == instrument['id']
                and e['createArgument'].get('lock') is None]

    def place(self, party, kind, amount, lifetime=500, minimum='0'):
        """Lock the input and place a Deposit, Redeem or TopUp order. Returns the order id."""
        redeem = kind == 'Redeem'
        admin, instrument, factory = ((self.custodian, 'vUSD', self.share_rules) if redeem
                                      else (self.registrar, 'USD', self.underlying_rules))
        receiver = BURN if redeem else account(self.custodian)
        deadline = iso(lifetime)
        side = {'transferLegId': 'input', 'side': 'SenderSide', 'otherside': receiver,
                'amount': amount, 'instrumentId': instrument, 'meta': {'values': {}}}
        allocate = {
            'settlement': {'executors': [self.custodian], 'id': uuid.uuid4().hex, 'cid': None,
                           'meta': {'values': {}}},
            'allocation': {'admin': admin, 'authorizer': account(party), 'transferLegSides': [side],
                           'settlementDeadline': deadline, 'nextIterationFunding': None,
                           'committed': True, 'meta': {'values': {}}},
            'requestedAt': iso(),
            'inputHoldingCids': self.free_holdings(party, {'admin': admin, 'id': instrument}),
            'actors': [party], 'extraArgs': NO_ARGS}
        tree = self.submit(party, exercise_cmd(
            f'{self.allocation_pkg}:Splice.Api.Token.AllocationInstructionV2:AllocationFactory',
            factory, 'AllocationFactory_Allocate', allocate))
        argument = {'allocation': self.created(tree, 'TokenAllocation'), 'amount': amount,
                    'deadline': deadline}
        if kind == 'Deposit':
            argument.update(holder=party, minShares=minimum)
        elif kind == 'Redeem':
            argument.update(holder=party, minAssets=minimum)
        tree = self.submit(party, exercise_cmd(self.vault('VaultManager'), self.manager,
                                               'VaultManager_Place' + kind, argument))
        return self.created(tree, kind + 'Order')

    def set_halted(self, flag):
        choice, role = ('VaultManager_Halt', self.halt_role) if flag else ('VaultManager_Resume', self.resume_role)
        self.submit(self.warden, exercise_cmd(self.vault('VaultManager'), self.manager, choice, {
            'ledger': self.check_balances(quiet=True)['contractId'],
            'authorization': {'actor': self.warden, 'role': role}}))

    def check_balances(self, quiet=False):
        """The book must equal the real tokens. Returns the current ledger event."""
        events = self.api.contracts([self.custodian, self.registrar])
        current = next(e for e in events if e['templateId'] == self.vault('VaultLedger')
                       and e['createArgument']['manager'] == self.manager)
        book = current['createArgument']['book']
        holdings = {e['contractId']: e['createArgument'] for e in events
                    if e['templateId'].endswith(':TokenHolding')}
        reserved = sum((Decimal(holdings[cid]['amount']) for cid in book['reserve']), Decimal(0))
        custody = sum((Decimal(h['amount']) for h in holdings.values()
                       if h['owner'] == self.custodian and h['admin'] == self.registrar
                       and h['instrument'] == 'USD'), Decimal(0))
        shares = sum((Decimal(h['amount']) for h in holdings.values()
                      if h['admin'] == self.custodian and h['instrument'] == 'vUSD'), Decimal(0))
        assert reserved == custody == Decimal(book['managedAssets']), book
        assert shares == Decimal(book['issuedShares']), book
        if not quiet:
            say(json.dumps({'balanced': True, 'assets': str(custody), 'shares': str(shares)}))
        return current

    def place_and_wait(self, party, kind, amount, lifetime=500):
        order = self.place(party, kind, amount, lifetime)
        self.wait_until_visible(order, self.custodian)
        return order

    def open_orders(self):
        return {e['contractId'] for e in self.api.contracts([self.custodian])}


def drop_next_response(api):
    """Make the next submit-and-wait lose its HTTP response after the ledger commits."""
    real = api.post

    def post(path, body=None):
        result = real(path, body)
        if path == '/v2/commands/submit-and-wait':
            api.post = real
            raise ApiError(503, {'message': 'response lost on purpose'})
        return result
    api.post = post


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:18703')
    args = parser.parse_args()
    s = Sandbox(JsonApi(args.url))
    for party in [*s.holders, s.sponsor]:
        s.give_usd(party)

    with tempfile.TemporaryDirectory(prefix='vault-batcher-') as folder:
        config = {'user_id': 'sandbox-run', 'custodian': s.custodian, 'batcher': s.batcher,
                  'manager': s.manager, 'batch_role': s.batch_role, 'read_as': [s.registrar],
                  'max_orders': 2}
        runner = BatchRunner(s.api, config, Path(folder) / 'first.journal.json')

        for holder in s.holders:
            s.place_and_wait(holder, 'Deposit', '10')
        drop_next_response(s.api)
        for expected in (2, 1):
            result = runner.tick()
            assert result['status'] == 'executed' and result['orders'] == expected, result
            s.check_balances()
        say('PASS three deposits in capped batches, a lost response was reconciled without resending')

        for role in (s.batch_role, s.halt_role, s.resume_role):
            s.revoke(role)
        try:
            runner.tick()
        except RuntimeError as error:
            assert 'not active or not visible' in str(error), error
        else:
            raise AssertionError('a revoked batch role was used')
        new_batcher = s.party('batcher-2')
        config = dict(config, batcher=new_batcher, batch_role=s.role(new_batcher, 'batch'))
        runner = BatchRunner(s.api, config, Path(folder) / 'second.journal.json')
        s.warden = s.party('warden-2')
        s.halt_role = s.role(s.warden, 'halt')
        s.resume_role = s.role(s.warden, 'resume')
        say('PASS revoked roles refused, a new batcher and warden took over')

        s.place_and_wait(s.holders[0], 'Redeem', '2')
        s.place_and_wait(s.sponsor, 'TopUp', '3')
        near_deadline = s.place_and_wait(s.holders[1], 'Deposit', '1', lifetime=45)
        s.check_balances()
        s.set_halted(True)
        assert runner.tick()['status'] == 'halted'
        s.check_balances()
        s.set_halted(False)
        result = runner.tick()
        assert result['status'] == 'executed' and result['orders'] == 2, result
        s.check_balances()
        assert runner.tick()['status'] == 'idle'
        assert near_deadline in s.open_orders()
        say('PASS redeem and top-up executed after a halt, the order near its deadline was left alone')

        before = s.check_balances()['contractId']
        dust = s.place_and_wait(s.holders[1], 'Deposit', '0.0000000001')
        result = runner.tick()
        assert result['status'] == 'executed' and result['orders'] == 1, result
        assert s.check_balances()['contractId'] == before
        assert dust in s.open_orders()
        assert runner.tick()['status'] == 'idle'
        assert runner.tick()['status'] == 'idle'
        say('PASS a zero-share deposit was deferred, the ledger did not move and later rounds stay idle')


if __name__ == '__main__':
    main()
