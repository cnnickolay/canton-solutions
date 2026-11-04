"""Prove the network is a real cluster before benchmarking it.

1. Each participant has its own participant id.
2. A party allocated on one participant is known to the other, but not local to it.
3. A transaction crosses nodes: the registrar on the custodian participant offers
   tokens, the holder on the holders participant accepts. The resulting holding
   is signed by both, so both participants must confirm it, and it shows up on both.
"""
import argparse
import sys
import time

from bench import TOKEN_DAR, SHARDED_DAR, Node, created, package_id
from jsonapi import create_cmd, exercise_cmd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--custodian', required=True)
    parser.add_argument('--holders', required=True)
    args = parser.parse_args()
    c, h = Node(args.custodian), Node(args.holders)
    ids = [n.api.post('/v2/parties/participant-id')['participantId'] for n in (c, h)]
    print('participant ids:', ids)
    assert ids[0] != ids[1], 'both URLs reach the same participant'
    for node in (c, h):
        node.upload(SHARDED_DAR)
    registrar, holder = c.party('check-registrar'), h.party('check-holder')
    time.sleep(2)
    known = {p['party']: p['isLocal'] for p in c.api.post('/v2/parties')['partyDetails']}
    print('custodian participant knows the holder:', holder in known, 'local:', known.get(holder))
    assert holder in known and not known[holder], 'the holder should be known but remote'
    token = package_id(TOKEN_DAR)
    offer_t = f'{token}:CantonSolutions.Token:TokenOffer'
    tree = c.tree([registrar], create_cmd(offer_t, {'admin': registrar, 'owner': holder, 'instrument': 'USD',
                                                     'amount': '1'}))
    offer = created(tree, 'TokenOffer')[0]['contractId']
    deadline = time.monotonic() + 60
    while offer not in {e['createdEvent']['contractId'] for e in h.active([holder])}:
        assert time.monotonic() < deadline, 'the offer never reached the holders participant'
        time.sleep(0.5)
    started = time.monotonic()
    tree = h.tree([holder], exercise_cmd(offer_t, offer, 'TokenOffer_Accept', {}))
    print(f'holder accepted on its own participant in {time.monotonic() - started:.2f}s')
    holding = created(tree, 'TokenHolding')[0]['contractId']
    seen = holding in {e['createdEvent']['contractId'] for e in c.active([registrar])}
    print('holding signed by registrar and holder is visible on the custodian participant:', seen)
    assert seen
    print('CLUSTER OK')


if __name__ == '__main__':
    sys.exit(main())
