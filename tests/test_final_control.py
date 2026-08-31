import copy
from datetime import timedelta
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_service import AISummaryService
from ai_summary import AISettings
from ai_summary import SummaryUnavailable
from control_state import ControlState, ControlStateError
from manual_delivery import ManualDeliveryError, build_test_plan, select_latest_requests
from matrix_commands import (ControlSecurityError, MatrixCommandListener,
                             command_from_event, validate_control_room)
from matrixbot import MatrixError
try:
    from test_integration import message
except ModuleNotFoundError:  # also support ``python -m unittest tests.test_final_control``
    from tests.test_integration import message


BOT = '@methodenbot:example.org'
USER = '@controller:example.org'
CONTROL = '!control:example.org'
PRODUCTION = '!production:example.org'


def room_state(extra_member=None):
    result = [
        {'type': 'm.room.member', 'state_key': BOT, 'content': {'membership': 'join'}},
        {'type': 'm.room.member', 'state_key': USER, 'content': {'membership': 'join'}},
        {'type': 'm.room.join_rules', 'state_key': '', 'content': {'join_rule': 'invite'}},
        {'type': 'm.room.guest_access', 'state_key': '', 'content': {'guest_access': 'forbidden'}},
        {'type': 'm.room.history_visibility', 'state_key': '', 'content': {'history_visibility': 'shared'}},
        {'type': 'm.room.power_levels', 'state_key': '', 'content': {
            'users': {BOT: 100, USER: 0}, 'users_default': 0, 'invite': 100,
            'state_default': 50, 'events': {'m.room.power_levels': 100}}},
    ]
    if extra_member:
        result.append({'type': 'm.room.member', 'state_key': extra_member,
                       'content': {'membership': 'join'}})
    return result


def config(tmp):
    return SimpleNamespace(
        matrix_console_room_id=CONTROL, matrix_room_id=PRODUCTION,
        matrix_control_user=USER, allow_unencrypted_control_dm=True,
        google_form_link='https://example.invalid/form?',
        control_state_dir=str(Path(tmp) / 'control'))


def event(event_id, body, *, sender=USER, extra=None):
    content = {'msgtype': 'm.text', 'body': body}
    if extra:
        content.update(extra)
    return {'event_id': event_id, 'type': 'm.room.message', 'sender': sender, 'content': content}


class FakeBot:
    def __init__(self):
        self.user_id = BOT
        self.state = room_state()
        self.sync_responses = []
        self.events = {}
        self.transaction_events = {}
        self.send_attempts = []

    def get_room_state(self, room):
        assert room == CONTROL
        return copy.deepcopy(self.state)

    def direct_mapping(self):
        return {USER: [CONTROL]}

    def sync(self, **kwargs):
        return copy.deepcopy(self.sync_responses.pop(0))

    def send_message(self, msg, room_id=None, thread_reply_to=None, html_msg=None, transaction_id=None):
        self.send_attempts.append(transaction_id)
        if transaction_id in self.transaction_events:
            return self.transaction_events[transaction_id]
        event_id = '$sent-' + str(len(self.transaction_events) + 1)
        content = {'msgtype': 'm.text', 'body': msg}
        if html_msg is not None:
            content.update(format='org.matrix.custom.html', formatted_body=html_msg)
        if thread_reply_to is not None:
            content['m.relates_to'] = {'rel_type': 'm.thread', 'event_id': thread_reply_to}
        self.events[room_id, event_id] = {'event_id': event_id, 'type': 'm.room.message',
                                          'sender': BOT, 'content': content}
        self.transaction_events[transaction_id] = event_id
        return event_id

    def read_event(self, room_id, event_id):
        return copy.deepcopy(self.events[room_id, event_id])


class Query:
    def __init__(self, values):
        self.values = list(values)

    def filter(self, datetime_received__gte):
        return Query([value for value in self.values if value.datetime_received >= datetime_received__gte])

    def order_by(self, field):
        assert field == '-datetime_received'
        return Query(sorted(self.values, key=lambda value: value.datetime_received, reverse=True))

    def only(self, *fields):
        return self

    def __getitem__(self, item):
        return self.values[item]


class Folder:
    folder_class = 'IPF.Note'

    def __init__(self, identity, values=()):
        self.id, self.values = identity, list(values)

    def all(self):
        return Query(self.values)

    def walk(self):
        return []


class TreeFolder(Folder):
    def __init__(self, identity, values=(), children=()):
        super().__init__(identity, values)
        self.children = list(children)

    def walk(self):
        return iter(self.children)


class Root:
    def __init__(self, correspondence):
        self.correspondence = correspondence

    def __floordiv__(self, name):
        return self.correspondence if name == 'Korrespondenz' else self


def exchange_with(count=3):
    values = []
    for index in range(count):
        item, _ = message()
        item.message_id = '<fixture-' + str(index) + '@example.invalid>'
        item.datetime_received = item.datetime_received + timedelta(minutes=index)
        values.append(item)
    correspondence = Folder('correspondence')
    return SimpleNamespace(inbox=Folder('inbox', values), root=Root(correspondence))


class FinalControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = config(self.directory.name)
        self.state = ControlState(self.config.control_state_dir)
        self.bot = FakeBot()
        self.ai = Mock()
        self.ai.check_available.return_value = True

    def listener(self):
        return MatrixCommandListener(self.bot, self.config, self.state, self.ai,
                                     account_factory=lambda: exchange_with())

    def test_only_exact_plain_commands_from_controller_are_accepted(self):
        self.assertEqual(command_from_event(event('$1', '  Test  '), self.config), ('$1', 'Test'))
        for candidate in (
                event('$2', 'test'), event('$3', 'Test', sender='@other:x'),
                event('$4', 'Test', extra={'format': 'org.matrix.custom.html', 'formatted_body': 'Test'}),
                event('$5', 'Test', extra={'m.relates_to': {'rel_type': 'm.replace'}}),
                {'event_id': '$6', 'type': 'm.reaction', 'sender': USER, 'content': {'body': 'Test'}}):
            self.assertIsNone(command_from_event(candidate, self.config))

    def test_first_sync_only_stores_cursor_and_ignores_old_commands(self):
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {CONTROL: {
            'timeline': {'events': [event('$old', 'Test')]}}}}}]
        self.assertTrue(self.listener().bootstrap())
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['since'], 's1')
        self.assertEqual(snapshot['queue'], [])
        self.assertEqual(self.bot.send_attempts, [])
        self.ai.render.assert_not_called()

    def test_commands_in_one_sync_observe_toggle_order_and_persist_final_off(self):
        self.state.bootstrap('s0')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {CONTROL: {'timeline': {
            'events': [event('$1', 'KI an'), event('$2', 'Test'),
                       event('$3', 'KI aus'), event('$4', 'Test')]}}}}}]
        seen = []

        def plan(_exchange, config, _ai, *, command, command_event_id, ai_enabled):
            seen.append(ai_enabled)
            return [{'msg': command, 'html_msg': '<p>' + command + '</p>', 'room_id': CONTROL,
                     'thread_root_part': None, 'transaction_id': 'tx-' + command_event_id[1:],
                     'event_id': None}]

        with patch('matrix_commands.build_test_plan', side_effect=plan):
            self.listener().poll_once(timeout_ms=0)
        self.assertEqual(seen, [True, False])
        self.assertFalse(self.state.snapshot()['ai_enabled'])
        self.assertEqual(self.state.snapshot()['queue'], [])
        self.assertEqual(self.state.snapshot()['completed'], ['$1', '$2', '$3', '$4'])

    def test_failed_ki_an_forces_safe_off_state_and_sends_private_notice(self):
        self.state.bootstrap('s0')
        self.state.record_sync('s1', [('$off', 'KI aus')])
        self.listener().drain()
        self.state.record_sync('s2', [('$on', 'KI an')])
        self.ai.check_available.side_effect = SummaryUnavailable('key_file_unreadable')
        self.listener().drain()
        self.assertFalse(self.state.snapshot()['ai_enabled'])
        transaction = 'control-' + hashlib.sha256(b'$on').hexdigest()[:40] + '-0'
        notice = self.bot.events[CONTROL, self.bot.transaction_events[transaction]]['content']['body']
        self.assertIn('nicht eingeschaltet', notice)

    def test_unsafe_room_stops_before_any_command(self):
        self.bot.state = room_state('@third:example.org')
        with self.assertRaises(ControlSecurityError):
            validate_control_room(self.bot, self.config)
        self.assertEqual(self.bot.send_attempts, [])

    def test_controller_cannot_have_power_to_enable_encryption_later(self):
        for state_event in self.bot.state:
            if state_event.get('type') == 'm.room.power_levels':
                state_event['content']['events']['m.room.encryption'] = 0
        with self.assertRaisesRegex(ControlSecurityError, 'controller_can_change_security_state'):
            validate_control_room(self.bot, self.config)

    def test_m_direct_is_only_a_signal_not_authorization(self):
        self.bot.direct_mapping = lambda: {}
        self.assertTrue(validate_control_room(self.bot, self.config))

    def test_test_plan_uses_three_private_threads_and_ai_off_calls_no_model(self):
        factory = Mock(side_effect=AssertionError('model must not be constructed'))
        service = AISummaryService(AISettings(True, True, api_key='SYNTHETIC'),
                                   summarizer_factory=factory)
        parts = build_test_plan(exchange_with(), self.config, service, command='Test',
                                command_event_id='$command', ai_enabled=False)
        self.assertEqual(len(parts), 6)
        roots = [part for part in parts if part['thread_root_part'] is None]
        self.assertEqual(len(roots), 3)
        self.assertTrue(all(part['room_id'] == CONTROL and part['msg'].startswith('Test · ')
                            for part in roots))
        self.assertTrue(all(part['room_id'] == CONTROL for part in parts))
        factory.assert_not_called()

    def test_test_two_targets_production_and_contains_ai_then_private_ack(self):
        service = Mock()
        service.render.return_value = {'msg': '### KI-Zusammenfassung', 'html_msg': '<h3>KI</h3>'}
        parts = build_test_plan(exchange_with(1), self.config, service, command='Test 2',
                                command_event_id='$command2', ai_enabled=True)
        self.assertEqual(len(parts), 4)
        self.assertTrue(parts[0]['msg'].startswith('Techniktest'))
        self.assertEqual([part['room_id'] for part in parts[:3]], [PRODUCTION] * 3)
        self.assertEqual(parts[1]['thread_root_part'], 0)
        self.assertEqual(parts[2]['thread_root_part'], 0)
        self.assertEqual(parts[3]['room_id'], CONTROL)
        service.render.assert_called_once()

    def test_latest_three_are_global_distinct_and_sorted_across_mail_folders(self):
        items = []
        for index in range(4):
            item, _ = message()
            item.message_id = '<global-' + str(index) + '@example.invalid>'
            item.datetime_received += timedelta(minutes=index)
            items.append(item)
        nested = Folder('nested', [items[2]])
        correspondence = TreeFolder('correspondence', [items[1]], [nested])
        exchange = SimpleNamespace(inbox=Folder('inbox', [items[0], items[3]]),
                                   root=Root(correspondence))
        selected = select_latest_requests(exchange, 3)
        self.assertEqual([item.message_id for item in selected],
                         ['<global-3@example.invalid>', '<global-2@example.invalid>',
                          '<global-1@example.invalid>'])

    def test_conflicting_duplicate_aborts_before_ai_or_rendering(self):
        first, _ = message()
        duplicate, _ = message()
        duplicate.body += '<p>different</p>'
        duplicate.datetime_received = first.datetime_received
        correspondence = TreeFolder('correspondence', [duplicate])
        exchange = SimpleNamespace(inbox=Folder('inbox', [first]), root=Root(correspondence))
        with self.assertRaisesRegex(ManualDeliveryError, 'conflicting_request_copies'):
            build_test_plan(exchange, self.config, self.ai, command='Test 2',
                            command_event_id='$conflict', ai_enabled=True)
        self.ai.render.assert_not_called()

    def test_crash_after_matrix_acceptance_reuses_transaction_without_duplicate(self):
        self.state.bootstrap('s0')
        self.state.record_sync('s1', [('$command', 'Test')])
        part = {'msg': 'Test', 'html_msg': '<p>Test</p>', 'room_id': CONTROL,
                'thread_root_part': None, 'transaction_id': 'stable-transaction', 'event_id': None}
        self.state.plan_head('$command', False, [part])
        listener = self.listener()
        original = self.state.record_part
        first = True

        def crash_once(*args):
            nonlocal first
            if first:
                first = False
                raise ControlStateError('synthetic_crash')
            return original(*args)

        self.state.record_part = crash_once
        with self.assertRaisesRegex(ControlStateError, 'synthetic_crash'):
            listener.drain()
        self.state.record_part = original
        listener.drain()
        self.assertEqual(self.bot.send_attempts, ['stable-transaction', 'stable-transaction'])
        self.assertEqual(len(self.bot.transaction_events), 1)
        self.assertEqual(self.state.snapshot()['queue'], [])

    def test_permanent_partial_delivery_is_closed_with_distinct_private_warning(self):
        self.state.bootstrap('s0')
        self.state.record_sync('s1', [('$bad', 'Test 2'), ('$off', 'KI aus')])
        parts = [
            {'msg': 'Techniktest', 'html_msg': '<p>Techniktest</p>', 'room_id': PRODUCTION,
             'thread_root_part': None, 'transaction_id': 'root-tx', 'event_id': None},
            {'msg': 'Details', 'html_msg': '<p>Details</p>', 'room_id': PRODUCTION,
             'thread_root_part': 0, 'transaction_id': 'detail-tx', 'event_id': None},
        ]
        original_send = self.bot.send_message

        def reject_detail(*args, **kwargs):
            if kwargs.get('transaction_id') == 'detail-tx':
                raise MatrixError('matrix_http_error', status=413)
            return original_send(*args, **kwargs)

        self.bot.send_message = reject_detail
        with patch('matrix_commands.build_test_plan', return_value=copy.deepcopy(parts)):
            self.listener().drain()
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['queue'], [])
        self.assertEqual(snapshot['completed'], ['$bad', '$off'])
        self.assertEqual(self.bot.send_attempts.count('root-tx'), 1)
        failure_txs = [value for value in self.bot.send_attempts if value.endswith('-delivery-failed')]
        self.assertEqual(len(failure_txs), 1)
        self.assertNotEqual(failure_txs[0], 'root-tx')
        failure_event = self.bot.transaction_events[failure_txs[0]]
        self.assertIn('Teilzustellung', self.bot.events[CONTROL, failure_event]['content']['body'])


if __name__ == '__main__':
    unittest.main()
