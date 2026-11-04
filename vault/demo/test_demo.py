"""Unit tests for journaled actions and the readable ledger stream."""
import json
from pathlib import Path
import tempfile
import threading
import unittest

from stream import LedgerStream
from world import ApiError, DemoVault, Unresolved


class Completions:
    def __init__(self, row):
        self.row = row

    def recv(self):
        return json.dumps(self.row) if self.row else ''

    def close(self):
        pass


class LostResponses:
    """Every submission commits, but its HTTP response is lost."""

    def __init__(self, known=True):
        self.posts = 0
        self.known = known
        self.url = 'http://localhost'

    def offset(self):
        return 12

    def post(self, path, body=None):
        if path.startswith('/v2/commands/'):
            self.posts += 1
            self.command = body['commandId']
            raise ApiError(503, {'message': 'response lost after commit'})
        return {'transaction': {'updateId': 'committed'}}

    def socket(self, path, body, timeout):
        if not self.known:
            return Completions(None)
        return Completions({'completionResponse': {'Completion': {'value': {
            'commandId': self.command, 'updateId': 'committed', 'status': {'code': 0}}}}})


def names(party):
    return party


def vault(folder, known):
    v = DemoVault.__new__(DemoVault)
    v.folder, v.api = Path(folder), LostResponses(known)
    v.custodian, v.registrar, v.unresolved = 'custodian', 'registrar', {}
    v.names = {'alice': 'Alice'}
    v.names_lock = threading.RLock()
    v.locks = {'alice': threading.RLock()}
    v.emit = lambda *args, **kwargs: None
    return v


def journal(folder):
    return json.loads(next(Path(folder).glob('*.json')).read_text())


class ActionTests(unittest.TestCase):
    def test_lost_response_is_reconciled_not_resent(self):
        with tempfile.TemporaryDirectory() as folder:
            v = vault(folder, True)
            self.assertEqual(v.submit('alice', {'command': {}})['updateId'], 'committed')
            self.assertEqual(v.api.posts, 1)
            self.assertFalse(v.unresolved)
            self.assertFalse(journal(folder)['pending'])

    def test_unknown_outcome_blocks_the_party(self):
        with tempfile.TemporaryDirectory() as folder:
            v = vault(folder, False)
            with self.assertRaises(Unresolved):
                v.submit('alice', {'command': {}})
            with self.assertRaises(Unresolved):
                v.submit('alice', {'another': {}})
            self.assertEqual(v.api.posts, 1)
            self.assertTrue(journal(folder)['pending'])


def deposit_spec(amount, minimum='0', placed='2026-01-01T00:00:00Z'):
    return {'spec': {'holder': 'Alice', 'amount': amount, 'minShares': minimum,
                     'placedAt': placed, 'deadline': '2026-01-02T00:00:00Z'}}


class StreamTests(unittest.TestCase):
    def test_deferral_reasons_replay_the_running_book(self):
        s = LedgerStream()
        s.seen['ledger'] = {'createArgument': {'book': {'managedAssets': '11', 'issuedShares': '10'}}}
        refs = []
        for cid, amount, minimum in [('fills', '1', '0'), ('dust', '.0000000001', '0'), ('greedy', '11', '10')]:
            refs.append({'tag': 'DepositRef', 'value': cid})
            s.seen[cid] = {'createArgument': deposit_spec(amount, minimum)}
        node = {'choiceArgument': {'ledger': 'ledger', 'orders': list(reversed(refs))},
                'exerciseResult': {'executed': [{'order': refs[0], 'amountOut': '.9090909090'}]}}
        reasons = s.deferral_reasons(node, {'effectiveAt': '2026-01-01T00:01:00Z'})
        self.assertEqual(reasons, {'dust': 'zero output', 'greedy': 'below minimum'})

    def test_top_up_line_names_the_sponsor_and_no_minimum(self):
        s = LedgerStream()
        spec = {'sponsor': 'Sponsor', 'amount': '5', 'deadline': '2026-01-02T00:00:00Z'}
        event = {'templateId': 'pkg:Vault:TopUpOrder', 'contractId': 'top-up', 'createArgument': {'spec': spec}}
        s.translate({'eventsById': {'0': {'CreatedTreeEvent': {'value': event}}},
                     'effectiveAt': '2026-01-01T00:00:00Z', 'updateId': 'tx', 'offset': 1}, names)
        line = s.rows[-1]['lines'][0]
        self.assertIn('Sponsor placed a top-up order: 5 USD', line)
        self.assertNotIn('at least', line)

    def test_batch_card_shows_executions_deferrals_and_ledger_move(self):
        s = LedgerStream()
        for cid, assets, shares in [('before', '100', '100'), ('after', '111', '101')]:
            s.seen[cid] = {'createArgument': {'book': {'managedAssets': assets, 'issuedShares': shares}}}
        refs = []
        for cid, kind, owner, amount in [('deposit', 'Deposit', 'Alice', '2'), ('redeem', 'Redeem', 'Bob', '1'),
                                         ('deferred', 'Deposit', 'Carol', '1'), ('top-up', 'TopUp', 'Sponsor', '10')]:
            refs.append({'tag': kind + 'Ref', 'value': cid})
            who = 'sponsor' if kind == 'TopUp' else 'holder'
            s.seen[cid] = {'createArgument': {'spec': {who: owner, 'amount': amount, 'placedAt': cid}}}
        executed = [{'order': refs[i], 'amountOut': out} for i, out in [(0, '2'), (1, '1'), (3, '0')]]
        node = {'choiceArgument': {'ledger': 'before', 'orders': list(reversed(refs))},
                'exerciseResult': {'ledger': 'after', 'executed': executed, 'deferred': [refs[2]]}}
        tree = {'effectiveAt': '2026-01-01T00:00:00Z', 'updateId': 'tx'}
        card = s.batch_card(node, tree, names, {'deferred': 'below minimum'})
        self.assertEqual((card['executed'], card['deferred']), (3, 1))
        self.assertEqual((card['ledgerBefore'], card['ledgerAfter']), ('before', 'after'))
        self.assertEqual(card['priceBefore'], '1')
        self.assertAlmostEqual(float(card['priceAfter']), 111 / 101)
        self.assertEqual(card['tiles'][-1]['kind'], 'TopUp')
        self.assertEqual(card['tiles'][-1]['owner'], 'Sponsor')
        self.assertEqual(next(t for t in card['tiles'] if t['cid'] == 'deferred')['reason'], 'below minimum')
        node['exerciseResult'] = {'ledger': 'before', 'executed': [], 'deferred': refs}
        card = s.batch_card(node, tree, names, {})
        self.assertEqual(card['priceBefore'], card['priceAfter'])
        self.assertEqual(card['ledgerBefore'], card['ledgerAfter'])
        self.assertTrue(all(t['status'] == 'deferred' for t in card['tiles']))


if __name__ == '__main__':
    unittest.main()
