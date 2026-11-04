"""Drive a freshly started demo through its HTTP controls and check the results."""
import argparse
import json
import time
import urllib.request


class Demo:
    def __init__(self, url):
        self.url = url

    def call(self, path, data=None):
        body = json.dumps(data).encode() if data else None
        request = urllib.request.Request(self.url + path, body, {'Content-Type': 'application/json'})
        return json.load(urllib.request.urlopen(request, timeout=10))

    def state(self):
        return self.call('/api/state')

    def act(self, action, **fields):
        self.call('/api/action', dict(action=action, **fields))

    def wait(self, check, label, seconds=40):
        give_up = time.monotonic() + seconds
        while time.monotonic() < give_up:
            snapshot = self.state()
            if check(snapshot):
                print('PASS', label, flush=True)
                return snapshot
            time.sleep(.3)
        raise AssertionError(f'{label}: {json.dumps(self.state())}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8088')
    d = Demo(parser.parse_args().url)

    d.wait(lambda s: s.get('ready'), 'dashboard ready')
    holders = [w for w in d.state()['wallets'] if w['isHolder']]
    for h in holders[:3]:
        d.act('deposit', party=h['party'], amount='100', minimum='99')
    d.wait(lambda s: len(s['orders']) == 3, 'three deposit orders open')
    d.act('run')
    d.wait(lambda s: float(s['managedAssets']) == 300 and not s['orders'], 'first batch')
    d.act('topup', amount='30')
    d.wait(lambda s: len(s['orders']) == 1, 'top-up order open')
    d.act('run')
    d.wait(lambda s: float(s['price']) == 1.1, 'top-up raised the price')

    d.act('dust', party=holders[0]['party'])
    d.wait(lambda s: len(s['orders']) == 1, 'dust order open')
    before, count = d.state()['ledgerCid'], d.state()['batchCount']
    d.act('run')
    d.wait(lambda s: s['batchCount'] > count, 'dust batch reported')
    assert d.state()['ledgerCid'] == before
    d.act('donate', party=holders[1]['party'], amount='1')
    d.wait(lambda s: float(s['balance']['donations']) == 1, 'donation ignored by the book')
    assert d.state()['balance']['ok'] and float(d.state()['price']) == 1.1

    d.act('halt')
    d.wait(lambda s: s['halted'], 'halted')
    d.act('redeem', party=holders[2]['party'], amount='10', minimum='10')
    d.wait(lambda s: len(s['orders']) == 2, 'redeem accepted while halted')
    d.act('run')
    time.sleep(2)
    assert float(d.state()['issuedShares']) == 300
    d.act('resume')
    d.wait(lambda s: not s['halted'], 'resumed')
    d.act('run')
    d.wait(lambda s: float(s['issuedShares']) == 290, 'redeem executed')

    d.act('deposit', party=holders[1]['party'], amount='1', minimum='0', lifetime=5)
    s = d.wait(lambda s: len(s['orders']) == 2, 'short deadline order open')
    short_lived = next(o for o in s['orders'] if o['amount'] == '1.0000000000')
    time.sleep(6)
    d.act('withdraw', cid=short_lived['cid'])
    d.wait(lambda s: len(s['orders']) == 1, 'expired order withdrawn')

    for role, permission in [('Batcher', 'batch'), ('Warden', 'halt')]:
        old = next(r['cid'] for r in d.state()['roles'] if r['permission'] == permission)
        d.act('revoke', cid=old)
        d.wait(lambda s: all(r['cid'] != old for r in s['roles']), f'{role} role revoked')
        d.act('replace', role=role)
        d.wait(lambda s: any(r['permission'] == permission and r['cid'] != old for r in s['roles']),
               f'new {role.lower()} appointed')

    d.act('wallets', name='Dave', amount='50')
    d.wait(lambda s: any(w['name'] == 'Dave' and float(w['usd']) == 50 for w in s['wallets']), 'Dave funded')
    d.act('wallets', count=20, name='Load holder', amount='100')
    d.wait(lambda s: sum(w['name'].startswith('Load holder') and float(w['usd']) == 100 for w in s['wallets']) == 20,
           'twenty holders funded', seconds=90)
    for w in d.state()['wallets']:
        if w['name'].startswith('Load holder'):
            d.act('deposit', party=w['party'], amount='10', minimum='8')
    d.wait(lambda s: len(s['orders']) == 21, 'twenty deposits placed at once', seconds=60)
    d.act('run')
    d.wait(lambda s: len(s['orders']) == 1, 'twenty deposits executed in one batch', seconds=60)

    d.act('autorun', enabled=True)
    d.act('load', enabled=True, active=20, rate=5, mix=70, low='1', high='2', topUpEvery=6, topUpAmount='1')
    count = d.state()['batchCount']
    d.wait(lambda s: s['batchCount'] >= count + 5, 'batches keep running under load', seconds=180)
    d.act('load', enabled=False)
    d.wait(lambda s: not s['loading'], 'load stopped')
    time.sleep(6)
    d.act('autorun', enabled=False)
    d.wait(lambda s: not s['autorun'], 'batcher stopped')
    d.act('check')
    s = d.wait(lambda s: bool(s.get('batchCards')) and s['batchCards'][0]['ledgerAfter'] == s['ledgerCid'],
               'batch cards caught up with the ledger', seconds=60)
    assert s['balance']['ok'], s
    cards = s['batchCards']
    assert 0 < len(cards) <= 6
    assert all(len(c['tiles']) == c['executed'] + c['deferred'] for c in cards)
    assert [c['number'] for c in cards] == sorted([c['number'] for c in cards], reverse=True)
    print('PASS batch cards are consistent with the ledger', flush=True)

    rows = d.call('/api/events')
    text = [' '.join(r['lines']) for r in rows]
    assert any('Batch run by' in t for t in text)
    assert any('Role revoked' in t for t in text)
    assert any('withdrew an expired order' in t for t in text)
    errors = [r for r in rows if r['kind'] == 'error' and not r['lines'][0].startswith('Ledger stream reconnecting')]
    assert not errors, errors
    print('PASS readable ledger stream with no errors, book balanced', s['balance'], flush=True)


if __name__ == '__main__':
    main()
