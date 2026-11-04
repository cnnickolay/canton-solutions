"""Unit tests for the batcher, with the ledger replaced by mocks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from jsonapi import ApiError, JsonApi
from runner import MANAGER_MODULE, ROLES_MODULE, BatchRunner, Unresolved, error_ids

FAR = '9999-01-01T00:00:00Z'


def ledger(cid):
    return {'contractId': cid, 'createArgument': {'halted': False}}


def manager():
    return {'templateId': 'manager', 'contractId': 'manager'}


def report(deferred):
    """Transaction tree of a batch that deferred these deposit orders."""
    node = {'choice': 'VaultManager_RunBatch', 'contractId': 'manager',
            'exerciseResult': {'deferred': [{'tag': 'DepositRef', 'value': cid} for cid in deferred]}}
    return {'transaction': {'eventsById': {'0': {'ExercisedTreeEvent': {'value': node}}}}}


def completion(command_id):
    return json.dumps({'completionResponse': {'Completion': {'value': {
        'commandId': command_id, 'status': {'code': 0}, 'updateId': 'committed'}}}})


def event(name, cid, argument):
    return {'templateId': f'pkg:{MANAGER_MODULE}:{name}', 'contractId': cid, 'createArgument': argument}


def manager_event():
    return event('VaultManager', 'manager', {'terms': {'custodian': 'c'}})


def ledger_event():
    return event('VaultLedger', 'ledger', {'manager': 'manager', 'halted': False})


def role_event(**changes):
    argument = {'custodian': 'c', 'grantee': 'b', 'vault': 'manager', 'permission': 'batch', **changes}
    return {'templateId': f'pkg:{ROLES_MODULE}:VaultRole', 'contractId': 'role', 'createArgument': argument}


def sent_orders(runner):
    return runner.submit.call_args.args[0]['ExerciseCommand']['choiceArgument']['orders']


class ErrorIdTests(unittest.TestCase):
    def test_only_structured_ids_count(self):
        for body in ({'code': 'CONTRACT_NOT_FOUND'},
                     {'status': {'details': [{'valueDecoded': {'reason': 'CONTRACT_NOT_FOUND'}}]}}):
            self.assertTrue(BatchRunner.retryable(ApiError(409, body)))
        self.assertFalse(BatchRunner.retryable(ApiError(400, {
            'message': 'CONTRACT_NOT_FOUND', 'code': 'DAML_FAILURE'})))
        wrong = 'canton-solutions/vault-role-wrong-scope'
        error = ApiError(400, {'status': {'details': [
            {'reason': 'DAML_FAILURE', 'metadata': {'error_id': wrong}}]}})
        self.assertIn(wrong, error_ids(error))
        self.assertFalse(BatchRunner.retryable(error))
        self.assertFalse(error_ids(ApiError(400, {'message': wrong})))
        self.assertIn(wrong, error_ids(ApiError(400, {'context': {'error_id': wrong}})))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.api = Mock()
        self.api.url = 'http://localhost'
        self.api.offset.return_value = 7
        self.api.post.return_value = report([])
        self.path = Path(folder.name) / 'journal.json'
        self.runner = BatchRunner(self.api, {
            'custodian': 'c', 'batcher': 'b', 'user_id': 'test', 'batch_role': 'role',
            'completion_timeout': .01}, self.path)

    def journal(self):
        return json.loads(self.path.read_text())

    def test_deferred_orders_wait_for_a_new_ledger(self):
        waiting = [('0', 'dust', 'Deposit'), ('1', 'greedy', 'Redeem')]
        self.runner.view = Mock(return_value=(manager(), ledger('l1'), waiting))
        self.runner.submit = Mock(return_value={'updateId': 'committed'})
        self.api.post.return_value = report(['dust', 'greedy'])
        self.assertEqual(self.runner.tick()['orders'], 2)
        self.assertEqual(self.runner.tick()['status'], 'idle')
        self.assertEqual(self.runner.tick()['status'], 'idle')
        self.assertEqual(self.runner.submit.call_count, 1)
        self.runner.max_orders = 1
        self.runner.view.return_value = (manager(), ledger('l1'), waiting + [('2', 'fresh', 'Deposit')])
        self.api.post.return_value = report([])
        self.assertEqual(self.runner.tick()['orders'], 1)
        self.assertEqual(sent_orders(self.runner), [{'tag': 'DepositRef', 'value': 'fresh'}])
        self.runner.view.return_value = (manager(), ledger('l2'), waiting)
        self.assertEqual(self.runner.tick()['orders'], 1)
        self.assertEqual(sent_orders(self.runner), [{'tag': 'DepositRef', 'value': 'dust'}])

    def test_reconciled_batch_fills_the_deferred_set(self):
        command = {'ExerciseCommand': {'contractId': 'manager', 'choiceArgument': {'ledger': 'l1'}}}
        self.runner.journal.write({'pending': True, 'command_id': 'old', 'offset': 7,
                                   'identity': self.runner.identity(), 'body': {'commands': [command]}})
        self.api.socket.return_value.recv.return_value = completion('old')
        self.api.post.return_value = report(['dust'])
        self.assertEqual(self.runner.tick()['status'], 'reconciled')
        self.runner.view = Mock(return_value=(manager(), ledger('l1'), [('0', 'dust', 'Deposit')]))
        self.assertEqual(self.runner.tick()['status'], 'idle')
        self.assertEqual(self.api.post.call_count, 1)
        self.assertIn('/transaction-tree-by-id/committed?', self.api.post.call_args.args[0])

    def test_failed_report_lookup_never_resubmits(self):
        self.runner.view = Mock(return_value=(manager(), ledger('l1'), [('0', 'dust', 'Deposit')]))
        self.runner.submit = Mock(return_value={'updateId': 'committed'})
        self.api.post.side_effect = [ApiError(503, 'lookup unavailable'), report(['dust'])]
        with self.assertRaises(ApiError):
            self.runner.tick()
        self.assertEqual(self.runner.tick()['status'], 'idle')
        self.assertEqual(self.runner.submit.call_count, 1)

    def test_timeout_is_reconciled_not_resent(self):
        self.api.post.side_effect = ApiError(503, {'message': 'timeout'})

        def stream(*args, **kwargs):
            connection = Mock()
            connection.recv.return_value = completion(self.journal()['command_id'])
            return connection
        self.api.socket.side_effect = stream
        self.assertEqual(self.runner.submit({})['updateId'], 'committed')
        self.assertEqual(self.api.post.call_count, 1)
        self.assertFalse(self.journal()['pending'])

    def test_unknown_outcome_blocks_after_restart(self):
        self.runner.journal.write({'pending': True, 'command_id': 'old', 'offset': 7,
                                   'identity': self.runner.identity()})
        self.api.socket.return_value.recv.return_value = ''
        with self.assertRaises(Unresolved):
            self.runner.tick()
        self.api.post.assert_not_called()
        self.api.contracts.assert_not_called()
        self.assertTrue(self.journal()['pending'])

    def test_contention_rereads_the_ledger_and_drops_gone_orders(self):
        self.runner.view = Mock(side_effect=[
            (manager(), ledger('old'), [('0', 'gone', 'Deposit'), ('1', 'live', 'Deposit')]),
            (manager(), ledger('new'), [('1', 'live', 'Deposit'), ('2', 'later', 'Deposit')])])
        self.runner.submit = Mock(side_effect=[
            ApiError(409, {'code': 'LOCAL_VERDICT_LOCKED_CONTRACTS'}), {'updateId': 'ok'}])
        self.assertEqual(self.runner.tick()['orders'], 1)
        argument = self.runner.submit.call_args.args[0]['ExerciseCommand']['choiceArgument']
        self.assertEqual(argument['ledger'], 'new')
        self.assertEqual(argument['authorization'], {'actor': 'b', 'role': 'role'})
        self.assertEqual(argument['orders'], [{'tag': 'DepositRef', 'value': 'live'}])

    def test_view_skips_spent_allocations_and_keeps_the_oldest(self):
        def order(cid, allocation, placed):
            return event('DepositOrder', cid, {'spec': {
                'manager': 'manager', 'custodian': 'c', 'allocation': allocation,
                'placedAt': placed, 'deadline': FAR}})
        self.api.contracts.return_value = [
            manager_event(), ledger_event(), role_event(),
            order('newer', 'shared', '2'), order('dead', 'spent', '0'),
            order('oldest', 'shared', '1'), order('other', 'live', '3'),
            {'templateId': 'token:Allocation', 'contractId': 'shared'},
            {'templateId': 'token:Allocation', 'contractId': 'live'}]
        self.assertEqual(self.runner.view()[2], [('1', 'oldest', 'Deposit'), ('3', 'other', 'Deposit')])
        self.api.contracts.return_value = [
            e for e in self.api.contracts.return_value if e['contractId'] != 'shared']
        self.assertEqual(self.runner.view()[2], [('3', 'other', 'Deposit')])

    def test_view_refuses_a_missing_or_wrong_role(self):
        self.api.contracts.return_value = [manager_event(), ledger_event()]
        with self.assertRaisesRegex(RuntimeError, 'not active or not visible'):
            self.runner.view()
        wrong = [role_event(custodian='other'), role_event(grantee='other'),
                 role_event(permission='halt'), role_event(vault='other')]
        for role in wrong:
            self.api.contracts.return_value = [manager_event(), ledger_event(), role]
            with self.assertRaisesRegex(RuntimeError, 'does not match'):
                self.runner.view()

    def test_new_role_still_reconciles_the_old_command_first(self):
        body = {'authorization': {'actor': 'b', 'role': 'old-role'}}
        self.runner.journal.write({'pending': True, 'command_id': 'old', 'offset': 7,
                                   'identity': self.runner.identity(), 'body': body})
        self.runner.config['batch_role'] = 'new-role'
        self.api.socket.return_value.recv.return_value = completion('old')
        self.assertEqual(self.runner.tick()['status'], 'reconciled')
        self.api.post.assert_not_called()
        self.api.contracts.assert_not_called()
        self.assertEqual(self.journal()['body'], body)

    def test_halt_during_submission_keeps_the_orders(self):
        self.runner.view = Mock(return_value=(manager(), ledger('l'), [('0', 'order', 'Deposit')]))
        self.runner.submit = Mock(side_effect=ApiError(400, {'details': [
            {'reason': 'DAML_FAILURE', 'metadata': {'error_id': 'canton-solutions/vault-halted'}}]}))
        self.assertEqual(self.runner.tick()['status'], 'halted')
        self.assertEqual(self.runner.submit.call_count, 1)


class JsonApiTests(unittest.TestCase):
    def test_reads_more_than_200_contracts(self):
        api = JsonApi('http://localhost')
        connection = Mock()
        connection.recv.side_effect = [json.dumps({'contractEntry': {'JsActiveContract': {
            'createdEvent': {'contractId': str(i)}}}}) for i in range(250)] + ['']
        api.socket = Mock(return_value=connection)
        self.assertEqual(len(api.contracts(['c'], offset=7)), 250)


if __name__ == '__main__':
    unittest.main()
