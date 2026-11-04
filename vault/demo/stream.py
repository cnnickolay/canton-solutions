"""Turns committed transaction trees into readable lines for the dashboard.

Everything shown comes from committed trees. Service messages, like a batch
taking a while, are labelled as such.
"""
from collections import deque
import datetime as dt
from decimal import ROUND_DOWN, Decimal
import json
import threading
import time

import websocket

TICK = Decimal('0.0000000001')
TAG_RANK = {'DepositRef': 0, 'RedeemRef': 1, 'TopUpRef': 2}


def short(cid):
    return (cid or '')[:8]


def plain(value):
    text = format(Decimal(str(value)), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def parse_time(text):
    return dt.datetime.fromisoformat(text.replace('Z', '+00:00'))


def owner_of(spec):
    return spec.get('holder') or spec.get('sponsor')


def price_of(book):
    if book is None:
        return None
    issued = Decimal(book['issuedShares'])
    return str(Decimal(book['managedAssets']) / issued if issued else Decimal(1))


class LedgerStream:
    def __init__(self):
        self.condition = threading.Condition()
        self.rows = deque(maxlen=600)
        self.next_id = 0
        self.seen = {}
        self.batch_times = deque(maxlen=10000)
        self.prices = deque(maxlen=600)
        self.batch_count = 0
        self.last_batch_at = None
        self.connected = False
        self.batch_cards = deque(maxlen=6)

    def emit(self, kind, text, command='', **extra):
        with self.condition:
            self.next_id += 1
            row = {'id': self.next_id, 'kind': kind, 'lines': [text], 'time': time.time(),
                   'command': command, **extra}
            self.rows.append(row)
            self.condition.notify_all()
            return row

    def spec(self, cid):
        return self.seen.get(cid, {}).get('createArgument', {}).get('spec', {})

    def queue(self, refs):
        """The order the ledger executes refs in: oldest first, top-ups last."""
        return sorted(refs, key=lambda ref: (ref['tag'] == 'TopUpRef', self.spec(ref['value']).get('placedAt', ''),
                                             TAG_RANK[ref['tag']], ref['value']))

    def deferral_reasons(self, node, tree):
        """Why each deferred order was deferred, replaying the running book."""
        argument = node['choiceArgument']
        before = self.seen.get(argument['ledger'], {}).get('createArgument', {}).get('book')
        if before is None:
            return {}
        assets, shares = Decimal(before['managedAssets']), Decimal(before['issuedShares'])
        outputs = {e['order']['value']: Decimal(e['amountOut']) for e in node['exerciseResult']['executed']}
        reasons = {}
        for ref in self.queue(argument['orders']):
            cid, tag, spec = ref['value'], ref['tag'], self.spec(ref['value'])
            if not spec:
                continue
            amount = Decimal(spec['amount'])
            if cid in outputs:
                if tag == 'RedeemRef':
                    assets, shares = assets - outputs[cid], shares - amount
                else:
                    assets += amount
                    if tag == 'DepositRef':
                        shares += outputs[cid]
                continue
            empty = assets == 0 and shares == 0
            if parse_time(tree['effectiveAt']) >= parse_time(spec['deadline']):
                reason = 'expired'
            elif tag == 'TopUpRef':
                reason = 'no shares yet'
            elif not (assets > 0 and shares > 0) and not (tag == 'DepositRef' and empty):
                reason = 'book cannot price it'
            else:
                if empty:
                    out = amount
                elif tag == 'DepositRef':
                    out = amount * shares / assets
                else:
                    out = amount * assets / shares
                out = out.quantize(TICK, rounding=ROUND_DOWN)
                reason = 'zero output' if out <= 0 else 'below minimum'
            reasons[cid] = reason
        return reasons

    def batch_card(self, node, tree, names, reasons):
        report, argument = node['exerciseResult'], node['choiceArgument']
        outputs = {e['order']['value']: e['amountOut'] for e in report['executed']}

        def book(cid):
            return self.seen.get(cid, {}).get('createArgument', {}).get('book')
        tiles = []
        for ref in self.queue(argument['orders']):
            cid, spec = ref['value'], self.spec(ref['value'])
            tiles.append({'cid': cid, 'owner': names(owner_of(spec)), 'kind': ref['tag'].removesuffix('Ref'),
                          'amount': spec.get('amount'), 'output': outputs.get(cid),
                          'status': 'executed' if cid in outputs else 'deferred', 'reason': reasons.get(cid)})
        return {'number': self.batch_count, 'time': tree['effectiveAt'], 'updateId': tree['updateId'],
                'commandId': tree.get('commandId', ''), 'executed': len(report['executed']),
                'deferred': len(report['deferred']), 'ledgerBefore': argument['ledger'],
                'ledgerAfter': report['ledger'], 'priceBefore': price_of(book(argument['ledger'])),
                'priceAfter': price_of(book(report['ledger'])), 'tiles': tiles}

    def created_line(self, event, names, in_batch):
        """A headline for a created contract, or None to keep it in the details."""
        name, data, cid = event['templateId'].split(':')[-1], event['createArgument'], event['contractId']
        if name.endswith('Order') and 'spec' in data:
            spec, kind = data['spec'], name.removesuffix('Order')
            unit = 'vUSD' if kind == 'Redeem' else 'USD'
            minimum = ''
            if kind == 'Deposit':
                minimum = f", at least {plain(spec['minShares'])} vUSD"
            elif kind == 'Redeem':
                minimum = f", at least {plain(spec['minAssets'])} USD"
            label = {'Deposit': 'deposit', 'Redeem': 'redeem', 'TopUp': 'top-up'}[kind]
            return 'order', (f"{names(owner_of(spec))} placed a {label} order: {plain(spec['amount'])} {unit}"
                             f"{minimum}, deadline {spec['deadline'][11:19]} UTC [{short(cid)}]")
        if name == 'VaultLedger':
            book = data['book']
            price = Decimal(price_of(book))
            self.prices.append({'time': time.time(), 'price': str(price)})
            return 'state', (f"Ledger rolled: {plain(book['managedAssets'])} USD, {plain(book['issuedShares'])} vUSD, "
                             f"price {price:.4f} [{short(cid)}]")
        if name == 'VaultRole':
            return 'role', f"Role given to {names(data['grantee'])} ({data['permission']}) [{short(cid)}]"
        if name == 'TokenAllocation' and not in_batch:
            spec = data['spec']
            legs = ', '.join(f"{plain(x['amount'])} {x['instrumentId']}" for x in spec['transferLegSides'])
            executors = ', '.join(names(p) for p in data['settlement']['executors'])
            return 'order', (f"{names(spec['authorizer']['owner'])} locked {legs} "
                             f"({'committed' if spec['committed'] else 'not committed'}, executor {executors}) [{short(cid)}]")
        return None

    def detail_line(self, event, names):
        name, data, cid = event['templateId'].split(':')[-1], event['createArgument'], event['contractId']
        if name == 'TokenHolding':
            h = data
            return (f"Holding: {names(h['owner'])}, {plain(h['amount'])} {h['instrument']}, "
                    f"{'locked' if h.get('lock') else 'free'} [{short(cid)}]")
        if name == 'TokenAllocation':
            spec = data['spec']
            legs = ', '.join(f"{plain(x['amount'])} {x['instrumentId']} {x['side']}" for x in spec['transferLegSides'])
            return f"Allocation: {names(spec['authorizer']['owner'])}, {legs} [{short(cid)}]"
        fields = ', '.join(f'{k}={v}' for k, v in data.items() if isinstance(v, (str, bool, int)))[:180]
        return f'{name} created [{short(cid)}] {fields}'

    def batch_lines(self, node, tree, names):
        report = node['exerciseResult']
        batcher = names(node['choiceArgument']['authorization']['actor'])
        lines = [f"Batch run by {batcher}: {len(report['executed'])} executed, {len(report['deferred'])} deferred."]
        for execution in report['executed']:
            ref = execution['order']
            spec = self.spec(ref['value'])
            if not spec:
                lines.append(f"Executed {short(ref['value'])}, out {plain(execution['amountOut'])}")
                continue
            who = names(owner_of(spec))
            if ref['tag'] == 'DepositRef':
                lines.append(f"{who} +{plain(execution['amountOut'])} vUSD")
            elif ref['tag'] == 'RedeemRef':
                lines.append(f"{who} -{plain(spec['amount'])} vUSD, paid {plain(execution['amountOut'])} USD")
            else:
                lines.append(f"Top-up: +{plain(spec['amount'])} USD from {who}")
        reasons = self.deferral_reasons(node, tree)
        for ref in report['deferred']:
            lines.append(f"Deferred {names(owner_of(self.spec(ref['value'])))} [{short(ref['value'])}], "
                         f"{reasons.get(ref['value'], 'not executed')}")
        self.batch_count += 1
        self.last_batch_at = time.time()
        self.batch_times.append(self.last_batch_at)
        self.batch_cards.appendleft(self.batch_card(node, tree, names, reasons))
        return lines

    def translate(self, tree, names):
        events = [next(iter(wrapper.values()))['value'] for wrapper in tree['eventsById'].values()]
        # Remember creates first, so exercises in the same tree can refer to them.
        for event in events:
            if 'createArgument' in event:
                self.seen[event['contractId']] = event
        in_batch = any(e.get('choice') == 'VaultManager_RunBatch' for e in events)
        headline, details, kind = [], [], 'ledger'
        for event in events:
            cid, name = event['contractId'], event['templateId'].split(':')[-1]
            if 'createArgument' in event:
                line = self.created_line(event, names, in_batch)
                if line:
                    line_kind, text = line
                    headline.append(text)
                    if line_kind != 'state' or kind == 'ledger':
                        kind = line_kind
                else:
                    details.append(self.detail_line(event, names))
                continue
            choice = event.get('choice', '')
            before = self.seen.get(cid, {}).get('createArgument', {})
            if choice == 'VaultManager_RunBatch':
                headline = self.batch_lines(event, tree, names) + headline
                kind = 'batch'
            elif choice in ('VaultManager_Halt', 'VaultManager_Resume'):
                verb = 'halted' if choice.endswith('Halt') else 'resumed'
                headline.append(f"Vault {verb} by {names(event['choiceArgument']['authorization']['actor'])}")
                kind = 'halt'
            elif choice == 'VaultRole_Revoke':
                headline.append(f"Role revoked: {names(before.get('grantee'))} "
                                f"({before.get('permission', '?')}) [{short(cid)}]")
                kind = 'role'
            elif choice.endswith('Order_Withdraw'):
                spec = before.get('spec', {})
                unit = 'vUSD' if name == 'RedeemOrder' else 'USD'
                headline.append(f"{names(owner_of(spec))} withdrew an expired order, "
                                f"{plain(spec.get('amount', 0))} {unit} unlocked [{short(cid)}]")
                kind = 'order'
            if event.get('consuming'):
                details.append(f'{name} archived by {choice} [{short(cid)}]')
        if not headline:
            headline, details = details[:3], details[3:]
        if headline:
            self.emit(kind, headline[0], command=tree.get('commandId', ''), lines=headline, details=details,
                      ledgerTime=tree['effectiveAt'], updateId=tree['updateId'], offset=tree['offset'])

    def follow(self, demo, stop):
        """Read every committed tree from the demo start, reconnecting from the last offset."""
        offset = demo.start_offset
        while not stop.is_set():
            connection = None
            parties = demo.all_parties()
            try:
                connection = demo.api.socket('/v2/updates/trees', {
                    'beginExclusive': offset, 'filter': {'filtersByParty': {p: {} for p in parties}},
                    'verbose': True}, timeout=1)
                self.connected = True
                pinged = time.monotonic()
                while not stop.is_set() and parties == demo.all_parties():
                    try:
                        raw = connection.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.monotonic() - pinged > 15:
                            connection.ping()
                            pinged = time.monotonic()
                        continue
                    if not raw:
                        raise RuntimeError('the ledger closed the stream')
                    row = json.loads(raw)
                    if 'update' not in row:
                        raise RuntimeError(str(row))
                    update = row['update']
                    if 'TransactionTree' in update:
                        tree = update['TransactionTree']['value']
                        if int(tree['offset']) > int(offset):
                            self.translate(tree, demo.name)
                            offset = tree['offset']
                    elif 'OffsetCheckpoint' in update:
                        offset = update['OffsetCheckpoint']['value']['offset']
            except Exception as error:
                self.connected = False
                self.emit('error', f'Ledger stream reconnecting: {str(error)[:300]}')
                stop.wait(2)
            finally:
                if connection:
                    connection.close()
