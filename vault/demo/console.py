"""Read-only look at a running demo: active contracts, the price and transaction trees.

    console.py acs Alice            active contracts of a party (or "custodian")
    console.py price                current book and price
    console.py tx last-batch        the newest batch, condensed
    console.py tx <update id> --full

Nothing is ever submitted.
"""
import argparse
from decimal import Decimal
from pathlib import Path
import sys
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'batcher'))
from jsonapi import ApiError, JsonApi  # noqa: E402
from stream import LedgerStream  # noqa: E402

ALIASES = {'TokenHolding': 'Holding', 'TokenAllocation': 'Allocation'}
ORDERS = ('DepositOrder', 'RedeemOrder', 'TopUpOrder')


def plain(value):
    text = format(Decimal(str(value)), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def short(value):
    return (value or '')[:8]


def template(event):
    name = event['templateId'].split(':')[-1]
    return ALIASES.get(name, name)


def owner_of(spec):
    return spec.get('holder') or spec.get('sponsor')


class Console:
    def __init__(self, demo_url, ledger_url):
        self.demo = JsonApi(demo_url)
        self.state = self.demo.post('/api/state')
        if not self.state.get('ready'):
            raise ValueError('the demo is still starting')
        self.ledger = JsonApi(ledger_url or self.state.get('ledgerUrl', 'http://127.0.0.1:18803'))
        self.names = {w['party']: w['name'] for w in self.state['wallets']}
        sponsor = next(w['party'] for w in self.state['wallets'] if w['name'] == 'Sponsor')
        managers = [e for e in self.ledger.contracts([sponsor]) if e['templateId'].endswith(':VaultManager')]
        if len(managers) != 1:
            raise ValueError('expected one VaultManager, check --ledger')
        self.terms, self.manager = managers[0]['createArgument']['terms'], managers[0]['contractId']
        self.custodian = self.terms['custodian']
        self.names.update({self.custodian: 'Custodian', self.terms['underlying']['admin']: 'Registrar'})
        self.custody_events = self.ledger.contracts([self.custodian])
        self.cache = {e['contractId']: e for e in self.custody_events}
        roles = {r['cid']: r['name'] for r in self.state['roles']}
        for event in self.custody_events:
            if event['contractId'] in roles:
                self.names[event['createArgument']['grantee']] = roles[event['contractId']]
        self.lockers = self.allocation_locks(self.custody_events)

    def name(self, party):
        return self.names.get(party, (party or 'standard account').split('::')[0])

    @staticmethod
    def allocation_locks(events):
        return {held: e['contractId'] for e in events if template(e) == 'Allocation'
                for held in e['createArgument']['locked']}

    def describe(self, event):
        kind, data, cid = template(event), event['createArgument'], event['contractId']
        if kind in ORDERS:
            spec = data['spec']
            unit = 'vUSD' if kind == 'RedeemOrder' else 'USD'
            text = f"{self.name(owner_of(spec))}  {plain(spec['amount'])} {unit}"
            if kind == 'DepositOrder':
                text += f"  min {plain(spec['minShares'])} vUSD"
            elif kind == 'RedeemOrder':
                text += f"  min {plain(spec['minAssets'])} USD"
            return text + f"  deadline {spec['deadline'][11:19]}"
        if kind == 'Holding':
            h = data
            if not h.get('lock'):
                state = 'free'
            elif cid in self.lockers:
                state = 'locked by allocation ' + short(self.lockers[cid])
            else:
                state = 'locked'
            return f"{plain(h['amount'])} {h['instrument']}  owner {self.name(h['owner'])}  ({state})"
        if kind == 'Allocation':
            spec = data['spec']
            legs = ', '.join(f"{plain(x['amount'])} {x['instrumentId']} {x['side']}" for x in spec['transferLegSides'])
            executors = ', '.join(map(self.name, data['settlement']['executors']))
            return f"{legs}  executor {executors}  settle by {spec['settlementDeadline'][11:19]}"
        if kind == 'VaultLedger':
            book = data['book']
            issued = Decimal(book['issuedShares'])
            price = Decimal(book['managedAssets']) / issued if issued else Decimal(1)
            return (f"managed {plain(book['managedAssets'])}  issued {plain(book['issuedShares'])}  "
                    f"price {price:.4f}  halted {'yes' if data['halted'] else 'no'}  reserve {len(book['reserve'])} holdings")
        if kind == 'VaultRole':
            return f"{self.name(data['grantee'])}  {data['permission']}  from {self.name(data['custodian'])}"
        if 'config' in data:
            return 'admin ' + self.name(data['config'].get('admin'))
        return ''

    def acs(self, who, only):
        if who.lower() == 'custodian':
            party = self.custodian
        else:
            party = next((p for p, n in self.names.items() if n.lower() == who.lower()), None)
        if not party:
            raise ValueError(f'no party called {who}')
        events = self.custody_events if party == self.custodian else self.ledger.contracts([party])
        self.lockers.update(self.allocation_locks(events))
        rows = []
        for e in events:
            kind = template(e)
            if only and only not in (kind, e['templateId'].split(':')[-1]):
                continue
            order = '0' if kind == 'VaultLedger' else '2' if kind == 'VaultRole' else '1' + kind
            rows.append((order, kind, short(e['contractId']), self.describe(e)))
        print(f'{self.name(party)}: {len(rows)} active contracts')
        print(f"{'Template':<20} {'Contract':<9} Details")
        print('-' * 110)
        for _, kind, cid, text in sorted(rows):
            print(f'{kind:<20} {cid:<9} {text}'[:110])

    def created_event(self, cid):
        """The create event of a possibly archived contract, from the ledger's event lookup."""
        if cid not in self.cache:
            try:
                found = self.ledger.post('/v2/events/events-by-contract-id', {
                    'contractId': cid, 'eventFormat': {'filtersByParty': {self.custodian: {}}, 'verbose': True}})
                self.cache[cid] = found.get('created', {}).get('createdEvent')
            except ApiError:
                self.cache[cid] = None
        return self.cache[cid]

    def tree(self, update):
        query = urlencode({'parties': [self.custodian]}, doseq=True)
        return self.ledger.post(f'/v2/updates/transaction-tree-by-id/{update}?{query}')['transaction']

    def latest(self, which):
        if which == 'last-batch':
            cards = self.state.get('batchCards', [])
            if not cards:
                raise ValueError('no batch has run yet')
            return self.tree(cards[0]['updateId'])
        for update in dict.fromkeys(r['updateId'] for r in reversed(self.demo.post('/api/events')) if r.get('updateId')):
            try:
                return self.tree(update)
            except ApiError as error:
                if error.status != 404:
                    raise
        raise ValueError('no recent transaction is visible to the custodian')

    def condensed(self, events, tree):
        shown = 0
        for e in events:
            cid, choice = e['contractId'], e.get('choice', '')
            if 'createArgument' in e:
                if template(e) in (*ORDERS, 'VaultRole'):
                    print(f'create {template(e)} {short(cid)}  {self.describe(e)}')
                continue
            argument = e['choiceArgument']
            if choice.endswith('Order_Execute'):
                shown += 1
                if shown > 5:
                    continue
                created = self.created_event(cid)
                spec = created['createArgument']['spec'] if created else None
                print(f"  {choice} {short(cid)}" + (f"  {self.name(owner_of(spec))} {plain(spec['amount'])}" if spec else ''))
            elif choice == 'VaultLedger_Roll':
                created = self.created_event(cid)
                old = created['createArgument']['book'] if created else None
                new = argument['next']

                def pair(book):
                    return f"{plain(book['managedAssets'])}/{plain(book['issuedShares'])}" if book else '?'
                print(f"VaultLedger_Roll  book {pair(old)} to {pair(new)}  ledger {short(e['exerciseResult'])}")
            elif choice.startswith('VaultManager_') or choice == 'VaultRole_Revoke':
                role = f"  role {short(argument['authorization']['role'])}" if 'authorization' in argument else ''
                print(f"{choice}  by {', '.join(map(self.name, e['actingParties']))}{role}")
                if choice == 'VaultManager_RunBatch':
                    report = e['exerciseResult']
                    print(f"  {len(report['executed'])} executed, {len(report['deferred'])} deferred, "
                          f"input ledger {short(argument['ledger'])}")
                    if report['deferred']:
                        for ref in argument['orders']:
                            self.created_event(ref['value'])
                        self.created_event(argument['ledger'])
                        stream = LedgerStream()
                        stream.seen = {k: v for k, v in self.cache.items() if v}
                        reasons = stream.deferral_reasons(e, tree)
                        for ref in report['deferred']:
                            spec = (self.created_event(ref['value']) or {}).get('createArgument', {}).get('spec', {})
                            print(f"  deferred {self.name(owner_of(spec))} {plain(spec.get('amount', 0))}  "
                                  f"{reasons.get(ref['value'], 'not executed')}")
        if shown > 5:
            print(f'  {shown - 5} more order executions, see --full')

    def tx(self, which, full):
        tree = self.latest(which) if which in ('last', 'last-batch') else self.tree(which)
        print(f"Transaction {tree['updateId']}\nTime {tree['effectiveAt']}  command {tree.get('commandId') or '(not visible)'}")
        events = sorted((next(iter(w.values()))['value'] for w in tree['eventsById'].values()),
                        key=lambda e: int(e['nodeId']))
        if not full:
            self.condensed(events, tree)
            print(f'{len(events)} nodes in the full tree, add --full to see them all')
            return
        print('Seen as the custodian. Fetch nodes are not part of this API.')
        open_ends = []
        for e in events:
            while open_ends and int(e['nodeId']) > open_ends[-1]:
                open_ends.pop()
            indent = '  ' * len(open_ends)
            if 'createArgument' in e:
                print(f"{indent}create {template(e)} {short(e['contractId'])}  {self.describe(e)}")
                self.cache[e['contractId']] = e
            else:
                actors = ', '.join(map(self.name, e['actingParties']))
                print(f"{indent}exercise {e['choice']} {short(e['contractId'])}  by {actors}")
                open_ends.append(int(e['lastDescendantNodeId']))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--demo', default='http://127.0.0.1:8088')
    parser.add_argument('--ledger', help='ledger URL, if not the one the demo reports')
    parser.add_argument('--full', action='store_true', help='print every visible node')
    parser.add_argument('--template', help='only show this template in acs')
    parser.add_argument('command', choices=['acs', 'tx', 'price'])
    parser.add_argument('target', nargs='?')
    args = parser.parse_args()
    try:
        console = Console(args.demo, args.ledger)
        if args.command == 'price':
            current = next(e for e in console.custody_events if template(e) == 'VaultLedger'
                           and e['createArgument']['manager'] == console.manager)
            print(console.describe(current) + '  ledger ' + short(current['contractId']))
        elif not args.target:
            raise ValueError('give a party name or a transaction id')
        elif args.command == 'acs':
            console.acs(args.target, args.template)
        else:
            console.tx(args.target, args.full)
    except Exception as error:
        message = f'ledger request failed (HTTP {error.status})' if isinstance(error, ApiError) else str(error)
        print('Error: ' + ' '.join(message.split()), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
