"""Lose the HTTP response of one real command and check it is not sent twice."""
import argparse
from pathlib import Path
import tempfile
import threading
import uuid

from world import ApiError, DemoVault, JsonApi
from sandbox_run import ROOT, package_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', default='http://127.0.0.1:18803')
    api = JsonApi(parser.parse_args().ledger)
    party = api.post('/v2/parties', {'partyIdHint': f'recovery-{uuid.uuid4().hex[:8]}'})['partyDetails']['party']
    vault = DemoVault.__new__(DemoVault)
    vault.folder, vault.api = Path(tempfile.mkdtemp(prefix='vault-recovery-')), api
    vault.custodian = vault.registrar = party
    vault.unresolved, vault.names = {}, {party: 'Recovery check'}
    vault.names_lock, vault.locks = threading.RLock(), {party: threading.RLock()}
    vault.emit = lambda *args, **kwargs: print(args, kwargs)
    real, sent = api.post, []

    def lose_response(path, body=None):
        result = real(path, body)
        if path == '/v2/commands/submit-and-wait-for-transaction-tree':
            sent.append(body['commandId'])
            api.post = real
            raise ApiError(503, {'message': 'response lost on purpose after the commit'})
        return result
    api.post = lose_response
    token = package_id(ROOT / 'packages/token/.daml/dist/canton-token-0.1.0.dar')
    tree = vault.submit(party, {'CreateCommand': {
        'templateId': f'{token}:CantonSolutions.Token:TokenRules',
        'createArguments': {'admin': party, 'maxLifetime': {'microseconds': '3600000000'}}}})
    assert tree['updateId'] and len(sent) == 1 and not vault.unresolved
    assert len(api.contracts([party])) == 1
    print('PASS a lost response was reconciled, one submission, one contract', tree['updateId'])


if __name__ == '__main__':
    main()
