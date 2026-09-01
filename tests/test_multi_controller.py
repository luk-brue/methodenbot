import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from control_state import COMMANDS, ControlState
from main import controller_state_directory
from manual_delivery import (build_delivery_failure_ack, build_failure_ack,
                             build_test_plan, build_test_wait_ack, build_toggle_ack)
from matrix_commands import (ControlSecurityError, MatrixCommandListener,
                             MatrixCommandWorker)

try:
    from test_final_control import exchange_with
except ModuleNotFoundError:  # also support ``python -m unittest tests.test_multi_controller``
    from tests.test_final_control import exchange_with


BOT = '@methodenbot:example.invalid'
PRIMARY_USER = '@primary:example.invalid'
PRIMARY_ROOM = '!primary:example.invalid'
SECONDARY_USER = '@secondary:example.invalid'
SECONDARY_ROOM = '!secondary:example.invalid'
THIRD_USER = '@third:example.invalid'
THIRD_ROOM = '!third:example.invalid'
OTHER_USER = '@other:example.invalid'
PRODUCTION_ROOM = '!production:example.invalid'


def config(directory):
    return SimpleNamespace(
        matrix_console_room_id=PRIMARY_ROOM,
        matrix_room_id=PRODUCTION_ROOM,
        matrix_control_user=PRIMARY_USER,
        allow_unencrypted_control_dm=True,
        google_form_link='https://form.example.invalid/?',
        control_state_dir=str(Path(directory) / 'control'))


def event(event_id, body, sender, *, extra=None):
    content = {'msgtype': 'm.text', 'body': body}
    if extra:
        content.update(extra)
    return {'event_id': event_id, 'type': 'm.room.message',
            'sender': sender, 'content': content}


def room_state(controller, *, extra_member=None):
    result = [
        {'type': 'm.room.member', 'state_key': BOT,
         'content': {'membership': 'join'}},
        {'type': 'm.room.member', 'state_key': controller,
         'content': {'membership': 'join'}},
        {'type': 'm.room.join_rules', 'state_key': '',
         'content': {'join_rule': 'invite'}},
        {'type': 'm.room.guest_access', 'state_key': '',
         'content': {'guest_access': 'forbidden'}},
        {'type': 'm.room.history_visibility', 'state_key': '',
         'content': {'history_visibility': 'shared'}},
        {'type': 'm.room.power_levels', 'state_key': '', 'content': {
            'users': {BOT: 100, controller: 0},
            'users_default': 0,
            'invite': 100,
            'state_default': 50,
            'events': {'m.room.power_levels': 100},
        }},
    ]
    if extra_member is not None:
        result.append({'type': 'm.room.member', 'state_key': extra_member,
                       'content': {'membership': 'join'}})
    return result


def sync_response(cursor, room_id, events, *, other_rooms=None):
    joined = {room_id: {'timeline': {'events': list(events)}}}
    joined.update(other_rooms or {})
    return {'next_batch': cursor, 'rooms': {'join': joined}}


class FakeBot:
    def __init__(self, states):
        self.user_id = BOT
        self.states = copy.deepcopy(states)
        self.direct = {event['state_key']: [room]
                       for room, values in self.states.items()
                       for event in values
                       if (event.get('type') == 'm.room.member'
                           and event.get('state_key') != BOT
                           and event.get('content', {}).get('membership') == 'join')}
        self.sync_responses = []
        self.sync_calls = []
        self.on_sync = None
        self.events = {}
        self.transaction_events = {}
        self.send_attempts = []

    def get_room_state(self, room_id):
        return copy.deepcopy(self.states.get(room_id, []))

    def direct_mapping(self):
        return copy.deepcopy(self.direct)

    def sync(self, **kwargs):
        self.sync_calls.append(copy.deepcopy(kwargs))
        response = copy.deepcopy(self.sync_responses.pop(0))
        if self.on_sync is not None:
            self.on_sync()
        return response

    def send_message(self, msg, room_id=None, thread_reply_to=None, html_msg=None,
                     transaction_id=None):
        key = (room_id, transaction_id)
        self.send_attempts.append(key)
        if key in self.transaction_events:
            return self.transaction_events[key]
        event_id = '$sent-' + str(len(self.transaction_events) + 1)
        content = {'msgtype': 'm.text', 'body': msg}
        if html_msg is not None:
            content.update(format='org.matrix.custom.html', formatted_body=html_msg)
        if thread_reply_to is not None:
            content['m.relates_to'] = {'rel_type': 'm.thread',
                                       'event_id': thread_reply_to}
        self.events[room_id, event_id] = {
            'event_id': event_id,
            'type': 'm.room.message',
            'sender': BOT,
            'content': content,
        }
        self.transaction_events[key] = event_id
        return event_id

    def read_event(self, room_id, event_id):
        return copy.deepcopy(self.events[room_id, event_id])


class MultiControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = config(self.temporary.name)

    def state(self, name):
        return ControlState(Path(self.temporary.name) / name)

    def listener(self, bot, state, user, room, *, ai_state=None,
                 account_factory=None, execution_lock=None):
        ai = Mock()
        ai.check_available.return_value = True
        listener = MatrixCommandListener(
            bot, self.config, state, ai,
            account_factory=(account_factory or Mock(return_value=object())),
            room_id=room, control_user=user,
            ai_state=(state if ai_state is None else ai_state),
            execution_lock=execution_lock)
        return listener, ai

    def test_each_listener_accepts_all_exact_commands_only_for_its_bound_pair(self):
        commands = ('Test', 'Test 2', 'KI an', 'KI aus')
        bindings = ((PRIMARY_USER, PRIMARY_ROOM, SECONDARY_ROOM),
                    (SECONDARY_USER, SECONDARY_ROOM, PRIMARY_ROOM))
        bot = FakeBot({PRIMARY_ROOM: room_state(PRIMARY_USER),
                       SECONDARY_ROOM: room_state(SECONDARY_USER)})
        for binding_index, (user, room, wrong_room) in enumerate(bindings):
            with self.subTest(user=user, room=room):
                listener, _ai = self.listener(
                    bot, self.state('binding-' + str(binding_index)), user, room)
                accepted = [event('$ok-' + str(binding_index) + '-' + str(index), command,
                                  user)
                            for index, command in enumerate(commands)]
                wrong_sender = [event('$wrong-sender-' + str(binding_index) + '-' + str(index),
                                      command, OTHER_USER)
                                for index, command in enumerate(commands)]
                invalid = [event('$wrong-case-' + str(binding_index), 'test', user),
                           event('$formatted-' + str(binding_index), 'Test', user,
                                 extra={'format': 'org.matrix.custom.html',
                                        'formatted_body': 'Test'})]
                wrong_room_event = event('$wrong-room-' + str(binding_index), 'Test', user)
                response = sync_response(
                    'sync-' + str(binding_index), room, accepted + wrong_sender + invalid,
                    other_rooms={wrong_room: {
                        'timeline': {'events': [wrong_room_event]}}})

                cursor, jobs = listener._events_from_sync(response)

                self.assertEqual(cursor, 'sync-' + str(binding_index))
                self.assertEqual([command for _event_id, command in jobs], list(commands))
                self.assertEqual({event_id for event_id, _command in jobs},
                                 {item['event_id'] for item in accepted})
                self.assertEqual(set(commands), COMMANDS)

    def test_all_private_responses_route_to_the_origin_room(self):
        ai = Mock()
        ai.render.return_value = None
        test_parts = build_test_plan(
            exchange_with(3), self.config, ai, command='Test',
            command_event_id='$test', ai_enabled=False,
            reply_room_id=SECONDARY_ROOM)
        self.assertTrue(test_parts)
        self.assertEqual({part['room_id'] for part in test_parts}, {SECONDARY_ROOM})

        test_two = build_test_plan(
            exchange_with(1), self.config, ai, command='Test 2',
            command_event_id='$test-two', ai_enabled=False,
            reply_room_id=SECONDARY_ROOM)
        self.assertEqual({part['room_id'] for part in test_two[:-1]}, {PRODUCTION_ROOM})
        self.assertEqual(test_two[-1]['room_id'], SECONDARY_ROOM)
        self.assertIn('vollständig zugestellt', test_two[-1]['msg'])

        private_parts = [
            build_test_wait_ack(
                self.config, '$wait', 'Test', False,
                reply_room_id=SECONDARY_ROOM),
            *build_toggle_ack(
                self.config, '$toggle', True,
                reply_room_id=SECONDARY_ROOM),
            *build_failure_ack(
                self.config, '$failure', 'test_failed',
                reply_room_id=SECONDARY_ROOM),
            *build_delivery_failure_ack(
                self.config, '$delivery', 'Test 2',
                reply_room_id=SECONDARY_ROOM),
        ]
        self.assertEqual({part['room_id'] for part in private_parts}, {SECONDARY_ROOM})

    def test_controller_hash_paths_and_v1_states_are_separate(self):
        base = Path(self.temporary.name) / 'control'
        primary = ControlState(base)
        secondary_path = controller_state_directory(base, SECONDARY_USER, SECONDARY_ROOM)
        third_path = controller_state_directory(base, THIRD_USER, THIRD_ROOM)

        self.assertEqual(secondary_path,
                         controller_state_directory(base, SECONDARY_USER, SECONDARY_ROOM))
        self.assertNotEqual(secondary_path, third_path)
        self.assertEqual(secondary_path.parent, base / 'controllers')
        self.assertRegex(secondary_path.name, r'^[0-9a-f]{32}$')
        self.assertNotIn('secondary', str(secondary_path))
        self.assertNotIn('@', str(secondary_path))
        self.assertNotIn('!', str(secondary_path))

        secondary = ControlState(secondary_path)
        third = ControlState(third_path)
        primary.bootstrap('primary-0')
        secondary.bootstrap('secondary-0')
        third.bootstrap('third-0')
        primary.record_sync('primary-1', [('$primary', 'Test')])
        secondary.record_sync('secondary-1', [('$secondary', 'KI an')])

        self.assertEqual(primary.snapshot()['version'], 1)
        self.assertEqual(secondary.snapshot()['version'], 1)
        self.assertEqual(third.snapshot()['version'], 1)
        self.assertEqual([job['event_id'] for job in primary.snapshot()['queue']], ['$primary'])
        self.assertEqual([job['event_id'] for job in secondary.snapshot()['queue']], ['$secondary'])
        self.assertEqual(third.snapshot()['queue'], [])

    def test_poll_only_queues_and_immediately_sends_an_idempotent_wait_ack(self):
        state = self.state('poll-only')
        state.bootstrap('sync-0')
        bot = FakeBot({SECONDARY_ROOM: room_state(SECONDARY_USER)})
        command_event = event('$queued-test', 'Test', SECONDARY_USER)
        bot.sync_responses = [
            sync_response('sync-1', SECONDARY_ROOM, [command_event]),
            sync_response('sync-2', SECONDARY_ROOM, [command_event]),
        ]
        account_factory = Mock(side_effect=AssertionError('Exchange planning must not run'))
        listener, ai = self.listener(
            bot, state, SECONDARY_USER, SECONDARY_ROOM,
            account_factory=account_factory)
        work_event = threading.Event()

        bot.on_sync = listener.stop_event.set
        listener.run_poll_forever(work_event)
        bot.on_sync = None
        listener.stop_event.clear()
        self.assertTrue(listener.poll_once(
            timeout_ms=0, execute=False, work_event=work_event))

        digest = hashlib.sha256(b'$queued-test').hexdigest()[:40]
        transaction = 'control-' + digest + '-accepted'
        self.assertEqual(bot.send_attempts,
                         [(SECONDARY_ROOM, transaction), (SECONDARY_ROOM, transaction)])
        self.assertEqual(len(bot.transaction_events), 1)
        self.assertTrue(work_event.is_set())
        snapshot = state.snapshot()
        self.assertEqual(snapshot['since'], 'sync-2')
        self.assertEqual([(job['event_id'], job['command'], job['status'])
                          for job in snapshot['queue']],
                         [('$queued-test', 'Test', 'queued')])
        account_factory.assert_not_called()
        ai.check_available.assert_not_called()
        ai.render.assert_not_called()

    def test_worker_serializes_controllers_and_uses_one_shared_ai_switch(self):
        shared_ai_state = self.state('shared-ai')
        first_state = self.state('controller-one')
        second_state = self.state('controller-two')
        first_state.bootstrap('one-0')
        second_state.bootstrap('two-0')
        first_state.record_sync('one-1', [('$enable', 'KI an')])
        second_state.record_sync('two-1', [('$other-test', 'Test')])
        bot = FakeBot({SECONDARY_ROOM: room_state(SECONDARY_USER),
                       THIRD_ROOM: room_state(THIRD_USER)})
        execution_lock = threading.Lock()
        first_account = Mock(side_effect=AssertionError('Toggle must not open Exchange'))
        second_account = Mock(return_value=object())
        first, first_ai = self.listener(
            bot, first_state, SECONDARY_USER, SECONDARY_ROOM,
            ai_state=shared_ai_state, account_factory=first_account,
            execution_lock=execution_lock)
        second, second_ai = self.listener(
            bot, second_state, THIRD_USER, THIRD_ROOM,
            ai_state=shared_ai_state, account_factory=second_account,
            execution_lock=execution_lock)
        first.ai_service = second.ai_service = first_ai
        observed = []

        def plan(_exchange, _config, _ai, *, command, command_event_id,
                 ai_enabled, reply_room_id=None):
            observed.append((command, command_event_id, ai_enabled, reply_room_id))
            return [{'msg': 'Test fertig', 'html_msg': '<p>Test fertig</p>',
                     'room_id': reply_room_id, 'thread_root_part': None,
                     'transaction_id': 'worker-result', 'event_id': None}]

        work_event = threading.Event()
        worker = MatrixCommandWorker([first, second], work_event)
        with patch('matrix_commands.build_test_plan', side_effect=plan):
            work_event.set()
            thread = threading.Thread(target=worker.run_forever)
            thread.start()
            deadline = time.monotonic() + 3
            while ((first_state.head() is not None or second_state.head() is not None)
                   and time.monotonic() < deadline):
                time.sleep(0.01)
            worker.stop_event.set()
            work_event.set()
            thread.join(3)

        self.assertFalse(thread.is_alive())
        self.assertIsNone(first_state.head())
        self.assertIsNone(second_state.head())
        self.assertTrue(shared_ai_state.snapshot()['ai_enabled'])
        self.assertEqual(observed, [('Test', '$other-test', True, THIRD_ROOM)])
        first_ai.check_available.assert_called_once_with()
        second_account.assert_called_once_with()
        first_account.assert_not_called()
        self.assertIn((SECONDARY_ROOM,
                       'control-' + hashlib.sha256(b'$enable').hexdigest()[:40] + '-0'),
                      bot.send_attempts)
        self.assertIn((THIRD_ROOM, 'worker-result'), bot.send_attempts)

    def test_unsafe_secondary_room_fails_closed_before_any_side_effect(self):
        state = self.state('unsafe-secondary')
        state.bootstrap('sync-0')
        state.record_sync('sync-1', [('$unsafe', 'Test')])
        bot = FakeBot({SECONDARY_ROOM: room_state(
            SECONDARY_USER, extra_member=OTHER_USER)})
        account_factory = Mock(side_effect=AssertionError('Exchange must not be opened'))
        listener, ai = self.listener(
            bot, state, SECONDARY_USER, SECONDARY_ROOM,
            account_factory=account_factory)

        with self.assertRaisesRegex(ControlSecurityError, 'control_room_not_exclusive'):
            listener.process_head()

        self.assertEqual(state.head()['status'], 'queued')
        self.assertEqual(bot.send_attempts, [])
        account_factory.assert_not_called()
        ai.check_available.assert_not_called()
        ai.render.assert_not_called()


if __name__ == '__main__':
    unittest.main()
