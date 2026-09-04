import copy
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from digest_bundle import DigestBundleError, pack_bundle, unpack_bundle
from digest_service import (MARKDOWN_MEDIA_UPLOAD_SUFFIX, MARKDOWN_MEDIA_URI_SUFFIX,
                            DigestService, DigestServiceError, _validate_fallback_room,
                            digest_command_from_event, markdown_to_matrix_html,
                            split_markdown)
from digest_state import DigestState, DigestStateError
import digest_upload_receiver
import digest_upload
import digest_service
from matrixbot import (DIGEST_FALLBACK_MARKER, MAX_EVENT_CONTENT_BYTES, MatrixError,
                       matrix_file_mimetype, matrix_message_content)


BOT = '@methodenbot:example.org'
USER = '@reader:example.org'
ROOM = '!digest:example.org'
SECOND_USER = '@second-reader:example.org'
SECOND_ROOM = '!second-digest:example.org'
ENCRYPTED_USER = '@encrypted-reader:example.org'
SECOND_ENCRYPTED_USER = '@second-encrypted-reader:example.org'


def room_state(*, user_id=USER, extra=None, encrypted=False):
    result = [
        {'type': 'm.room.member', 'state_key': BOT, 'content': {'membership': 'join'}},
        {'type': 'm.room.member', 'state_key': user_id,
         'content': {'membership': 'join'}},
        {'type': 'm.room.join_rules', 'state_key': '', 'content': {'join_rule': 'invite'}},
        {'type': 'm.room.guest_access', 'state_key': '',
         'content': {'guest_access': 'forbidden'}},
        {'type': 'm.room.history_visibility', 'state_key': '',
         'content': {'history_visibility': 'shared'}},
    ]
    if extra:
        result.append({'type': 'm.room.member', 'state_key': extra,
                       'content': {'membership': 'join'}})
    if encrypted:
        result.append({'type': 'm.room.encryption', 'state_key': '',
                       'content': {'algorithm': 'm.megolm.v1.aes-sha2'}})
    return result


def fallback_room_state(user_id, target_sha256, *, membership='invite', unsafe=None):
    protected = {
        'm.room.power_levels': 100,
        'm.room.encryption': 100,
        'm.room.join_rules': 100,
        'm.room.guest_access': 100,
        'm.room.history_visibility': 100,
        'm.room.name': 100,
        'm.room.topic': 100,
        'm.room.third_party_invite': 100,
        'm.room.tombstone': 150,
    }
    result = [
        {'type': 'm.room.create', 'state_key': '', 'sender': BOT,
         'content': {DIGEST_FALLBACK_MARKER: {
             'version': 1, 'target_sha256': target_sha256}}},
        {'type': 'm.room.member', 'state_key': BOT,
         'content': {'membership': 'join'}},
        {'type': 'm.room.join_rules', 'state_key': '',
         'content': {'join_rule': 'invite'}},
        {'type': 'm.room.guest_access', 'state_key': '',
         'content': {'guest_access': 'forbidden'}},
        {'type': 'm.room.history_visibility', 'state_key': '',
         'content': {'history_visibility': 'shared'}},
        {'type': 'm.room.power_levels', 'state_key': '', 'sender': BOT,
         'content': {
             'users_default': 0, 'events_default': 0, 'state_default': 100,
             'invite': 100, 'kick': 100, 'ban': 100, 'redact': 100,
             'events': protected}},
    ]
    if membership is not None:
        result.insert(2, {'type': 'm.room.member', 'state_key': user_id,
                          'content': {'membership': membership}})
    if unsafe == 'encrypted':
        result.append({'type': 'm.room.encryption', 'state_key': '', 'content': {}})
    elif unsafe == 'power':
        next(value for value in result
             if value['type'] == 'm.room.power_levels')['content']['invite'] = 0
    return result


def event(identity, body, *, sender=USER, extra=None):
    content = {'msgtype': 'm.text', 'body': body}
    if extra:
        content.update(extra)
    return {'event_id': identity, 'type': 'm.room.message', 'sender': sender,
            'content': content}


def paged_event(identity, body, *, room_id=ROOM, sender=USER, extra=None):
    result = event(identity, body, sender=sender, extra=extra)
    result['room_id'] = room_id
    return result


def membership_event(identity, *, sender, membership, room_id=ROOM,
                     state_key=BOT):
    return {
        'room_id': room_id, 'event_id': identity, 'type': 'm.room.member',
        'state_key': state_key, 'sender': sender,
        'content': {'membership': membership},
    }


class FakeBot:
    def __init__(self):
        self.user_id = BOT
        self.states = {ROOM: room_state()}
        self.sync_responses = []
        self.sync_calls = []
        self.room_message_responses = []
        self.room_message_calls = []
        self.state_calls = []
        self.joined = []
        self.created_rooms = []
        self.invited_users = []
        self.rejected_invitations = []
        self.reject_results = []
        self.read_results = []
        self.actions = []
        self.events = {}
        self.transactions = {}
        self.send_attempts = []
        self.file_attempts = []
        self.media_uploads = []
        self.media_creations = 0
        self.delivery_attempts = []

    def sync(self, **kwargs):
        self.sync_calls.append(copy.deepcopy(kwargs))
        response = self.sync_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def room_messages(self, room_id, **kwargs):
        self.room_message_calls.append((room_id, copy.deepcopy(kwargs)))
        response = self.room_message_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def join_room(self, room_id):
        self.joined.append(room_id)
        return room_id

    def get_room_state(self, room_id):
        self.state_calls.append(room_id)
        return copy.deepcopy(self.states[room_id])

    def joined_room_ids(self):
        return sorted(self.states)

    def create_digest_fallback_room(self, user_id, target_sha256):
        room_id = '!fallback-' + str(len(self.created_rooms) + 1) + ':example.org'
        self.created_rooms.append((user_id, target_sha256, room_id))
        self.actions.append(('create_fallback', room_id))
        self.states[room_id] = fallback_room_state(user_id, target_sha256)
        return room_id

    def invite_user(self, room_id, user_id):
        self.invited_users.append((room_id, user_id))
        self.actions.append(('invite_user', room_id))
        self.states[room_id].append({
            'type': 'm.room.member', 'state_key': user_id,
            'content': {'membership': 'invite'}})
        return True

    def reject_invitation(self, room_id):
        self.rejected_invitations.append(room_id)
        self.actions.append(('reject_invitation', room_id))
        if self.reject_results:
            result = self.reject_results.pop(0)
            if isinstance(result, Exception):
                raise result
        return True

    def send_message(self, msg, room_id=None, transaction_id=None, html_msg=None, **_kwargs):
        self.delivery_attempts.append(('text', room_id, transaction_id))
        self.send_attempts.append((room_id, transaction_id, msg, html_msg))
        self.actions.append(('send_message', room_id))
        if transaction_id in self.transactions:
            return self.transactions[transaction_id]
        event_id = '$sent-' + str(len(self.transactions) + 1)
        self.transactions[transaction_id] = event_id
        content = {'msgtype': 'm.text', 'body': msg}
        if html_msg is not None:
            content.update(format='org.matrix.custom.html', formatted_body=html_msg)
        self.events[room_id, event_id] = {
            'event_id': event_id, 'type': 'm.room.message', 'sender': BOT,
            'content': content}
        return event_id

    def create_media_uri(self):
        self.media_creations += 1
        return 'mxc://example.org/media' + str(self.media_creations)

    def upload_media(self, content_uri, raw, filename, content_type=None, **_kwargs):
        mimetype = matrix_file_mimetype(filename)
        if mimetype is None or content_type not in (None, mimetype):
            raise MatrixError('invalid_matrix_media')
        self.media_uploads.append((content_uri, raw, filename, mimetype))
        return content_uri

    def send_file(self, content_uri, filename, size, room_id=None, transaction_id=None):
        self.delivery_attempts.append(('file', room_id, transaction_id))
        self.file_attempts.append((room_id, transaction_id, content_uri, filename, size))
        if transaction_id in self.transactions:
            return self.transactions[transaction_id]
        event_id = '$sent-' + str(len(self.transactions) + 1)
        self.transactions[transaction_id] = event_id
        mimetype = matrix_file_mimetype(filename)
        if mimetype is None:
            raise MatrixError('invalid_matrix_file')
        self.events[room_id, event_id] = {
            'event_id': event_id, 'type': 'm.room.message', 'sender': BOT,
            'content': {
                'msgtype': 'm.file', 'body': filename, 'filename': filename,
                'url': content_uri,
                'info': {'mimetype': mimetype, 'size': size},
            }}
        return event_id

    def read_event(self, room_id, event_id):
        self.actions.append(('read_event', room_id))
        if self.read_results:
            result = self.read_results.pop(0)
            if isinstance(result, Exception):
                raise result
        return copy.deepcopy(self.events[room_id, event_id])


class Stdin:
    def __init__(self, raw):
        self.buffer = io.BytesIO(raw)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.config = SimpleNamespace(
            digest_state_dir=str(root / 'state'), digest_inbox_dir=str(root / 'state/inbox'))
        self.state = DigestState(self.config.digest_state_dir)
        self.bot = FakeBot()
        self.service = DigestService(self.bot, self.config, self.state)

    def test_only_exact_digest_commands_and_standard_formatting_are_accepted(self):
        self.assertEqual(digest_command_from_event(event('$1', ' Digest ')),
                         ('$1', USER, 'Digest'))
        self.assertEqual(digest_command_from_event(event('$2', 'Digest aus')),
                         ('$2', USER, 'Digest aus'))
        self.assertEqual(digest_command_from_event(event(
            '$3', 'Digest', extra={'format': 'org.matrix.custom.html',
                                   'formatted_body': '<strong>Digest</strong>'})),
            ('$3', USER, 'Digest'))
        for candidate in (
                event('$4', 'digest'),
                event('$5', 'Digest', extra={'format': 'org.matrix.custom.html'}),
                event('$6', 'Digest', extra={'formatted_body': 'Digest'}),
                event('$7', 'Digest', extra={'format': 'org.matrix.custom.html',
                                             'formatted_body': 7}),
                event('$8', 'Digest', extra={'m.relates_to': {'rel_type': 'm.replace'}}),
                {'event_id': '$9', 'type': 'm.reaction', 'sender': USER,
                 'content': {'body': 'Digest'}}):
            self.assertIsNone(digest_command_from_event(candidate))

    def test_bootstrap_ignores_old_commands(self):
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'events': [event('$old', 'Digest')]}}}}}]
        self.assertTrue(self.service.bootstrap())
        self.assertEqual(self.state.snapshot()['subscriptions'], {})
        self.assertEqual(self.bot.send_attempts, [])

    def test_invite_join_catches_first_formatted_command_without_cursor_loss(self):
        self.state.bootstrap('s0')
        invited = '!invite:example.org'
        self.bot.states[invited] = room_state()
        formatted = event('$subscribe', 'Digest', extra={
            'format': 'org.matrix.custom.html', 'formatted_body': '<p>Digest</p>'})
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [
                    {'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                     'content': {'membership': 'invite'}}]}}}}},
            {'next_batch': 'catch-up-only', 'rooms': {'join': {invited: {
                'timeline': {'events': [formatted]}}}}},
        ]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.joined, [invited])
        self.assertEqual(self.state.snapshot()['subscriptions'][USER]['room_id'], invited)
        self.assertEqual(self.state.snapshot()['since'], 's1')
        self.assertEqual(self.bot.sync_calls, [
            {'since': 's0', 'timeout_ms': 0},
            {'since': 's0', 'room_id': invited, 'timeout_ms': 0},
        ])
        self.assertIn('Digest aktiviert', self.bot.send_attempts[-1][2])

        self.bot.sync_responses = [{'next_batch': 's2', 'rooms': {'join': {invited: {
            'timeline': {'events': [formatted]}}}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.send_attempts), 1)
        self.assertEqual(self.state.snapshot()['since'], 's2')

    def test_join_catch_up_error_keeps_original_cursor(self):
        self.state.bootstrap('s0')
        invited = '!invite-error:example.org'
        self.bot.states[invited] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [
                    {'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                     'content': {'membership': 'invite'}}]}}}}},
            MatrixError('matrix_network_error'),
        ]
        with self.assertRaisesRegex(MatrixError, 'matrix_network_error'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['since'], 's0')
        self.assertEqual(self.state.snapshot()['subscriptions'], {})

    def test_limited_join_catch_up_closes_gap_and_advances_original_cursor(self):
        self.state.bootstrap('s0')
        invited = '!invite-limited:example.org'
        self.bot.states[invited] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [
                    {'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                     'content': {'membership': 'invite'}}]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'state': {'events': [{
                    'event_id': '$bot-join', 'type': 'm.room.member',
                    'state_key': BOT, 'sender': BOT,
                    'content': {'membership': 'join'},
                }]},
                'timeline': {'limited': True,
                             'prev_batch': 'p-before-visible',
                             'events': [event('$unsubscribe', 'Digest aus')]}}}}},
        ]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-empty',
             'chunk': [
                 membership_event(
                     '$bot-invite', sender=USER, membership='invite',
                     room_id=invited),
                 membership_event(
                     '$bot-join', sender=BOT, membership='join',
                     room_id=invited),
                 paged_event('$subscribe-gap', 'Digest', room_id=invited),
             ]},
            {'start': 'p-empty', 'end': 'p-before-visible', 'chunk': []},
        ]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['since'], 's1')
        self.assertEqual(self.state.snapshot()['subscriptions'], {})
        self.assertEqual(self.bot.room_message_calls, [
            (invited, {'from_token': 's0', 'to_token': 'p-before-visible', 'limit': 50}),
            (invited, {'from_token': 'p-empty', 'to_token': 'p-before-visible', 'limit': 50}),
        ])
        self.assertIn('Digest aktiviert', self.bot.send_attempts[0][2])
        self.assertIn('Digest deaktiviert', self.bot.send_attempts[1][2])

    def test_limited_join_catch_up_state_join_allows_visible_command(self):
        self.state.bootstrap('s0')
        invited = '!invite-state-join-visible:example.org'
        self.bot.states[invited] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'state': {'events': [{
                    'event_id': '$state-visible-join',
                    'type': 'm.room.member', 'state_key': BOT, 'sender': BOT,
                    'content': {'membership': 'join'},
                }]},
                'timeline': {
                    'limited': True, 'prev_batch': 'p-visible',
                    'events': [event('$state-visible-command', 'Digest')],
                },
            }}}},
        ]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-visible', 'chunk': []}]
        self.service.poll_once(timeout_ms=0)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['since'], 's1')
        self.assertEqual(
            snapshot['subscriptions'][USER]['event_id'],
            '$state-visible-command')

    def test_limited_join_catch_up_state_invite_alone_rejects_visible_command(self):
        self.state.bootstrap('s0')
        invited = '!invite-state-invite-only:example.org'
        self.bot.states[invited] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'state': {'events': [{
                    'event_id': '$state-only-invite',
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]},
                'timeline': {
                    'limited': True, 'prev_batch': 'p-visible',
                    'events': [event('$unsafe-visible-command', 'Digest')],
                },
            }}}},
        ]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-visible', 'chunk': []}]
        with self.assertRaisesRegex(
                MatrixError, 'digest_command_before_join_boundary'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_limited_join_catch_up_accepts_expected_invite_join_gap_boundary(self):
        self.state.bootstrap('s0')
        invited = '!invite-gap-boundary:example.org'
        self.bot.states[invited] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'timeline': {
                    'limited': True, 'prev_batch': 'p-visible', 'events': [],
                },
            }}}},
        ]
        self.bot.room_message_responses = [{
            'start': 's0', 'end': 'p-visible', 'chunk': [
                membership_event(
                    '$expected-invite', sender=USER, membership='invite',
                    room_id=invited),
                membership_event(
                    '$expected-self-join', sender=BOT, membership='join',
                    room_id=invited),
                paged_event('$expected-command', 'Digest', room_id=invited),
            ],
        }]
        self.service.poll_once(timeout_ms=0)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['since'], 's1')
        self.assertEqual(
            snapshot['subscriptions'][USER]['event_id'], '$expected-command')
        self.assertEqual(len(self.bot.send_attempts), 1)

    def test_limited_join_catch_up_accepts_exact_state_and_gap_boundary_duplicate(self):
        self.state.bootstrap('s0')
        invited = '!invite-duplicate-boundary:example.org'
        self.bot.states[invited] = room_state()
        state_join = membership_event(
            '$duplicate-self-join', sender=BOT, membership='join',
            room_id=invited)
        state_join.pop('room_id')
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'state': {'events': [state_join]},
                'timeline': {
                    'limited': True, 'prev_batch': 'p-visible', 'events': [],
                },
            }}}},
        ]
        self.bot.room_message_responses = [{
            'start': 's0', 'end': 'p-visible', 'chunk': [
                membership_event(
                    '$duplicate-invite', sender=USER, membership='invite',
                    room_id=invited),
                membership_event(
                    '$duplicate-self-join', sender=BOT, membership='join',
                    room_id=invited),
                paged_event('$duplicate-command', 'Digest', room_id=invited),
            ],
        }]
        self.service.poll_once(timeout_ms=0)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['since'], 's1')
        self.assertEqual(
            snapshot['subscriptions'][USER]['event_id'], '$duplicate-command')
        self.assertEqual(len(self.bot.send_attempts), 1)

    def test_limited_join_catch_up_rejects_command_before_duplicated_boundary(self):
        self.state.bootstrap('s0')
        invited = '!invite-history-replay:example.org'
        self.bot.states[invited] = room_state()
        state_invite = membership_event(
            '$replay-invite', sender=USER, membership='invite',
            room_id=invited)
        state_join = membership_event(
            '$replay-self-join', sender=BOT, membership='join',
            room_id=invited)
        state_invite.pop('room_id')
        state_join.pop('room_id')
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'state': {'events': [state_invite, state_join]},
                'timeline': {
                    'limited': True, 'prev_batch': 'p-visible', 'events': [],
                },
            }}}},
        ]
        self.bot.room_message_responses = [{
            'start': 's0', 'end': 'p-visible', 'chunk': [
                paged_event('$pre-invite-command', 'Digest', room_id=invited),
                membership_event(
                    '$replay-invite', sender=USER, membership='invite',
                    room_id=invited),
                membership_event(
                    '$replay-self-join', sender=BOT, membership='join',
                    room_id=invited),
            ],
        }]
        with self.assertRaisesRegex(
                MatrixError, 'digest_security_state_changed_in_gap'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['subscriptions'], {})
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_limited_join_catch_up_rejects_state_timeline_event_id_conflict(self):
        self.state.bootstrap('s0')
        invited = '!invite-state-id-conflict:example.org'
        self.bot.states[invited] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'state': {'events': [{
                    'event_id': '$state-command-conflict',
                    'type': 'm.room.topic', 'state_key': '', 'sender': USER,
                    'content': {'topic': 'Legitimate state update'},
                }, {
                    'event_id': '$state-conflict-self-join',
                    'type': 'm.room.member', 'state_key': BOT, 'sender': BOT,
                    'content': {'membership': 'join'},
                }]},
                'timeline': {
                    'limited': True, 'prev_batch': 'p-visible', 'events': [],
                },
            }}}},
        ]
        self.bot.room_message_responses = [{
            'start': 's0', 'end': 'p-visible',
            'chunk': [
                membership_event(
                    '$state-conflict-self-join', sender=BOT,
                    membership='join', room_id=invited),
                paged_event(
                    '$state-command-conflict', 'Digest', room_id=invited),
            ],
        }]
        with self.assertRaisesRegex(MatrixError, 'conflicting_matrix_event'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['subscriptions'], {})
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_limited_join_catch_up_rejects_invite_from_wrong_sender(self):
        self.state.bootstrap('s0')
        invited = '!invite-wrong-sender:example.org'
        wrong = '@wrong-inviter:example.org'
        self.bot.states[invited] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'timeline': {
                    'limited': True, 'prev_batch': 'p-visible', 'events': [],
                },
            }}}},
        ]
        self.bot.room_message_responses = [{
            'start': 's0', 'end': 'p-visible', 'chunk': [
                membership_event(
                    '$wrong-invite', sender=wrong, membership='invite',
                    room_id=invited),
                membership_event(
                    '$wrong-invite-self-join', sender=BOT, membership='join',
                    room_id=invited),
                paged_event('$wrong-invite-command', 'Digest', room_id=invited),
            ],
        }]
        with self.assertRaisesRegex(
                MatrixError, 'digest_security_state_changed_in_gap'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_limited_join_catch_up_rejects_additional_member_transition(self):
        self.state.bootstrap('s0')
        invited = '!invite-extra-member:example.org'
        extra = '@extra:example.org'
        self.bot.states[invited] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'timeline': {
                    'limited': True, 'prev_batch': 'p-visible', 'events': [],
                },
            }}}},
        ]
        self.bot.room_message_responses = [{
            'start': 's0', 'end': 'p-visible', 'chunk': [
                membership_event(
                    '$extra-invite', sender=USER, membership='invite',
                    room_id=invited),
                membership_event(
                    '$extra-self-join', sender=BOT, membership='join',
                    room_id=invited),
                membership_event(
                    '$extra-member', sender=extra, membership='join',
                    room_id=invited, state_key=extra),
                paged_event('$extra-command', 'Digest', room_id=invited),
            ],
        }]
        with self.assertRaisesRegex(
                MatrixError, 'digest_security_state_changed_in_gap'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_limited_join_catch_up_rejects_nonboundary_security_transition(self):
        self.state.bootstrap('s0')
        invited = '!invite-limited-unsafe:example.org'
        self.bot.states[invited] = room_state()
        bot_join = {
            'event_id': '$late-bot-join', 'type': 'm.room.member',
            'state_key': BOT, 'sender': BOT,
            'content': {'membership': 'join'},
        }
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'timeline': {
                    'limited': True, 'prev_batch': 'p-visible',
                    'events': [event('$command-before-join', 'Digest'), bot_join],
                },
            }}}},
        ]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-visible', 'chunk': []}]
        with self.assertRaisesRegex(
                MatrixError, 'digest_security_state_changed_in_gap'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_poll_request_budget_is_shared_by_catch_up_and_global_gap(self):
        self.state.bootstrap('s0')
        invited = '!invite-budget:example.org'
        later = '!later-budget:example.org'
        self.bot.states[invited] = room_state()
        self.bot.states[later] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {
                'invite': {invited: {'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}},
                'join': {later: {'timeline': {
                    'limited': True, 'prev_batch': 'p-later', 'events': []}}},
            }},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'state': {'events': [{
                    'event_id': '$budget-bot-join', 'type': 'm.room.member',
                    'state_key': BOT, 'sender': BOT,
                    'content': {'membership': 'join'},
                }]},
                'timeline': {
                    'limited': True, 'prev_batch': 'p-invited',
                    'events': [event('$caught-before-budget', 'Digest')],
                },
            }}}},
        ]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-invited', 'chunk': []}]
        with patch('digest_service.MAX_DIGEST_GAP_REQUESTS_PER_POLL', 2):
            with self.assertRaisesRegex(
                    MatrixError, 'digest_gap_poll_budget_exceeded'):
                self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.state_calls, [invited])
        self.assertEqual(len(self.bot.room_message_calls), 1)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_poll_gap_event_budget_is_shared_across_rooms(self):
        self.state.bootstrap('s0')
        second = '!second-gap-budget:example.org'
        self.bot.states[second] = room_state()
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {
            ROOM: {'timeline': {
                'limited': True, 'prev_batch': 'p-one', 'events': []}},
            second: {'timeline': {
                'limited': True, 'prev_batch': 'p-two', 'events': []}},
        }}}]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-one',
             'chunk': [paged_event('$budget-one', 'hello')]},
            {'start': 's0', 'end': 'p-two',
             'chunk': [paged_event('$budget-two', 'hello', room_id=second)]},
        ]
        with patch('digest_service.MAX_DIGEST_GAP_EVENTS_PER_POLL', 1):
            with self.assertRaisesRegex(
                    MatrixError, 'digest_gap_poll_budget_exceeded'):
                self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.room_message_calls), 2)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_poll_event_id_registry_is_bounded_across_rooms(self):
        self.state.bootstrap('s0')
        second = '!second-registry-budget:example.org'
        self.bot.states[second] = room_state()
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {
            ROOM: {'timeline': {'events': [event('$registry-one', 'hello')]}},
            second: {'timeline': {'events': [event('$registry-two', 'hello')]}},
        }}}]
        with patch('digest_service.MAX_DIGEST_EVENT_IDS_PER_POLL', 1):
            with self.assertRaisesRegex(
                    MatrixError, 'digest_gap_poll_budget_exceeded'):
                self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_limited_group_room_does_not_block_digest_dm_or_cursor(self):
        self.state.bootstrap('s0')
        group = '!busy-group:example.org'
        self.bot.states[group] = room_state(extra='@third:example.org')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {
            group: {'timeline': {'limited': True, 'events': []}},
            ROOM: {'timeline': {'events': [event('$subscribe', 'Digest')]}}
        }}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['subscriptions'][USER]['room_id'], ROOM)
        self.assertEqual(self.state.snapshot()['since'], 's1')
        self.assertIn('Digest aktiviert', self.bot.send_attempts[-1][2])

    def test_limited_digest_dm_closes_multipage_gap_in_chronological_order(self):
        self.state.bootstrap('s0')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'limited': True,
                         'prev_batch': 'p-before-visible',
                         'events': [event('$visible-on', 'Digest')]}
        }}}}]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-mid',
             'chunk': [paged_event('$gap-on', 'Digest')]},
            {'start': 'p-mid', 'end': 'p-before-visible',
             'chunk': [paged_event('$gap-off', 'Digest aus')]},
        ]
        self.service.poll_once(timeout_ms=0)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['since'], 's1')
        self.assertEqual(snapshot['subscriptions'][USER]['event_id'], '$visible-on')
        self.assertEqual([
            'deaktiviert' if 'deaktiviert' in attempt[2] else 'aktiviert'
            for attempt in self.bot.send_attempts],
            ['aktiviert', 'deaktiviert', 'aktiviert'])

    def test_limited_digest_dm_without_prev_batch_remains_fail_closed(self):
        self.state.bootstrap('s0')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'limited': True, 'events': [event('$visible', 'Digest')]}
        }}}}]
        with self.assertRaisesRegex(MatrixError, 'invalid_sync_response'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['since'], 's0')
        self.assertEqual(self.bot.send_attempts, [])

    def test_limited_digest_dm_aborts_before_other_room_side_effects(self):
        self.state.bootstrap('s0')
        second_room = '!second-digest:example.org'
        self.bot.states[second_room] = room_state()
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {
            ROOM: {'timeline': {'events': [event('$would-subscribe', 'Digest')]}},
            second_room: {'timeline': {
                'limited': True, 'prev_batch': 'p-gap', 'events': []}},
        }}}]
        self.bot.room_message_responses = [MatrixError('matrix_network_error')]
        with self.assertRaisesRegex(MatrixError, 'matrix_network_error'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')
        self.assertEqual(self.state.snapshot()['subscriptions'], {})

    def test_limited_gap_is_fully_fetched_before_any_command_side_effect(self):
        self.state.bootstrap('s0')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {
                'limited': True, 'prev_batch': 'p-final', 'events': []}
        }}}}]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-mid',
             'chunk': [paged_event('$would-subscribe', 'Digest')]},
            MatrixError('matrix_network_error'),
        ]
        with self.assertRaisesRegex(MatrixError, 'matrix_network_error'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')
        self.assertEqual(self.state.snapshot()['subscriptions'], {})

    def test_later_global_gap_error_prevents_invite_catch_up_command_side_effect(self):
        self.state.bootstrap('s0')
        invited = '!invite-before-error:example.org'
        later = '!later-limited:example.org'
        self.bot.states[invited] = room_state()
        self.bot.states[later] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {
                'invite': {invited: {'invite_state': {'events': [{
                    'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                    'content': {'membership': 'invite'},
                }]}}},
                'join': {later: {'timeline': {
                    'limited': True, 'prev_batch': 'p-later', 'events': []}}},
            }},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'timeline': {'events': [event('$caught-up', 'Digest')]}
            }}}},
        ]
        self.bot.room_message_responses = [MatrixError('matrix_network_error')]
        with self.assertRaisesRegex(MatrixError, 'matrix_network_error'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.joined, [invited])
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')
        self.assertEqual(self.state.snapshot()['subscriptions'], {})

    def test_security_state_event_in_limited_gap_keeps_cursor(self):
        self.state.bootstrap('s0')
        membership = {
            'room_id': ROOM, 'event_id': '$member-gap', 'type': 'm.room.member',
            'sender': USER, 'state_key': USER,
            'content': {'membership': 'join', 'displayname': 'Changed'},
        }
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {
                'limited': True, 'prev_batch': 'p-final',
                'events': [event('$visible-command', 'Digest')]}
        }}}}]
        self.bot.room_message_responses = [{
            'start': 's0', 'end': 'p-final', 'chunk': [membership]}]
        with self.assertRaisesRegex(
                MatrixError, 'digest_security_state_changed_in_gap'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_security_state_event_in_visible_limited_timeline_keeps_cursor(self):
        self.state.bootstrap('s0')
        join_rules = {
            'event_id': '$join-rules-visible', 'type': 'm.room.join_rules',
            'sender': USER, 'state_key': '', 'content': {'join_rule': 'invite'},
        }
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {
                'limited': True, 'prev_batch': 'p-final',
                'events': [join_rules, event('$visible-command', 'Digest')]}
        }}}}]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-final', 'chunk': []}]
        with self.assertRaisesRegex(
                MatrixError, 'digest_security_state_changed_in_gap'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_security_state_delta_for_limited_timeline_keeps_cursor(self):
        self.state.bootstrap('s0')
        power = {
            'event_id': '$power-gap', 'type': 'm.room.power_levels',
            'sender': BOT, 'state_key': '', 'content': {'users_default': 0},
        }
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'state': {'events': [power]},
            'timeline': {
                'limited': True, 'prev_batch': 'p-final',
                'events': [event('$visible-command', 'Digest')]}
        }}}}]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-final', 'chunk': []}]
        with self.assertRaisesRegex(
                MatrixError, 'digest_security_state_changed_in_gap'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_security_state_delta_without_digest_candidate_does_not_block_other_room(self):
        self.state.bootstrap('s0')
        other = '!other-digest:example.org'
        self.bot.states[other] = room_state()
        membership = {
            'event_id': '$fallback-member', 'type': 'm.room.member',
            'sender': BOT, 'state_key': BOT, 'content': {'membership': 'join'},
        }
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {
            ROOM: {
                'state': {'events': [membership]},
                'timeline': {
                    'limited': True, 'prev_batch': 'p-final', 'events': []},
            },
            other: {'timeline': {'events': [event('$other-subscribe', 'Digest')]}},
        }}}]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-final', 'chunk': []}]
        self.service.poll_once(timeout_ms=0)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['since'], 's1')
        self.assertEqual(snapshot['subscriptions'][USER]['room_id'], other)
        self.assertEqual(len(self.bot.send_attempts), 1)

    def test_limited_gap_without_end_is_a_complete_bounded_range(self):
        self.state.bootstrap('s0')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {
                'limited': True, 'prev_batch': 'p-final', 'events': []}
        }}}}]
        self.bot.room_message_responses = [{
            'start': 's0', 'chunk': [paged_event('$subscribe', 'Digest')]}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['since'], 's1')
        self.assertEqual(
            self.state.snapshot()['subscriptions'][USER]['event_id'], '$subscribe')

    def test_gap_and_visible_duplicate_event_is_processed_once(self):
        self.state.bootstrap('s0')
        duplicate = event('$duplicate', 'Digest')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'limited': True, 'prev_batch': 'p-final',
                         'events': [duplicate]}
        }}}}]
        self.bot.room_message_responses = [{
            'start': 's0', 'end': 'p-final',
            'chunk': [paged_event('$duplicate', 'Digest')]}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.send_attempts), 1)
        self.assertEqual(self.state.snapshot()['since'], 's1')

    def test_conflicting_duplicate_event_keeps_cursor_and_sends_nothing(self):
        self.state.bootstrap('s0')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'limited': True, 'prev_batch': 'p-final',
                         'events': [event('$duplicate', 'Digest aus')]}
        }}}}]
        self.bot.room_message_responses = [{
            'start': 's0', 'end': 'p-final',
            'chunk': [paged_event('$duplicate', 'Digest')]}]
        with self.assertRaisesRegex(MatrixError, 'conflicting_matrix_event'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_gap_pagination_token_cycle_keeps_cursor(self):
        self.state.bootstrap('s0')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {
                'limited': True, 'prev_batch': 'p-final', 'events': []}
        }}}}]
        self.bot.room_message_responses = [
            {'start': 's0', 'end': 'p-mid', 'chunk': []},
            {'start': 'p-mid', 'end': 's0', 'chunk': []},
        ]
        with self.assertRaisesRegex(MatrixError, 'invalid_matrix_messages_response'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_gap_pagination_page_bound_keeps_cursor(self):
        self.state.bootstrap('s0')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {
                'limited': True, 'prev_batch': 'p-final', 'events': []}
        }}}}]
        self.bot.room_message_responses = [
            {'start': 's0' if index == 0 else 'p' + str(index),
             'end': 'p' + str(index + 1), 'chunk': []}
            for index in range(40)]
        with self.assertRaisesRegex(MatrixError, 'digest_gap_too_large'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.room_message_calls), 40)
        self.assertEqual(self.state.snapshot()['since'], 's0')

    def test_limited_encrypted_room_is_safely_skipped(self):
        self.state.bootstrap('s0')
        encrypted = '!encrypted-legacy:example.org'
        self.bot.states[encrypted] = room_state(encrypted=True)
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {
            encrypted: {'timeline': {'limited': True, 'events': []}},
        }}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['since'], 's1')
        self.assertEqual(self.state.snapshot()['subscriptions'], {})

    def test_limited_room_with_unknown_state_remains_fail_closed(self):
        self.state.bootstrap('s0')
        unknown = '!unknown-state:example.org'
        self.bot.states[unknown] = [
            {'type': 'm.room.member', 'state_key': BOT, 'content': {}}]
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {unknown: {
            'timeline': {'limited': True, 'events': []}
        }}}}]
        with self.assertRaisesRegex(MatrixError, 'invalid_digest_room_state'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['since'], 's0')
        self.assertEqual(self.state.snapshot()['subscriptions'], {})

    def test_digest_command_immediately_sends_latest_newsletter_and_both_files(self):
        inbox = Path(self.config.digest_inbox_dir)
        inbox.mkdir(mode=0o700)
        markdown = b'# Methoden-Journal-Digest \xe2\x80\x93 28. August 2026\n\n**Kurzfazit**\n'
        ris = b'TY  - JOUR\nTI  - Beispiel\nER  -\n'
        source = inbox / '2026-08-28-methoden-digest.bundle'
        source.write_bytes(pack_bundle(markdown, ris))
        source.chmod(0o600)
        self.service.process_inbox()
        self.assertEqual(self.bot.send_attempts, [])
        self.state.bootstrap('s0')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'events': [event('$subscribe-now', 'Digest')]}}}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['subscriptions'][USER]['room_id'], ROOM)
        self.assertEqual(len(self.bot.send_attempts), 2)
        self.assertIn('Digest aktiviert', self.bot.send_attempts[0][2])
        self.assertEqual(self.bot.send_attempts[1][2], markdown.decode())
        self.assertEqual(len(self.bot.file_attempts), 2)
        self.assertEqual(self.bot.file_attempts[0][3], '2026-08-28-methoden-artikel.ris')
        self.assertEqual(self.bot.file_attempts[1][3], '2026-08-28-methoden-digest.md')
        self.assertTrue(self.bot.file_attempts[0][1].endswith('-ris'))
        self.assertTrue(self.bot.file_attempts[1][1].endswith('-md'))
        self.assertEqual(self.bot.media_uploads[0][1], ris)
        self.assertEqual(self.bot.media_uploads[0][3],
                         'application/x-research-info-systems')
        self.assertEqual(self.bot.media_uploads[1][1], markdown)
        self.assertEqual(self.bot.media_uploads[1][3], 'text/markdown; charset=utf-8')
        self.assertNotEqual(self.bot.file_attempts[0][2], self.bot.file_attempts[1][2])
        self.assertEqual([kind for kind, _room, _transaction in self.bot.delivery_attempts],
                         ['text', 'text', 'file', 'file'])
        self.assertTrue(self.bot.delivery_attempts[0][2].startswith('digest-command-'))
        self.assertTrue(self.bot.delivery_attempts[1][2].endswith('-m1'))
        self.assertTrue(self.bot.delivery_attempts[2][2].endswith('-ris'))
        self.assertTrue(self.bot.delivery_attempts[3][2].endswith('-md'))

    def test_immediate_markdown_readback_retry_reuses_media_and_transactions(self):
        inbox = Path(self.config.digest_inbox_dir)
        inbox.mkdir(mode=0o700)
        markdown = b'# Methoden-Journal-Digest - 28. August 2026\n\nKurz.\n'
        ris = b'TY  - JOUR\nTI  - Beispiel\nER  -\n'
        source = inbox / '2026-08-28-methoden-digest.bundle'
        source.write_bytes(pack_bundle(markdown, ris))
        source.chmod(0o600)
        self.service.process_inbox()
        self.state.bootstrap('s0')
        response = {'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'events': [event('$subscribe-retry', 'Digest')]}}}}}
        self.bot.sync_responses = [copy.deepcopy(response), copy.deepcopy(response)]
        original_read = self.bot.read_event
        failed = {'once': False}

        def fail_markdown_once(room_id, event_id):
            result = original_read(room_id, event_id)
            if (result['content'].get('filename', '').endswith('-methoden-digest.md')
                    and not failed['once']):
                failed['once'] = True
                result['content']['info']['size'] += 1
            return result

        self.bot.read_event = fail_markdown_once
        with self.assertRaisesRegex(MatrixError, 'matrix_file_readback_mismatch'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['since'], 's0')
        self.assertNotIn('$subscribe-retry', self.state.snapshot()['completed_commands'])
        first_markdown = self.bot.file_attempts[-1]
        self.service.poll_once(timeout_ms=0)
        second_markdown = self.bot.file_attempts[-1]
        self.assertEqual(first_markdown[1:], second_markdown[1:])
        self.assertEqual(len(self.bot.media_uploads), 2)
        self.assertEqual(self.state.snapshot()['since'], 's1')
        self.assertIn('$subscribe-retry', self.state.snapshot()['completed_commands'])

    def test_non_newsletter_digest_is_not_used_for_immediate_delivery(self):
        inbox = Path(self.config.digest_inbox_dir)
        inbox.mkdir(mode=0o700)
        source = inbox / '2026-08-31-methoden-digest.bundle'
        source.write_bytes(pack_bundle(
            b'# Formatierungstest des Methoden-Digests\n',
            b'TY  - JOUR\nTI  - Test\nER  -\n'))
        source.chmod(0o600)
        self.service.process_inbox()
        self.state.bootstrap('s0')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'events': [event('$subscribe-test', 'Digest')]}}}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.send_attempts), 1)
        self.assertEqual(self.bot.file_attempts, [])

    def test_encrypted_invite_gets_hardened_unencrypted_fallback(self):
        self.state.bootstrap('s0')
        invited = '!encrypted:example.org'
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'invite': {invited: {
            'invite_state': {'events': [
                {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
                {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
                 'content': {'membership': 'invite'}}]}}}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.joined, [])
        self.assertEqual(len(self.bot.created_rooms), 1)
        self.assertEqual(self.bot.created_rooms[0][0], ENCRYPTED_USER)
        fallback = self.bot.created_rooms[0][2]
        self.assertEqual(self.bot.send_attempts[-1][0], fallback)
        self.assertIn('nicht Ende-zu-Ende-verschlüsselten Raum',
                      self.bot.send_attempts[-1][2])
        self.assertEqual(self.bot.rejected_invitations, [invited])
        self.assertLess(self.bot.actions.index(('send_message', fallback)),
                        self.bot.actions.index(('read_event', fallback)))
        self.assertLess(self.bot.actions.index(('read_event', fallback)),
                        self.bot.actions.index(('reject_invitation', invited)))
        self.assertEqual(self.state.snapshot()['since'], 's1')

    def test_existing_unencrypted_subscription_suppresses_notice_and_rejects_old_invite(self):
        self.state.bootstrap('s0')
        fallback = '!existing-plain:example.org'
        invited = '!encrypted-old:example.org'
        self.bot.states[fallback] = room_state(user_id=ENCRYPTED_USER)
        self.state.apply_command(
            '$existing-subscription', 'Digest', ENCRYPTED_USER, fallback)
        before = self.state.snapshot()['subscriptions']
        invite = {'invite_state': {'events': [
            {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
            {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
             'content': {'membership': 'invite'}}]}}
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: invite}}}]

        self.service.poll_once(timeout_ms=0)

        self.assertEqual(self.bot.created_rooms, [])
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.bot.rejected_invitations, [invited])
        self.assertEqual(self.state.snapshot()['subscriptions'], before)
        self.assertEqual(self.state.snapshot()['since'], 's1')

    def test_existing_unencrypted_room_without_subscription_also_suppresses_notice(self):
        self.state.bootstrap('s0')
        fallback = '!existing-plain-no-subscription:example.org'
        invited = '!encrypted-old-no-subscription:example.org'
        self.bot.states[fallback] = room_state(user_id=ENCRYPTED_USER)
        invite = {'invite_state': {'events': [
            {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
            {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
             'content': {'membership': 'invite'}}]}}
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: invite}}}]

        self.service.poll_once(timeout_ms=0)

        self.assertEqual(self.bot.created_rooms, [])
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.bot.rejected_invitations, [invited])
        self.assertEqual(self.state.snapshot()['subscriptions'], {})
        self.assertEqual(self.state.snapshot()['since'], 's1')

    def test_startup_suppresses_old_encrypted_notice_for_existing_subscription(self):
        self.state.bootstrap('s0')
        fallback = '!existing-startup-plain:example.org'
        invited = '!encrypted-startup-old:example.org'
        self.bot.states[fallback] = room_state(user_id=ENCRYPTED_USER)
        self.state.apply_command('$startup-subscription', 'Digest', ENCRYPTED_USER, fallback)
        before = self.state.snapshot()['subscriptions']
        self.assertFalse(self.service.bootstrap())
        invite = {'invite_state': {'events': [
            {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
            {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
             'content': {'membership': 'invite'}}]}}
        self.bot.sync_responses = [
            {'next_batch': 'reconcile', 'rooms': {'invite': {invited: invite}}},
            {'next_batch': 's1', 'rooms': {}},
        ]

        self.service.poll_once(timeout_ms=0)

        self.assertEqual(self.bot.created_rooms, [])
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.bot.rejected_invitations, [invited])
        self.assertEqual(self.state.snapshot()['subscriptions'], before)
        self.assertEqual(self.state.snapshot()['since'], 's1')

    def test_startup_reconciles_old_encrypted_invite_without_replaying_history(self):
        self.state.bootstrap('s0')
        self.assertFalse(self.service.bootstrap())
        invited = '!encrypted-old:example.org'
        invite = {'invite_state': {'events': [
            {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
            {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
             'content': {'membership': 'invite'}}]}}
        self.bot.sync_responses = [
            {'next_batch': 'reconcile', 'rooms': {
                'invite': {invited: invite},
                'join': {ROOM: {'timeline': {'events': [event('$old', 'Digest')]}}}}},
            {'next_batch': 's1', 'rooms': {}},
        ]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.created_rooms), 1)
        self.assertEqual(self.bot.rejected_invitations, [invited])
        self.assertEqual(self.state.snapshot()['subscriptions'], {})
        self.assertFalse(self.state.command_completed('$old'))
        self.assertEqual(self.state.snapshot()['since'], 's1')

    def test_startup_joins_old_unencrypted_invite_but_requests_command_resend(self):
        self.state.bootstrap('s0')
        invited = '!old-unencrypted:example.org'
        self.bot.states[invited] = room_state()
        self.assertFalse(self.service.bootstrap())
        self.bot.sync_responses = [
            {'next_batch': 'reconcile', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [
                    {'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                     'content': {'membership': 'invite'}}]}}}}},
            {'next_batch': 's1', 'rooms': {}},
        ]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.joined, [invited])
        self.assertEqual(self.bot.rejected_invitations, [])
        self.assertEqual(self.state.snapshot()['subscriptions'], {})
        self.assertIn('sende den Befehl bitte hier noch einmal',
                      self.bot.send_attempts[-1][2])
        self.assertEqual(self.bot.sync_calls, [
            {'since': None, 'timeout_ms': 0},
            {'since': 's0', 'timeout_ms': 0},
        ])

    def test_startup_reuses_marked_fallback_instead_of_creating_duplicate(self):
        self.state.bootstrap('s0')
        target_sha256 = hashlib.sha256(ENCRYPTED_USER.encode()).hexdigest()
        fallback = '!existing-fallback:example.org'
        self.bot.states[fallback] = fallback_room_state(ENCRYPTED_USER, target_sha256)
        self.assertFalse(self.service.bootstrap())
        invite = {'invite_state': {'events': [
            {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
            {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
             'content': {'membership': 'invite'}}]}}
        self.bot.sync_responses = [
            {'next_batch': 'reconcile', 'rooms': {
                'invite': {'!encrypted-old:example.org': invite}}},
            {'next_batch': 's1', 'rooms': {}},
        ]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.created_rooms, [])
        self.assertEqual(self.bot.send_attempts[-1][0], fallback)
        self.assertEqual(
            self.bot.rejected_invitations, ['!encrypted-old:example.org'])

    def test_startup_recovers_bot_only_marker_before_any_second_create(self):
        self.state.bootstrap('s0')
        target_sha256 = hashlib.sha256(ENCRYPTED_USER.encode()).hexdigest()
        fallback = '!bot-only-fallback:example.org'
        self.bot.states[fallback] = fallback_room_state(
            ENCRYPTED_USER, target_sha256, membership=None)
        self.assertFalse(self.service.bootstrap())
        invite = {'invite_state': {'events': [
            {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
            {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
             'content': {'membership': 'invite'}}]}}
        self.bot.sync_responses = [
            {'next_batch': 'reconcile', 'rooms': {
                'invite': {'!encrypted-old:example.org': invite}}},
            {'next_batch': 's1', 'rooms': {}},
        ]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.created_rooms, [])
        self.assertEqual(self.bot.invited_users, [(fallback, ENCRYPTED_USER)])
        self.assertEqual(self.bot.send_attempts[-1][0], fallback)
        self.assertEqual(
            self.bot.rejected_invitations, ['!encrypted-old:example.org'])

    def test_notice_readback_failure_does_not_reject_invite_or_advance_cursor(self):
        self.state.bootstrap('s0')
        invited = '!encrypted-readback:example.org'
        invite = {'invite_state': {'events': [
            {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
            {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
             'content': {'membership': 'invite'}}]}}
        self.bot.read_results = [MatrixError('matrix_network_error')]
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: invite}}}]

        with self.assertRaisesRegex(MatrixError, 'matrix_network_error'):
            self.service.poll_once(timeout_ms=0)

        self.assertEqual(len(self.bot.created_rooms), 1)
        self.assertEqual(self.bot.rejected_invitations, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

        self.bot.sync_responses = [
            {'next_batch': 's2', 'rooms': {'invite': {invited: invite}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.created_rooms), 1)
        self.assertEqual(len(self.bot.transactions), 1)
        self.assertEqual(self.bot.rejected_invitations, [invited])
        self.assertEqual(self.state.snapshot()['since'], 's2')

    def test_reject_failure_retries_without_second_visible_notice_or_room(self):
        self.state.bootstrap('s0')
        invited = '!encrypted-retry:example.org'
        invite = {'invite_state': {'events': [
            {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
            {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
             'content': {'membership': 'invite'}}]}}
        self.bot.reject_results = [MatrixError('matrix_network_error'), True]
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: invite}}}]

        with self.assertRaisesRegex(MatrixError, 'matrix_network_error'):
            self.service.poll_once(timeout_ms=0)

        fallback = self.bot.created_rooms[0][2]
        first_transaction = self.bot.send_attempts[-1][1]
        self.assertEqual(self.state.snapshot()['since'], 's0')

        self.bot.sync_responses = [
            {'next_batch': 's2', 'rooms': {'invite': {invited: invite}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.created_rooms), 1)
        self.assertEqual([attempt[1] for attempt in self.bot.send_attempts],
                         [first_transaction, first_transaction])
        self.assertEqual(len(self.bot.events), 1)
        self.assertIn((fallback, self.bot.transactions[first_transaction]), self.bot.events)
        self.assertEqual(self.bot.rejected_invitations, [invited, invited])
        self.assertEqual(self.state.snapshot()['since'], 's2')

    def test_ambiguous_reject_already_applied_needs_no_second_action(self):
        self.state.bootstrap('s0')
        invited = '!encrypted-ambiguous-applied:example.org'
        invite = {'invite_state': {'events': [
            {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
            {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
             'content': {'membership': 'invite'}}]}}
        self.bot.reject_results = [MatrixError('matrix_network_error')]
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: invite}}}]

        with self.assertRaisesRegex(MatrixError, 'matrix_network_error'):
            self.service.poll_once(timeout_ms=0)

        self.assertEqual(len(self.bot.send_attempts), 1)
        self.assertEqual(self.bot.rejected_invitations, [invited])
        self.assertEqual(self.state.snapshot()['since'], 's0')

        # The lost response was ambiguous, but the next sync proves that the
        # homeserver applied the leave and no longer reports the invitation.
        self.bot.sync_responses = [{'next_batch': 's2', 'rooms': {}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.send_attempts), 1)
        self.assertEqual(self.bot.rejected_invitations, [invited])
        self.assertEqual(self.state.snapshot()['since'], 's2')

    def test_reject_retry_never_warns_existing_unencrypted_subscriber(self):
        self.state.bootstrap('s0')
        fallback = '!existing-retry-plain:example.org'
        invited = '!encrypted-existing-retry:example.org'
        self.bot.states[fallback] = room_state(user_id=ENCRYPTED_USER)
        self.state.apply_command('$retry-subscription', 'Digest', ENCRYPTED_USER, fallback)
        invite = {'invite_state': {'events': [
            {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
            {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
             'content': {'membership': 'invite'}}]}}
        self.bot.reject_results = [MatrixError('matrix_network_error'), True]
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: invite}}}]

        with self.assertRaisesRegex(MatrixError, 'matrix_network_error'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.state.snapshot()['since'], 's0')

        self.bot.sync_responses = [
            {'next_batch': 's2', 'rooms': {'invite': {invited: invite}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.bot.rejected_invitations, [invited, invited])
        self.assertEqual(self.state.snapshot()['since'], 's2')

    def test_encrypted_group_invite_with_third_invitee_creates_no_fallback(self):
        self.state.bootstrap('s0')
        invited = '!encrypted-group:example.org'
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'invite': {invited: {
            'invite_state': {'events': [
                {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
                {'type': 'm.room.member', 'state_key': '@third:example.org',
                 'sender': ENCRYPTED_USER, 'content': {'membership': 'invite'}},
                {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
                 'content': {'membership': 'invite'}}]}}}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.created_rooms, [])
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.bot.rejected_invitations, [])

    def test_multiple_encrypted_invites_from_same_user_send_one_notice_and_reject_both(self):
        self.state.bootstrap('s0')

        def encrypted_invite():
            return {'invite_state': {'events': [
                {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
                {'type': 'm.room.member', 'state_key': BOT, 'sender': ENCRYPTED_USER,
                 'content': {'membership': 'invite'}}]}}

        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'invite': {
            '!encrypted-a:example.org': encrypted_invite(),
            '!encrypted-b:example.org': encrypted_invite(),
        }}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.created_rooms), 1)
        self.assertEqual(len(self.bot.send_attempts), 1)
        self.assertEqual(self.bot.rejected_invitations, [
            '!encrypted-a:example.org', '!encrypted-b:example.org'])
        self.assertEqual(self.state.snapshot()['since'], 's1')

    def test_only_one_fallback_is_created_per_pass_and_retry_reconciles(self):
        self.state.bootstrap('s0')

        def encrypted_invite(sender):
            return {'invite_state': {'events': [
                {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
                {'type': 'm.room.member', 'state_key': BOT, 'sender': sender,
                 'content': {'membership': 'invite'}}]}}

        response = {'next_batch': 's1', 'rooms': {'invite': {
            '!a-encrypted:example.org': encrypted_invite(ENCRYPTED_USER),
            '!b-encrypted:example.org': encrypted_invite(SECOND_ENCRYPTED_USER),
        }}}
        self.bot.sync_responses = [response]
        with self.assertRaisesRegex(DigestServiceError, 'digest_fallback_create_limit'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.created_rooms), 1)
        self.assertEqual(self.state.snapshot()['since'], 's0')

        retry = {'next_batch': 's2', 'rooms': {'invite': {
            '!b-encrypted:example.org': encrypted_invite(SECOND_ENCRYPTED_USER),
        }}}
        self.bot.sync_responses = [retry]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.created_rooms), 2)
        self.assertEqual(self.bot.rejected_invitations, [
            '!a-encrypted:example.org', '!b-encrypted:example.org'])
        self.assertEqual(self.state.snapshot()['since'], 's2')

    def test_fallback_validation_rejects_unsafe_state_marker_creator_and_members(self):
        target_sha256 = hashlib.sha256(ENCRYPTED_USER.encode()).hexdigest()
        wrong_marker = fallback_room_state(ENCRYPTED_USER, target_sha256)
        wrong_marker[0]['content'][DIGEST_FALLBACK_MARKER]['target_sha256'] = 'b' * 64
        wrong_creator = fallback_room_state(ENCRYPTED_USER, target_sha256)
        wrong_creator[0]['sender'] = '@other-bot:example.org'
        for index, state in enumerate((
                fallback_room_state(ENCRYPTED_USER, target_sha256,
                                    unsafe='encrypted'),
                fallback_room_state(ENCRYPTED_USER, target_sha256,
                                    unsafe='power'),
                wrong_marker,
                wrong_creator,
                fallback_room_state(ENCRYPTED_USER, target_sha256) + [{
                    'type': 'm.room.member', 'state_key': '@third:example.org',
                    'content': {'membership': 'join'}}])):
            room_id = '!unsafe-' + str(index) + ':example.org'
            with self.assertRaises(DigestServiceError):
                _validate_fallback_room(
                    self.bot, room_id, ENCRYPTED_USER, target_sha256, state=state)

    def test_group_invite_is_not_joined(self):
        self.state.bootstrap('s0')
        invited = '!group:example.org'
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'invite': {invited: {
            'invite_state': {'events': [
                {'type': 'm.room.member', 'state_key': USER, 'sender': USER,
                 'content': {'membership': 'join'}},
                {'type': 'm.room.member', 'state_key': '@third:example.org', 'sender': USER,
                 'content': {'membership': 'join'}},
                {'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                 'content': {'membership': 'invite'}}]}}}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.joined, [])

    def test_group_command_is_ignored_without_public_ack(self):
        self.state.bootstrap('s0')
        self.bot.states[ROOM] = room_state(extra='@third:example.org')
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'events': [event('$group', 'Digest')]}}}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['subscriptions'], {})
        self.assertEqual(self.bot.send_attempts, [])

    def test_digest_aus_removes_subscription(self):
        self.state.bootstrap('s0')
        self.state.apply_command('$sub', 'Digest', USER, ROOM)
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'events': [event('$off', 'Digest aus')]}}}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['subscriptions'], {})
        self.assertIn('Digest deaktiviert', self.bot.send_attempts[-1][2])

    def test_markdown_is_delivered_once_and_receipt_is_complete(self):
        self.state.apply_command('$sub', 'Digest', USER, ROOM)
        inbox = Path(self.config.digest_inbox_dir)
        inbox.mkdir(mode=0o700)
        markdown = '# Wochenüberblick\n\nEine geprüfte Zusammenfassung.\n'
        ris = 'TY  - JOUR\nTI  - Beispiel\nER  -\n'
        source = inbox / '2026-08-28-methoden-digest.bundle'
        source.write_bytes(pack_bundle(markdown.encode(), ris.encode()))
        source.chmod(0o600)
        self.service.process_inbox()
        first_count = len(self.bot.send_attempts)
        self.assertFalse(source.exists())
        self.service.process_inbox()
        self.assertEqual(len(self.bot.send_attempts), first_count)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['digests']['2026-08-28']['status'], 'complete')
        recipient = snapshot['digests']['2026-08-28']['recipients'][USER]
        self.assertEqual(recipient['status'], 'delivered')
        self.assertEqual(''.join(value[2] for value in self.bot.send_attempts), markdown)
        self.assertEqual(self.bot.send_attempts[0][3],
                         '<h1>Wochenüberblick</h1><p>Eine geprüfte Zusammenfassung.</p>')
        self.assertEqual(len(self.bot.file_attempts), 2)
        self.assertEqual(self.bot.file_attempts[0][3], '2026-08-28-methoden-artikel.ris')
        self.assertEqual(self.bot.file_attempts[1][3], '2026-08-28-methoden-digest.md')
        self.assertEqual(self.bot.media_uploads[0][1], ris.encode())
        self.assertEqual(self.bot.media_uploads[1][1], markdown.encode())
        self.assertEqual(self.bot.media_uploads[1][3], 'text/markdown; charset=utf-8')
        self.assertEqual(snapshot['version'], 2)
        self.assertEqual(len(recipient['parts']), 2)
        for sidecar in self.state.content_directory.glob('*.matrix-markdown-*'):
            sidecar.unlink()
        self.bot.file_attempts.clear()
        self.bot.media_uploads.clear()
        DigestService(self.bot, self.config, self.state).process_inbox()
        self.assertEqual(self.bot.file_attempts, [])
        self.assertEqual(self.bot.media_uploads, [])

    def test_weekly_markdown_retry_keeps_v2_receipts_and_reuses_event(self):
        self.state.apply_command('$sub', 'Digest', USER, ROOM)
        inbox = Path(self.config.digest_inbox_dir)
        inbox.mkdir(mode=0o700)
        markdown = b'# Wochenueberblick\n\nKurz.\n'
        ris = b'TY  - JOUR\nTI  - Beispiel\nER  -\n'
        source = inbox / '2026-08-28-methoden-digest.bundle'
        source.write_bytes(pack_bundle(markdown, ris))
        source.chmod(0o600)
        original_read = self.bot.read_event
        failed = {'once': False}

        def fail_markdown_once(room_id, event_id):
            result = original_read(room_id, event_id)
            if (result['content'].get('filename', '').endswith('-methoden-digest.md')
                    and not failed['once']):
                failed['once'] = True
                result['content']['url'] = 'mxc://example.org/wrong'
            return result

        self.bot.read_event = fail_markdown_once
        self.service.process_inbox()
        pending = self.state.snapshot()['digests']['2026-08-28']['recipients'][USER]
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(pending['failures'], 1)
        self.assertTrue(all(pending['parts']))
        self.assertEqual(len(pending['parts']), 2)
        text_attempts = len(self.bot.send_attempts)
        first_markdown = self.bot.file_attempts[-1]
        DigestService(self.bot, self.config, self.state).process_inbox()
        delivered = self.state.snapshot()['digests']['2026-08-28']['recipients'][USER]
        self.assertEqual(delivered['status'], 'delivered')
        self.assertEqual(len(self.bot.send_attempts), text_attempts)
        self.assertEqual(first_markdown[1:], self.bot.file_attempts[-1][1:])
        self.assertEqual(len(self.bot.media_uploads), 2)

    def test_weekly_files_are_uploaded_once_for_multiple_recipients(self):
        self.state.apply_command('$sub', 'Digest', USER, ROOM)
        second_state = room_state()
        for state_event in second_state:
            if state_event.get('state_key') == USER:
                state_event['state_key'] = SECOND_USER
        self.bot.states[SECOND_ROOM] = second_state
        self.state.apply_command('$sub-second', 'Digest', SECOND_USER, SECOND_ROOM)
        inbox = Path(self.config.digest_inbox_dir)
        inbox.mkdir(mode=0o700)
        markdown = b'# Wochenueberblick\n\nKurz.\n'
        ris = b'TY  - JOUR\nTI  - Beispiel\nER  -\n'
        source = inbox / '2026-08-28-methoden-digest.bundle'
        source.write_bytes(pack_bundle(markdown, ris))
        source.chmod(0o600)
        original_read = self.bot.read_event
        failed = {'once': False}

        def fail_first_recipient_markdown_once(room_id, event_id):
            result = original_read(room_id, event_id)
            if (room_id == ROOM
                    and result['content'].get('filename', '').endswith('-methoden-digest.md')
                    and not failed['once']):
                failed['once'] = True
                result['content']['info']['size'] += 1
            return result

        self.bot.read_event = fail_first_recipient_markdown_once
        self.service.process_inbox()
        snapshot = self.state.snapshot()['digests']['2026-08-28']
        self.assertEqual(snapshot['recipients'][USER]['status'], 'pending')
        self.assertEqual(snapshot['recipients'][USER]['failures'], 1)
        self.assertEqual(snapshot['recipients'][SECOND_USER]['status'], 'delivered')
        self.assertEqual(len(self.bot.media_uploads), 2)
        self.assertEqual([attempt[3] for attempt in self.bot.file_attempts], [
            '2026-08-28-methoden-artikel.ris',
            '2026-08-28-methoden-digest.md',
            '2026-08-28-methoden-artikel.ris',
            '2026-08-28-methoden-digest.md',
        ])
        self.assertEqual(self.bot.file_attempts[0][2], self.bot.file_attempts[2][2])
        self.assertEqual(self.bot.file_attempts[1][2], self.bot.file_attempts[3][2])
        self.assertNotEqual(self.bot.file_attempts[0][1], self.bot.file_attempts[2][1])
        self.assertNotEqual(self.bot.file_attempts[1][1], self.bot.file_attempts[3][1])
        first_markdown = self.bot.file_attempts[1]
        DigestService(self.bot, self.config, self.state).process_inbox()
        retried = self.state.snapshot()['digests']['2026-08-28']['recipients']
        self.assertEqual(retried[USER]['status'], 'delivered')
        self.assertEqual(retried[SECOND_USER]['status'], 'delivered')
        self.assertEqual(first_markdown[1:], self.bot.file_attempts[-1][1:])
        self.assertEqual(len(self.bot.file_attempts), 5)
        self.assertEqual(len(self.bot.media_uploads), 2)

    def test_corrupt_markdown_media_sidecar_stops_before_delivery(self):
        self.state.apply_command('$sub', 'Digest', USER, ROOM)
        inbox = Path(self.config.digest_inbox_dir)
        inbox.mkdir(mode=0o700)
        source = inbox / '2026-08-28-methoden-digest.bundle'
        source.write_bytes(pack_bundle(
            b'# Wochenueberblick\n', b'TY  - JOUR\nTI  - Beispiel\nER  -\n'))
        source.chmod(0o600)
        self.service._stage_inbox()
        digest = self.state.snapshot()['digests']['2026-08-28']
        sidecar = (self.state.content_directory
                   / (digest['content_file'] + MARKDOWN_MEDIA_URI_SUFFIX))
        sidecar.write_bytes(b'not-a-content-uri\n')
        sidecar.chmod(0o600)
        with self.assertRaisesRegex(
                DigestServiceError, 'invalid_digest_markdown_media'):
            self.service._deliver()
        self.assertEqual(self.bot.send_attempts, [])
        self.assertEqual(self.bot.file_attempts, [])
        recipient = self.state.snapshot()['digests']['2026-08-28']['recipients'][USER]
        self.assertEqual(recipient['status'], 'pending')

    def test_retry_after_markdown_upload_before_proof_reuses_media_uri(self):
        self.state.apply_command('$sub', 'Digest', USER, ROOM)
        inbox = Path(self.config.digest_inbox_dir)
        inbox.mkdir(mode=0o700)
        source = inbox / '2026-08-28-methoden-digest.bundle'
        source.write_bytes(pack_bundle(
            b'# Wochenueberblick\n', b'TY  - JOUR\nTI  - Beispiel\nER  -\n'))
        source.chmod(0o600)
        self.service._stage_inbox()
        original_write = digest_service._atomic_private_write
        failed = {'once': False}

        def fail_proof_once(directory, name, raw):
            if name.endswith(MARKDOWN_MEDIA_UPLOAD_SUFFIX) and not failed['once']:
                failed['once'] = True
                raise DigestServiceError('simulated_proof_write_failure')
            return original_write(directory, name, raw)

        with patch.object(digest_service, '_atomic_private_write', fail_proof_once):
            with self.assertRaisesRegex(
                    DigestServiceError, 'simulated_proof_write_failure'):
                self.service._deliver()
        markdown_uploads = [upload for upload in self.bot.media_uploads
                            if upload[2].endswith('-methoden-digest.md')]
        self.assertEqual(len(markdown_uploads), 1)
        self.assertEqual(self.bot.file_attempts, [])

        DigestService(self.bot, self.config, self.state)._deliver()
        markdown_uploads = [upload for upload in self.bot.media_uploads
                            if upload[2].endswith('-methoden-digest.md')]
        self.assertEqual(len(markdown_uploads), 2)
        self.assertEqual(markdown_uploads[0][0], markdown_uploads[1][0])
        self.assertEqual(self.bot.media_creations, 2)
        self.assertEqual(
            self.state.snapshot()['digests']['2026-08-28']['recipients'][USER]['status'],
            'delivered')

    def test_markdown_html_formats_digest_and_escapes_source(self):
        markdown = ('# Titel & Befund\n\n**Berichtszeitraum:** 22.–28. August  \n'
                    '*Einordnung*\n\n- **DOI/Link:** '
                    '[Beleg](https://doi.org/10.1/example?x=1&y=2)\n'
                    '- <script>alert(1)</script>\n\n'
                    '[Unsicher](javascript:alert(1))\n')
        rendered = markdown_to_matrix_html(markdown)
        self.assertIn('<h1>Titel &amp; Befund</h1>', rendered)
        self.assertIn('<strong>Berichtszeitraum:</strong>', rendered)
        self.assertIn('<br><em>Einordnung</em>', rendered)
        self.assertIn('<ul><li><strong>DOI/Link:</strong> '
                      '<a href="https://doi.org/10.1/example?x=1&amp;y=2">Beleg</a></li>',
                      rendered)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', rendered)
        self.assertNotIn('javascript:', rendered)

    def test_same_date_with_changed_content_stops_fail_closed(self):
        inbox = Path(self.config.digest_inbox_dir)
        inbox.mkdir(mode=0o700)
        source = inbox / '2026-08-28-methoden-digest.bundle'
        ris = b'TY  - JOUR\nTI  - Beispiel\nER  -\n'
        source.write_bytes(pack_bundle(b'first\n', ris))
        source.chmod(0o600)
        self.service.process_inbox()
        source.write_bytes(pack_bundle(b'changed\n', ris))
        source.chmod(0o600)
        with self.assertRaisesRegex(DigestStateError, 'digest_date_hash_conflict'):
            self.service.process_inbox()

    def test_large_markdown_is_split_without_content_change(self):
        text = ('Änderung und Methode\n' * 10_000)
        parts = split_markdown(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(matrix_message_content(
            part, markdown_to_matrix_html(part))[1] <= MAX_EVENT_CONTENT_BYTES for part in parts))

    def test_restricted_receiver_checks_hash_and_writes_private_file(self):
        inbox = Path(self.directory.name) / 'upload'
        inbox.mkdir(mode=0o700)
        raw = pack_bundle(b'# Digest\n', b'TY  - JOUR\nTI  - Beispiel\nER  -\n')
        digest = hashlib.sha256(raw).hexdigest()
        command = 'digest-upload-v2 2026-08-28 ' + digest
        with (patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': command,
                                      'METHODENBOT_DIGEST_INBOX': str(inbox)}, clear=False),
              patch.object(digest_upload_receiver.sys, 'stdin', Stdin(raw))):
            self.assertEqual(digest_upload_receiver.main(), 0)
        target = inbox / '2026-08-28-methoden-digest.bundle'
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_uploader_requires_exact_remote_receipt(self):
        source = Path(self.directory.name) / '2026-08-28-methoden-digest.md'
        source.write_bytes(b'# Digest\n')
        ris = Path(self.directory.name) / '2026-08-28-methoden-artikel.ris'
        ris.write_bytes(b'TY  - JOUR\nTI  - Beispiel\nER  -\n')
        bundle = pack_bundle(source.read_bytes(), ris.read_bytes())
        digest = hashlib.sha256(bundle).hexdigest()
        expected = ('digest_upload_ok 2026-08-28 ' + digest + '\n').encode()
        completed = SimpleNamespace(returncode=0, stdout=expected, stderr=b'')
        with patch.object(digest_upload.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(digest_upload.upload(
                source, ris, 'methodenbot-digest-upload'), digest)
        self.assertEqual(run.call_args.args[0][0:5],
                         ['/usr/bin/ssh', '-T', '-o', 'BatchMode=yes',
                          'methodenbot-digest-upload'])
        self.assertEqual(run.call_args.kwargs['input'], bundle)

    def test_bundle_requires_valid_ris_records(self):
        with self.assertRaises(DigestBundleError):
            pack_bundle(b'# Digest\n', b'not ris\n')
        markdown, ris = unpack_bundle(pack_bundle(
            b'# Digest\n', b'TY  - JOUR\nTI  - Beispiel\nER  -\n'))
        self.assertEqual(markdown, b'# Digest\n')
        self.assertIn(b'TY  - JOUR', ris)

    def test_version_one_state_is_migrated_without_changing_deliveries(self):
        root = Path(self.directory.name) / 'legacy-state'
        root.mkdir(mode=0o700)
        legacy = {
            'version': 1, 'since': 's0', 'subscriptions': {}, 'completed_commands': [],
            'digests': {'2026-08-28': {
                'sha256': 'a' * 64, 'content_file': '2026-08-28-' + 'a' * 64 + '.md',
                'status': 'complete', 'recipients': {}}}}
        path = root / 'state.json'
        path.write_text(json.dumps(legacy) + '\n', encoding='utf-8')
        path.chmod(0o600)
        migrated = DigestState(root).snapshot()
        self.assertEqual(migrated['version'], 2)
        self.assertIsNone(migrated['digests']['2026-08-28']['ris_mxc'])
        self.assertFalse(migrated['digests']['2026-08-28']['ris_uploaded'])

    def test_existing_version_two_state_ignores_markdown_sidecars(self):
        root = Path(self.directory.name) / 'existing-v2-state'
        state = DigestState(root)
        state.bootstrap('cursor-42')
        state.apply_command('$sub-one', 'Digest', USER, ROOM)
        state.apply_command('$sub-two', 'Digest', SECOND_USER, SECOND_ROOM)
        digest_hash = 'a' * 64
        ris_hash = 'b' * 64
        content_file = '2026-08-28-' + digest_hash + '-' + ris_hash + '.md'
        state.stage_digest('2026-08-28', digest_hash, content_file, 2)
        state.record_part('2026-08-28', USER, 0, '$text-confirmed')
        state.record_digest_media('2026-08-28', 'mxc://example.org/ris-existing')
        state.mark_digest_media_uploaded(
            '2026-08-28', 'mxc://example.org/ris-existing')
        expected = state.snapshot()
        uri_sidecar = root / 'content' / (content_file + MARKDOWN_MEDIA_URI_SUFFIX)
        proof_sidecar = root / 'content' / (content_file + MARKDOWN_MEDIA_UPLOAD_SUFFIX)
        uri_sidecar.write_bytes(b'mxc://example.org/markdown-existing\n')
        proof_sidecar.write_bytes((b'c' * 64) + b'\n')
        uri_sidecar.chmod(0o600)
        proof_sidecar.chmod(0o600)

        reopened = DigestState(root).snapshot()
        self.assertEqual(reopened, expected)
        self.assertEqual(reopened['version'], 2)
        self.assertEqual(reopened['since'], 'cursor-42')
        self.assertEqual(reopened['completed_commands'], ['$sub-one', '$sub-two'])
        self.assertEqual(
            reopened['digests']['2026-08-28']['recipients'][USER]['parts'],
            ['$text-confirmed', None])
        self.assertEqual(
            reopened['digests']['2026-08-28']['recipients'][SECOND_USER]['parts'],
            [None, None])


if __name__ == '__main__':
    unittest.main()
