"""Strict personal Matrix command listener for the final Methodenbot."""

import logging
import threading
import time
import unicodedata

from ai_summary import SummaryUnavailable
from control_state import COMMANDS, MAX_DELIVERY_FAILURES, ControlStateError
from manual_delivery import (build_delivery_failure_ack, build_failure_ack, build_test_plan,
                             build_test_wait_ack, build_toggle_ack, ManualDeliveryError)
from matrixbot import MatrixError


logger = logging.getLogger(__name__)


class ControlSecurityError(RuntimeError):
    pass


SECURITY_STATE = ('m.room.power_levels', 'm.room.join_rules', 'm.room.guest_access',
                  'm.room.history_visibility', 'm.room.encryption',
                  'm.room.third_party_invite', 'm.room.tombstone')


def validate_control_room(bot, config, *, room_id=None, control_user=None):
    room = config.matrix_console_room_id if room_id is None else room_id
    controller = config.matrix_control_user if control_user is None else control_user
    if (not config.allow_unencrypted_control_dm or room == config.matrix_room_id
            or not isinstance(room, str) or not room.startswith('!')):
        raise ControlSecurityError('control_room_not_approved')
    if bot.user_id == controller:
        raise ControlSecurityError('controller_is_bot')
    state = bot.get_room_state(room)
    members, events = {}, {}
    forbidden = {'m.room.encryption', 'm.room.third_party_invite', 'm.room.tombstone'}
    for event in state:
        if not isinstance(event, dict) or not isinstance(event.get('content'), dict):
            raise ControlSecurityError('invalid_control_room_state')
        kind = event.get('type')
        if kind in forbidden:
            raise ControlSecurityError('unsupported_control_room_state')
        if kind == 'm.room.member':
            members[event.get('state_key')] = event['content'].get('membership')
        elif event.get('state_key') == '':
            events[kind] = event['content']
    expected = {bot.user_id, controller}
    if set(members) != expected or any(members[user] != 'join' for user in expected):
        raise ControlSecurityError('control_room_not_exclusive')
    if (events.get('m.room.join_rules', {}).get('join_rule') != 'invite'
            or events.get('m.room.guest_access', {}).get('guest_access', 'forbidden') != 'forbidden'
            or events.get('m.room.history_visibility', {}).get('history_visibility') == 'world_readable'):
        raise ControlSecurityError('unsafe_control_room_access')
    levels = events.get('m.room.power_levels')
    if not isinstance(levels, dict):
        raise ControlSecurityError('control_power_levels_missing')
    users = levels.get('users', {})
    event_levels = levels.get('events', {})
    if not isinstance(users, dict) or not isinstance(event_levels, dict):
        raise ControlSecurityError('invalid_control_power_levels')
    default_user = levels.get('users_default', 0)
    controller_level = users.get(controller, default_user)
    bot_level = users.get(bot.user_id, default_user)
    invite_level = levels.get('invite', 50)
    state_default = levels.get('state_default', 50)
    if (not all(type(value) is int for value in (controller_level, bot_level, invite_level, state_default))
            or invite_level < 100 or bot_level < invite_level or controller_level >= invite_level):
        raise ControlSecurityError('unsafe_control_power_levels')
    for kind in SECURITY_STATE:
        threshold = event_levels.get(kind, state_default)
        if type(threshold) is not int or controller_level >= threshold:
            raise ControlSecurityError('controller_can_change_security_state')
    # m.direct is account-local UI metadata, not an authorization mechanism.
    # Keep it as a diagnostic signal; the explicit room id, membership and power
    # levels above are the actual security boundary.
    try:
        mapping = bot.direct_mapping()
        rooms = mapping.get(controller) if isinstance(mapping, dict) else None
        if not isinstance(rooms, list) or room not in rooms:
            logger.warning('Kontrollraum ist im Botkonto nicht als m.direct markiert.')
    except MatrixError:
        logger.warning('m.direct-Metadaten konnten nicht geprüft werden.')
    return True


def command_from_event(event, config, *, control_user=None):
    controller = config.matrix_control_user if control_user is None else control_user
    if (not isinstance(event, dict) or event.get('type') != 'm.room.message'
            or event.get('sender') != controller):
        return None
    event_id, content = event.get('event_id'), event.get('content')
    if not isinstance(event_id, str) or not event_id.startswith('$') or not isinstance(content, dict):
        return None
    if set(content) - {'msgtype', 'body', 'm.mentions'}:
        return None
    if content.get('msgtype') != 'm.text' or 'format' in content or 'formatted_body' in content:
        return None
    if 'm.relates_to' in content or content.get('m.mentions') not in (None, {}):
        return None
    body = content.get('body')
    if not isinstance(body, str):
        return None
    command = unicodedata.normalize('NFC', body).strip()
    return (event_id, command) if command in COMMANDS else None


class MatrixCommandListener:
    def __init__(self, bot, config, state, ai_service, account_factory, *, sleep=time.sleep,
                 room_id=None, control_user=None, ai_state=None, execution_lock=None):
        self.bot, self.config, self.state = bot, config, state
        self.ai_service, self.account_factory = ai_service, account_factory
        self.room_id = config.matrix_console_room_id if room_id is None else room_id
        self.control_user = config.matrix_control_user if control_user is None else control_user
        self.ai_state = state if ai_state is None else ai_state
        self.execution_lock = threading.Lock() if execution_lock is None else execution_lock
        self.sleep = sleep
        self.stop_event = threading.Event()

    def _validate_room(self):
        return validate_control_room(
            self.bot, self.config, room_id=self.room_id, control_user=self.control_user)

    def _events_from_sync(self, response):
        if not isinstance(response, dict) or not isinstance(response.get('next_batch'), str):
            raise MatrixError('invalid_sync_response')
        rooms = response.get('rooms', {})
        joined = rooms.get('join', {}) if isinstance(rooms, dict) else {}
        room = joined.get(self.room_id, {}) if isinstance(joined, dict) else {}
        timeline = room.get('timeline', {}) if isinstance(room, dict) else {}
        if timeline.get('limited') is True:
            raise MatrixError('control_timeline_limited')
        events = timeline.get('events', [])
        if not isinstance(events, list) or len(events) > 50:
            raise MatrixError('invalid_control_timeline')
        return response['next_batch'], [command for event in events
                                        if (command := command_from_event(
                                            event, self.config,
                                            control_user=self.control_user)) is not None]

    def bootstrap(self):
        self._validate_room()
        if self.state.snapshot()['since'] is not None:
            return False
        response = self.bot.sync(since=None, room_id=self.room_id, timeout_ms=0)
        cursor = response.get('next_batch') if isinstance(response, dict) else None
        if not isinstance(cursor, str) or not cursor:
            raise MatrixError('invalid_sync_response')
        self.state.bootstrap(cursor)
        logger.info('Matrix-Kontrolle initialisiert; vorhandene Historie wurde nicht ausgeführt.')
        return True

    def _plan_queued(self, job):
        event_id, command = job['event_id'], job['command']
        self._validate_room()
        if command in ('KI an', 'KI aus'):
            with self.execution_lock:
                self._validate_room()
                enabled = command == 'KI an'
                if enabled:
                    try:
                        self.ai_service.check_available()
                    except (SummaryUnavailable, OSError):
                        self.ai_state.set_ai_enabled(False)
                        parts = build_failure_ack(
                            self.config, event_id, 'ai_unavailable', reply_room_id=self.room_id)
                        self.state.plan_head(event_id, False, parts)
                        return
                self.ai_state.set_ai_enabled(enabled)
                self.state.plan_head(
                    event_id, enabled,
                    build_toggle_ack(
                        self.config, event_id, enabled, reply_room_id=self.room_id))
            return
        self._send_wait_ack(event_id, command)
        with self.execution_lock:
            self._validate_room()
            ai_enabled = self.ai_state.snapshot()['ai_enabled']
            try:
                exchange = self.account_factory()
                parts = build_test_plan(
                    exchange, self.config, self.ai_service, command=command,
                    command_event_id=event_id, ai_enabled=ai_enabled,
                    reply_room_id=self.room_id)
            except Exception as exc:
                # Only a type/code is logged; mail text and identifiers are not.
                logger.warning('Testbefehl vor Versand abgebrochen: %s', type(exc).__name__)
                parts = build_failure_ack(
                    self.config, event_id, 'test_failed', reply_room_id=self.room_id)
            self.state.plan_head(event_id, ai_enabled, parts)

    def _send_wait_ack(self, event_id, command):
        ai_enabled = self.ai_state.snapshot()['ai_enabled']
        wait_ack = build_test_wait_ack(
            self.config, event_id, command, ai_enabled, reply_room_id=self.room_id)
        wait_event = self.bot.send_message(
            msg=wait_ack['msg'], html_msg=wait_ack['html_msg'], room_id=wait_ack['room_id'],
            transaction_id=wait_ack['transaction_id'])
        self._verify(wait_ack, wait_event, None)
        return wait_event

    @staticmethod
    def _validate_part(part, parts):
        if (not isinstance(part, dict) or set(part) != {
                'msg', 'html_msg', 'room_id', 'thread_root_part', 'transaction_id', 'event_id'}
                or not isinstance(part['msg'], str) or not part['msg']
                or part['html_msg'] is not None and not isinstance(part['html_msg'], str)
                or not isinstance(part['room_id'], str) or not part['room_id'].startswith('!')
                or not isinstance(part['transaction_id'], str)
                or part['event_id'] is not None and (not isinstance(part['event_id'], str)
                                                     or not part['event_id'].startswith('$'))):
            raise ControlStateError('invalid_control_part')
        root = part['thread_root_part']
        if root is not None and (type(root) is not int or not 0 <= root < len(parts)):
            raise ControlStateError('invalid_control_thread_reference')

    def _verify(self, part, event_id, thread_root):
        event = self.bot.read_event(part['room_id'], event_id)
        content = event.get('content', {}) if isinstance(event, dict) else {}
        if (event.get('event_id', event_id) != event_id or event.get('type') != 'm.room.message'
                or event.get('sender') != self.bot.user_id or content.get('body') != part['msg']):
            raise MatrixError('matrix_readback_mismatch')
        if part['html_msg'] is not None and content.get('formatted_body') != part['html_msg']:
            raise MatrixError('matrix_readback_mismatch')
        relation = content.get('m.relates_to')
        if thread_root is None:
            if relation is not None:
                raise MatrixError('matrix_readback_mismatch')
        elif (not isinstance(relation, dict) or relation.get('rel_type') != 'm.thread'
              or relation.get('event_id') != thread_root):
            raise MatrixError('matrix_readback_mismatch')

    def _deliver_planned(self, job):
        parts = job.get('parts')
        if not isinstance(parts, list) or not parts:
            raise ControlStateError('empty_control_plan')
        for index, part in enumerate(parts):
            self._validate_part(part, parts)
            self._validate_room()
            root_index = part['thread_root_part']
            thread_root = None if root_index is None else parts[root_index].get('event_id')
            if root_index is not None and not isinstance(thread_root, str):
                raise ControlStateError('control_thread_root_not_confirmed')
            event_id = part.get('event_id')
            try:
                if event_id is None:
                    event_id = self.bot.send_message(
                        msg=part['msg'], html_msg=part['html_msg'], room_id=part['room_id'],
                        thread_reply_to=thread_root, transaction_id=part['transaction_id'])
                self._verify(part, event_id, thread_root)
            except MatrixError as exc:
                permanent = (exc.status is not None and 400 <= exc.status < 500
                             and exc.status not in (408, 409, 429)) or str(exc) in {
                                 'invalid_matrix_message', 'invalid_matrix_html',
                                 'matrix_message_too_large', 'matrix_readback_mismatch'}
                failures = self.state.record_delivery_failure(job['event_id'])
                if not permanent and failures < MAX_DELIVERY_FAILURES:
                    raise
                if not job.get('reporting_failure', False):
                    notice = build_delivery_failure_ack(
                        self.config, job['event_id'], job['command'],
                        reply_room_id=self.room_id)
                    self.state.replace_head_with_failure_notice(job['event_id'], notice)
                    logger.error('Matrix-Befehl nach begrenzten Zustellversuchen abgebrochen; '
                                 'private Warnung wird versucht.')
                else:
                    self.state.complete_head(job['event_id'])
                    logger.error('Private Zustellwarnung nicht zustellbar; Befehl terminal beendet.')
                return
            self.state.record_part(job['event_id'], index, event_id)
            # Refresh the in-memory copy for child thread references.
            part['event_id'] = event_id
        self.state.complete_head(job['event_id'])

    def drain(self):
        while not self.stop_event.is_set():
            if not self.process_head():
                return

    def process_head(self):
        job = self.state.head()
        if job is None:
            return False
        if job['status'] == 'queued':
            self._plan_queued(job)
            job = self.state.head()
        self._deliver_planned(job)
        return True

    def poll_once(self, *, timeout_ms=30_000, execute=True, work_event=None):
        self._validate_room()
        state = self.state.snapshot()
        if state['since'] is None:
            return self.bootstrap()
        response = self.bot.sync(since=state['since'], room_id=self.room_id,
                                 timeout_ms=timeout_ms)
        cursor, jobs = self._events_from_sync(response)
        self.state.record_sync(cursor, jobs)
        if execute:
            self.drain()
        else:
            if jobs and work_event is not None:
                work_event.set()
            # This is intentionally also repeated by the worker with the same
            # transaction id. A crash between enqueue and acknowledgement can
            # therefore never let Exchange or AI run before a confirmed notice.
            for event_id, command in jobs:
                if command in ('Test', 'Test 2'):
                    self._send_wait_ack(event_id, command)
        return bool(jobs)

    def run_poll_forever(self, work_event):
        self.state.acquire_process_lock()
        delay = 2
        while not self.stop_event.is_set():
            try:
                if self.state.snapshot()['since'] is None:
                    self.bootstrap()
                if self.state.head() is not None:
                    work_event.set()
                self.poll_once(timeout_ms=30_000, execute=False, work_event=work_event)
                delay = 2
            except (ControlSecurityError, ControlStateError, MatrixError, ManualDeliveryError) as exc:
                logger.error('Matrix-Raumpoller pausiert: %s', str(exc))
                self.stop_event.wait(delay)
                delay = min(delay * 2, 60)
            except Exception as exc:
                logger.error('Matrix-Raumpoller unerwartet pausiert: %s', type(exc).__name__)
                self.stop_event.wait(delay)
                delay = min(delay * 2, 60)

    def run_forever(self):
        self.state.acquire_process_lock()
        delay = 2
        while not self.stop_event.is_set():
            try:
                if self.state.snapshot()['since'] is None:
                    self.bootstrap()
                self.drain()
                self.poll_once(timeout_ms=30_000)
                delay = 2
            except (ControlSecurityError, ControlStateError, MatrixError, ManualDeliveryError) as exc:
                logger.error('Matrix-Kontrolle pausiert: %s', str(exc))
                self.stop_event.wait(delay)
                delay = min(delay * 2, 60)
            except Exception as exc:
                logger.error('Matrix-Kontrolle unerwartet pausiert: %s', type(exc).__name__)
                self.stop_event.wait(delay)
                delay = min(delay * 2, 60)


class MatrixCommandWorker:
    """Process all controller queues serially while room pollers stay responsive."""

    def __init__(self, listeners, work_event, *, sleep=time.sleep):
        if not isinstance(listeners, (list, tuple)) or not listeners:
            raise ControlStateError('control_worker_without_listeners')
        self.listeners = tuple(listeners)
        self.work_event = work_event
        self.sleep = sleep
        self.stop_event = threading.Event()
        self._next = 0

    def _next_listener_with_work(self):
        for offset in range(len(self.listeners)):
            index = (self._next + offset) % len(self.listeners)
            listener = self.listeners[index]
            if listener.state.head() is not None:
                self._next = (index + 1) % len(self.listeners)
                return listener
        return None

    def run_forever(self):
        delay = 2
        while not self.stop_event.is_set():
            self.work_event.wait(2)
            self.work_event.clear()
            if self.stop_event.is_set():
                return
            listener = self._next_listener_with_work()
            if listener is None:
                continue
            try:
                listener.process_head()
                delay = 2
            except (ControlSecurityError, ControlStateError, MatrixError, ManualDeliveryError) as exc:
                logger.error('Matrix-Befehlsworker pausiert: %s', str(exc))
                self.stop_event.wait(delay)
                delay = min(delay * 2, 60)
            except Exception as exc:
                logger.error('Matrix-Befehlsworker unerwartet pausiert: %s', type(exc).__name__)
                self.stop_event.wait(delay)
                delay = min(delay * 2, 60)
            if any(candidate.state.head() is not None for candidate in self.listeners):
                self.work_event.set()
