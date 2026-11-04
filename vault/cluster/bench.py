"""Settlement throughput of the batched and the sharded vault on a Canton network.

The custodian, registrar, sponsor and batcher live on one participant, the
holders on another. Holders place orders from their own participant and see
the vault and token contracts through explicit disclosure. Setup and order
placement are not timed. The timed part is settling the whole backlog.

    bench.py --custodian http://HOST:5013 --holders http://HOST:5013 --mode sharded --shards 4
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / 'batcher'))
from jsonapi import ApiError, JsonApi, create_cmd, exercise_cmd  # noqa: E402
from sandbox_run import DARS, package_id  # noqa: E402

NO_ARGS = {'context': {'values': {}}, 'meta': {'values': {}}}
BURN = {'owner': None, 'provider': None, 'id': 'cip-112/burn'}
VAULT_DAR = ROOT / 'packages/vault/.daml/dist/canton-vault-0.1.0.dar'
SHARDED_DAR = ROOT / 'packages/vault-sharded/.daml/dist/canton-vault-sharded-0.1.0.dar'
TOKEN_DAR = ROOT / 'packages/token/.daml/dist/canton-token-0.1.0.dar'


def account(party):
    return {'owner': party, 'provider': None, 'id': ''}


def iso(seconds=0):
    moment = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return moment.isoformat().replace('+00:00', 'Z')


def micros(seconds):
    return {'microseconds': str(int(seconds * 1_000_000))}


def log(message):
    print(f'[{dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")}] {message}', flush=True)


def created(tree, suffix):
    """Contract ids created in a transaction tree whose template ends with `suffix`."""
    return [e['CreatedTreeEvent']['value'] for e in tree['eventsById'].values()
            if 'CreatedTreeEvent' in e and e['CreatedTreeEvent']['value']['templateId'].endswith(':' + suffix)]


class Node:
    """One participant's JSON Ledger API."""

    def __init__(self, url):
        self.api = JsonApi(url)
        self.url = url

    def party(self, hint):
        body = {'partyIdHint': f'{hint}-{uuid.uuid4().hex[:8]}'}
        return self.api.post('/v2/parties', body)['partyDetails']['party']

    def upload(self, dar):
        request = urllib.request.Request(self.url.rstrip('/') + '/v2/packages', dar.read_bytes(),
                                         {'Content-Type': 'application/octet-stream'})
        with urllib.request.urlopen(request, timeout=300) as response:
            response.read()

    def body(self, act_as, commands, read_as=(), disclosed=()):
        return {'userId': 'cluster-bench', 'commandId': uuid.uuid4().hex, 'actAs': list(act_as),
                'readAs': list(read_as), 'commands': commands, 'disclosedContracts': list(disclosed)}

    def tree(self, act_as, command, read_as=(), disclosed=(), retries=5):
        for attempt in range(retries + 1):
            try:
                return self.api.post('/v2/commands/submit-and-wait-for-transaction-tree',
                                     self.body(act_as, [command], read_as, disclosed))['transactionTree']
            except ApiError as error:
                text = json.dumps(error.body)
                if attempt < retries and ('CONTRACT_NOT_FOUND' in text or 'UNKNOWN_INFORMEES' in text
                                          or 'NO_SYNCHRONIZER' in text or error.status >= 500):
                    time.sleep(1 + attempt)
                    continue
                raise

    def delta(self, act_as, command, party, templates, read_as=()):
        """Submit and get back only creates and archives of `templates` seen by `party`."""
        filters = [{'identifierFilter': {'TemplateFilter': {'value': {
            'templateId': t, 'includeCreatedEventBlob': False}}}} for t in templates]
        body = {'commands': self.body(act_as, [command], read_as),
                'transactionFormat': {
                    'eventFormat': {'filtersByParty': {party: {'cumulative': filters}}, 'verbose': False},
                    'transactionShape': 'TRANSACTION_SHAPE_ACS_DELTA'}}
        return self.api.post('/v2/commands/submit-and-wait-for-transaction', body)['transaction']

    def active(self, parties):
        """Active contracts with their synchronizer, for disclosure."""
        wildcard = {'identifierFilter': {'WildcardFilter': {'value': {'includeCreatedEventBlob': True}}}}
        body = {'activeAtOffset': self.api.offset(),
                'filter': {'filtersByParty': {p: {'cumulative': [wildcard]} for p in parties}}, 'verbose': True}
        connection = self.api.socket('/v2/state/active-contracts', body)
        found = {}
        try:
            while raw := connection.recv():
                entry = json.loads(raw).get('contractEntry', {}).get('JsActiveContract')
                if entry:
                    found[entry['createdEvent']['contractId']] = entry
        finally:
            connection.close()
        return list(found.values())


class Cluster:
    def __init__(self, args):
        self.args = args
        self.c = Node(args.custodian)
        self.h = Node(args.holders_url)
        self.sharded = args.mode == 'sharded'
        self.token = package_id(TOKEN_DAR)
        self.vault_pkg = package_id(VAULT_DAR)
        self.sharded_pkg = package_id(SHARDED_DAR)
        self.alloc_iface = package_id(DARS / 'splice-api-token-allocation-instruction-v2-1.0.0.dar') + \
            ':Splice.Api.Token.AllocationInstructionV2:AllocationFactory'
        self.module = (f'{self.sharded_pkg}:CantonSolutions.Vault.Sharded' if self.sharded
                       else f'{self.vault_pkg}:CantonSolutions.Vault.Manager')
        self.manager_t = self.module + (':ShardedManager' if self.sharded else ':VaultManager')
        # Event filters take package names, not package ids.
        self.module_name = ('#canton-vault-sharded:CantonSolutions.Vault.Sharded' if self.sharded
                            else '#canton-vault:CantonSolutions.Vault.Manager')
        self.prefix = 'ShardedManager' if self.sharded else 'VaultManager'
        self.role_t = f'{self.vault_pkg}:CantonSolutions.Vault.Roles:VaultRole'

    def t(self, name):
        return f'{self.token}:CantonSolutions.Token:{name}'

    # Setup

    def setup(self):
        a = self.args
        for node in (self.c, self.h):
            node.upload(SHARDED_DAR)
        log('DARs uploaded to both participants')
        self.custodian, self.registrar = self.c.party('custodian'), self.c.party('registrar')
        self.sponsor, self.batcher = self.c.party('sponsor'), self.c.party('batcher')
        self.holders = [self.h.party(f'holder{i}') for i in range(a.holder_count)]
        self.usd_rules = self.make(self.registrar, self.t('TokenRules'),
                                   {'admin': self.registrar, 'maxLifetime': micros(3600)})
        self.share_rules = self.make(self.custodian, self.t('TokenRules'),
                                     {'admin': self.custodian, 'maxLifetime': micros(3600)})
        terms = {'custodian': self.custodian, 'sponsor': self.sponsor,
                 'underlying': {'admin': self.registrar, 'id': 'USD'},
                 'underlyingAllocator': self.usd_rules, 'underlyingSettler': self.usd_rules,
                 'shareRegistry': self.share_rules, 'shareId': 'vUSD',
                 'orderLifetime': micros(3000), 'underlyingLifetime': micros(3600)}
        argument = {'terms': terms}
        if self.sharded:
            argument.update(shardCount=str(a.shards), underlyingTransfers=self.usd_rules)
        self.manager = self.make(self.custodian, self.manager_t, argument)
        tree = self.c.tree([self.custodian], exercise_cmd(self.manager_t, self.manager, self.prefix + '_Open', {}))
        if self.sharded:
            self.board = created(tree, 'PriceBoard')[0]['contractId']
            shards = sorted(created(tree, 'Shard'), key=lambda e: int(e['createArgument']['index']))
            self.shards = [e['contractId'] for e in shards]
        else:
            self.ledger = created(tree, 'VaultLedger')[0]['contractId']
        self.batch_role = self.role(self.batcher, 'batch')
        self.fold_role = self.role(self.batcher, 'fold') if self.sharded else None
        events = {e['createdEvent']['contractId']: e for e in self.c.active([self.custodian, self.registrar])}
        self.disclosed = [{'templateId': events[cid]['createdEvent']['templateId'], 'contractId': cid,
                           'createdEventBlob': events[cid]['createdEvent']['createdEventBlob'],
                           'synchronizerId': events[cid]['synchronizerId']}
                          for cid in (self.manager, self.usd_rules, self.share_rules)]
        log(f'vault open: {a.mode}, shards {a.shards if self.sharded else 1}, {len(self.holders)} holders')
        offers = [self.make(self.registrar, self.t('TokenOffer'),
                            {'admin': self.registrar, 'owner': h, 'instrument': 'USD', 'amount': str(a.funding)})
                  for h in self.holders]
        self.wait_visible(self.h, self.holders, set(offers))
        self.free = {}
        with ThreadPoolExecutor(16) as pool:
            for h, holding in zip(self.holders, pool.map(self.accept, self.holders, offers)):
                self.free[(h, 'USD')] = [holding]
        log('holders funded')

    def make(self, party, template, argument):
        tree = self.c.tree([party], create_cmd(template, argument))
        return created(tree, template.split(':')[-1])[0]['contractId']

    def role(self, grantee, permission):
        return self.make(self.custodian, self.role_t, {'custodian': self.custodian, 'grantee': grantee,
                                                       'vault': self.manager, 'permission': permission})

    def accept(self, holder, offer):
        tree = self.h.tree([holder], exercise_cmd(self.t('TokenOffer'), offer, 'TokenOffer_Accept', {}))
        return created(tree, 'TokenHolding')[0]['contractId']

    def wait_visible(self, node, parties, cids, timeout=300):
        give_up = time.monotonic() + timeout
        while True:
            seen = {e['createdEvent']['contractId'] for e in node.active(parties)}
            if cids <= seen:
                return
            if time.monotonic() > give_up:
                raise RuntimeError(f'{len(cids - seen)} contracts never became visible')
            time.sleep(1)

    # Orders

    def settlement(self):
        if self.sharded:
            return {'executors': [self.custodian], 'id': 'canton-solutions/vault', 'cid': self.manager,
                    'meta': {'values': {}}}
        return {'executors': [self.custodian], 'id': uuid.uuid4().hex, 'cid': None, 'meta': {'values': {}}}

    def take_change(self, holder, instrument, tree):
        admin = self.custodian if instrument == 'vUSD' else self.registrar
        self.free[(holder, instrument)] = [
            e['contractId'] for e in created(tree, 'TokenHolding')
            if e['createArgument']['owner'] == holder and e['createArgument']['admin'] == admin
            and e['createArgument']['instrument'] == instrument and not e['createArgument'].get('lock')]

    def place(self, holder, kind, amount, lifetime=2900):
        """Lock the input from the holder's participant and place the order."""
        redeem = kind == 'Redeem'
        instrument = 'vUSD' if redeem else 'USD'
        admin = self.custodian if redeem else self.registrar
        rules = self.share_rules if redeem else self.usd_rules
        deadline = iso(lifetime)
        side = {'transferLegId': uuid.uuid4().hex, 'side': 'SenderSide',
                'otherside': BURN if redeem else account(self.custodian),
                'amount': amount, 'instrumentId': instrument, 'meta': {'values': {}}}
        allocate = {'settlement': self.settlement(),
                    'allocation': {'admin': admin, 'authorizer': account(holder), 'transferLegSides': [side],
                                   'settlementDeadline': deadline, 'nextIterationFunding': None,
                                   'committed': True, 'meta': {'values': {}}},
                    'requestedAt': iso(), 'inputHoldingCids': self.free[(holder, instrument)],
                    'actors': [holder], 'extraArgs': NO_ARGS}
        tree = self.h.tree([holder], exercise_cmd(self.alloc_iface, rules, 'AllocationFactory_Allocate', allocate),
                           disclosed=self.disclosed)
        self.take_change(holder, instrument, tree)
        allocation = created(tree, 'TokenAllocation')[0]['contractId']
        argument = {'allocation': allocation, 'amount': amount, 'deadline': deadline}
        if kind == 'Deposit':
            argument.update(holder=holder, minShares='0')
        elif kind == 'Redeem':
            argument.update(holder=holder, minAssets='0')
        party = self.sponsor if kind == 'TopUp' else holder
        node = self.c if kind == 'TopUp' else self.h
        tree = node.tree([party], exercise_cmd(self.manager_t, self.manager, f'{self.prefix}_Place{kind}', argument),
                         disclosed=self.disclosed)
        return created(tree, kind + 'Order')[0]['contractId']

    def fund_sponsor(self, amount):
        offer = self.make(self.registrar, self.t('TokenOffer'),
                          {'admin': self.registrar, 'owner': self.sponsor, 'instrument': 'USD', 'amount': str(amount)})
        tree = self.c.tree([self.sponsor], exercise_cmd(self.t('TokenOffer'), offer, 'TokenOffer_Accept', {}))
        self.free[(self.sponsor, 'USD')] = [created(tree, 'TokenHolding')[0]['contractId']]

    def place_topup(self, amount):
        """A sponsor top-up, placed from the custodian participant."""
        holder = self.sponsor
        deadline = iso(2900)
        side = {'transferLegId': uuid.uuid4().hex, 'side': 'SenderSide', 'otherside': account(self.custodian),
                'amount': str(amount), 'instrumentId': 'USD', 'meta': {'values': {}}}
        allocate = {'settlement': self.settlement(),
                    'allocation': {'admin': self.registrar, 'authorizer': account(holder), 'transferLegSides': [side],
                                   'settlementDeadline': deadline, 'nextIterationFunding': None,
                                   'committed': True, 'meta': {'values': {}}},
                    'requestedAt': iso(), 'inputHoldingCids': self.free[(holder, 'USD')],
                    'actors': [holder], 'extraArgs': NO_ARGS}
        tree = self.c.tree([holder], exercise_cmd(self.alloc_iface, self.usd_rules, 'AllocationFactory_Allocate',
                                                  allocate), disclosed=self.disclosed)
        allocation = created(tree, 'TokenAllocation')[0]['contractId']
        tree = self.c.tree([holder], exercise_cmd(self.manager_t, self.manager, f'{self.prefix}_PlaceTopUp',
                                                  {'allocation': allocation, 'amount': str(amount),
                                                   'deadline': deadline}), disclosed=self.disclosed)
        return created(tree, 'TopUpOrder')[0]['contractId']

    # Batches

    def authorization(self, role):
        return {'actor': self.batcher, 'role': role}

    def order_templates(self):
        return [f'{self.module_name}:{k}Order' for k in ('Deposit', 'Redeem', 'TopUp')]

    def run_batch(self, refs, shard=None):
        """One batch. Returns (seconds, executed order ids, new ledger or shard id)."""
        if self.sharded:
            argument = {'board': self.board, 'shard': shard, 'orders': refs, 'underlyingArgs': NO_ARGS,
                        'authorization': self.authorization(self.batch_role)}
            book = f'{self.module_name}:Shard'
        else:
            argument = {'ledger': self.ledger, 'orders': refs, 'underlyingArgs': NO_ARGS,
                        'authorization': self.authorization(self.batch_role)}
            book = f'{self.module_name}:VaultLedger'
        started = time.monotonic()
        tx = self.c.delta([self.batcher], exercise_cmd(self.manager_t, self.manager, self.prefix + '_RunBatch', argument),
                          self.custodian, [book, *self.order_templates()], read_as=[self.custodian, self.registrar])
        seconds = time.monotonic() - started
        archived, next_book = set(), None
        for event in tx.get('events', []):
            if 'ArchivedEvent' in event:
                archived.add(event['ArchivedEvent']['contractId'])
            elif 'CreatedEvent' in event and event['CreatedEvent']['templateId'].endswith(':' + book.split(':')[-1]):
                next_book = event['CreatedEvent']['contractId']
        return seconds, archived, next_book

    def fold(self, top_ups=(), orders=()):
        argument = {'board': self.board, 'shards': self.shards, 'topUps': list(top_ups), 'orders': list(orders),
                    'underlyingArgs': NO_ARGS, 'authorization': self.authorization(self.fold_role)}
        started = time.monotonic()
        tree = self.c.tree([self.batcher], exercise_cmd(self.manager_t, self.manager, 'ShardedManager_Fold', argument),
                           read_as=[self.custodian, self.registrar])
        seconds = time.monotonic() - started
        self.board = created(tree, 'PriceBoard')[0]['contractId']
        shards = sorted(created(tree, 'Shard'), key=lambda e: int(e['createArgument']['index']))
        self.shards = [e['contractId'] for e in shards]
        return seconds

    @staticmethod
    def refs(orders):
        return [{'tag': kind + 'Ref', 'value': cid} for cid, kind in orders]

    def drain(self, orders, shard_index=None):
        """Batch `orders` until all are executed. Returns per-batch records."""
        pending = list(orders)
        records = []
        while pending:
            chunk = pending[:self.args.batch]
            shard = self.shards[shard_index] if self.sharded else None
            seconds, archived, next_book = self.run_batch(self.refs(chunk), shard)
            if self.sharded and next_book:
                self.shards[shard_index] = next_book
            elif not self.sharded and next_book:
                self.ledger = next_book
            done = [o for o in chunk if o[0] in archived]
            records.append({'shard': shard_index, 'orders': len(chunk), 'executed': len(done), 'seconds': seconds})
            if not done:
                raise RuntimeError(f'a batch executed nothing: {len(chunk)} orders stuck')
            pending = [o for o in pending if o[0] not in archived]
        return records

    # Phases

    def seed(self):
        """Give every holder shares, then top up so the price is not one."""
        a = self.args
        with ThreadPoolExecutor(16) as pool:
            deposits = list(pool.map(lambda h: (self.place(h, 'Deposit', str(a.seed)), 'Deposit'), self.holders))
        self.wait_visible(self.c, [self.custodian], {cid for cid, _ in deposits})
        if self.sharded:
            for i in range(a.shards):
                self.drain(deposits[i::a.shards], i)
        else:
            self.drain(deposits)
        self.fund_sponsor(a.seed * len(self.holders))
        top = self.place_topup(Decimal(a.seed) * len(self.holders) / 10)
        self.wait_visible(self.c, [self.custodian], {top})
        if self.sharded:
            self.fold([top])
        else:
            self.drain([(top, 'TopUp')])
        for h in self.holders:
            self.free[(h, 'vUSD')] = [e['createdEvent']['contractId'] for e in self.h.active([h])
                                      if e['createdEvent']['templateId'].endswith(':TokenHolding')
                                      and e['createdEvent']['createArgument']['admin'] == self.custodian
                                      and e['createdEvent']['createArgument']['owner'] == h
                                      and not e['createdEvent']['createArgument'].get('lock')]
        log('seeded: every holder has shares, price raised by the top-up')

    def backlog(self):
        """Place N orders, half deposits and half redemptions, spread over holders."""
        a = self.args
        per_holder = a.orders // len(self.holders)
        placed, lock = [], threading.Lock()
        started = time.monotonic()

        def work(holder):
            for i in range(per_holder):
                kind, amount = ('Deposit', '1') if i % 2 == 0 else ('Redeem', '0.5')
                cid = self.place(holder, kind, amount)
                with lock:
                    placed.append((cid, kind))
        with ThreadPoolExecutor(min(len(self.holders), a.workers)) as pool:
            list(pool.map(work, self.holders))
        seconds = time.monotonic() - started
        log(f'placed {len(placed)} orders in {seconds:.1f}s ({len(placed) / seconds:.1f} orders/s)')
        self.wait_visible(self.c, [self.custodian], {cid for cid, _ in placed}, timeout=900)
        return placed, seconds

    def settle(self, placed):
        a = self.args
        started = time.monotonic()
        if self.sharded:
            parts = [[] for _ in range(a.shards)]
            for cid, kind in placed:
                parts[int(hashlib.sha256(cid.encode()).hexdigest(), 16) % a.shards].append((cid, kind))
            with ThreadPoolExecutor(a.shards) as pool:
                records = [r for part in pool.map(self.drain, parts, range(a.shards)) for r in part]
        else:
            records = self.drain(placed)
        return time.monotonic() - started, records

    def balances(self):
        events = [e['createdEvent'] for e in self.c.active([self.custodian, self.registrar])]
        holdings = {e['contractId']: e['createArgument'] for e in events if e['templateId'].endswith(':TokenHolding')}
        shares = sum((Decimal(h['amount']) for h in holdings.values()
                      if h['admin'] == self.custodian and h['instrument'] == 'vUSD'), Decimal(0))
        if self.sharded:
            board = next(e['createArgument'] for e in events if e['contractId'] == self.board)
            books = [e['createArgument']['book'] for e in events if e['contractId'] in self.shards]
            reserve = [cid for b in books for cid in b['reserve']]
            assets = sum((Decimal(b['assets']) for b in books), Decimal(0))
            issued = Decimal(board['issuedShares']) + sum((Decimal(b['shareDelta']) for b in books), Decimal(0))
        else:
            book = next(e['createArgument']['book'] for e in events if e['contractId'] == self.ledger)
            reserve, assets, issued = book['reserve'], Decimal(book['managedAssets']), Decimal(book['issuedShares'])
        reserved = sum((Decimal(holdings[c]['amount']) for c in reserve), Decimal(0))
        ok = reserved == assets and shares == issued
        return {'ok': ok, 'assets': str(assets), 'reserved': str(reserved), 'issued': str(issued), 'shares': str(shares)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--custodian', required=True)
    parser.add_argument('--holders', dest='holders_url', required=True)
    parser.add_argument('--mode', choices=['batched', 'sharded'], required=True)
    parser.add_argument('--shards', type=int, default=1)
    parser.add_argument('--orders', type=int, default=4000)
    parser.add_argument('--batch', type=int, default=200)
    parser.add_argument('--holder-count', type=int, default=40)
    parser.add_argument('--workers', type=int, default=40)
    parser.add_argument('--funding', type=int, default=400)
    parser.add_argument('--seed', type=int, default=100)
    parser.add_argument('--label', default='')
    parser.add_argument('--out', default=str(HERE / 'results' / 'runs.jsonl'))
    args = parser.parse_args()
    cluster = Cluster(args)
    run = {'label': args.label, 'mode': args.mode, 'shards': args.shards if args.mode == 'sharded' else 1,
           'orders': args.orders, 'batch': args.batch, 'holders': args.holder_count, 'started': iso()}
    cluster.setup()
    cluster.seed()
    placed, placing = cluster.backlog()
    run.update(placed=len(placed), placement_seconds=round(placing, 3))
    log('settling the backlog')
    run['settle_started'] = iso()
    seconds, records = cluster.settle(placed)
    run.update(settle_ended=iso(), settle_seconds=round(seconds, 3), orders_per_second=round(len(placed) / seconds, 2),
               batches=len(records), batch_seconds=[round(r['seconds'], 3) for r in records])
    log(f'settled {len(placed)} orders in {seconds:.1f}s: {len(placed) / seconds:.2f} orders/s, {len(records)} batches')
    if cluster.sharded:
        run['fold_seconds'] = round(cluster.fold(), 3)
        log(f"fold took {run['fold_seconds']}s")
    run['balance'] = cluster.balances()
    log(f"balance check: {run['balance']}")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('a') as f:
        f.write(json.dumps(run) + '\n')
    if not run['balance']['ok']:
        raise SystemExit('book does not match the token holdings')


if __name__ == '__main__':
    main()
