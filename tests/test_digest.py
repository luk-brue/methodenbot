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
from digest_service import (DigestService, DigestServiceError, _validate_fallback_room,
                            digest_command_from_event, markdown_to_matrix_html,
                            split_markdown)
from digest_state import DigestState, DigestStateError
import digest_upload_receiver
import digest_upload
from matrixbot import (DIGEST_FALLBACK_MARKER, MAX_EVENT_CONTENT_BYTES, MatrixError,
                       matrix_message_content)


BOT = '@methodenbot:example.org'
USER = '@reader:example.org'
ROOM = '!digest:example.org'
ENCRYPTED_USER = '@encrypted-reader:example.org'
SECOND_ENCRYPTED_USER = '@second-encrypted-reader:example.org'


def room_state(*, extra=None, encrypted=False):
    result = [
        {'type': 'm.room.member', 'state_key': BOT, 'content': {'membership': 'join'}},
        {'type': 'm.room.member', 'state_key': USER, 'content': {'membership': 'join'}},
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


class FakeBot:
    def __init__(self):
        self.user_id = BOT
        self.states = {ROOM: room_state()}
        self.sync_responses = []
        self.sync_calls = []
        self.joined = []
        self.created_rooms = []
        self.invited_users = []
        self.events = {}
        self.transactions = {}
        self.send_attempts = []
        self.file_attempts = []
        self.media_uploads = []

    def sync(self, **kwargs):
        self.sync_calls.append(copy.deepcopy(kwargs))
        response = self.sync_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    def join_room(self, room_id):
        self.joined.append(room_id)
        return room_id

    def get_room_state(self, room_id):
        return copy.deepcopy(self.states[room_id])

    def joined_room_ids(self):
        return sorted(self.states)

    def create_digest_fallback_room(self, user_id, target_sha256):
        room_id = '!fallback-' + str(len(self.created_rooms) + 1) + ':example.org'
        self.created_rooms.append((user_id, target_sha256, room_id))
        self.states[room_id] = fallback_room_state(user_id, target_sha256)
        return room_id

    def invite_user(self, room_id, user_id):
        self.invited_users.append((room_id, user_id))
        self.states[room_id].append({
            'type': 'm.room.member', 'state_key': user_id,
            'content': {'membership': 'invite'}})
        return True

    def send_message(self, msg, room_id=None, transaction_id=None, html_msg=None, **_kwargs):
        self.send_attempts.append((room_id, transaction_id, msg, html_msg))
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
        return 'mxc://example.org/ris' + str(len(self.media_uploads) + 1)

    def upload_media(self, content_uri, raw, filename, **_kwargs):
        self.media_uploads.append((content_uri, raw, filename))
        return content_uri

    def send_file(self, content_uri, filename, size, room_id=None, transaction_id=None):
        self.file_attempts.append((room_id, transaction_id, content_uri, filename, size))
        if transaction_id in self.transactions:
            return self.transactions[transaction_id]
        event_id = '$sent-' + str(len(self.transactions) + 1)
        self.transactions[transaction_id] = event_id
        self.events[room_id, event_id] = {
            'event_id': event_id, 'type': 'm.room.message', 'sender': BOT,
            'content': {
                'msgtype': 'm.file', 'body': filename, 'filename': filename,
                'url': content_uri,
                'info': {'mimetype': 'application/x-research-info-systems', 'size': size},
            }}
        return event_id

    def read_event(self, room_id, event_id):
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

    def test_limited_join_catch_up_keeps_original_cursor(self):
        self.state.bootstrap('s0')
        invited = '!invite-limited:example.org'
        self.bot.states[invited] = room_state()
        self.bot.sync_responses = [
            {'next_batch': 's1', 'rooms': {'invite': {invited: {
                'invite_state': {'events': [
                    {'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                     'content': {'membership': 'invite'}}]}}}}},
            {'next_batch': 'catch-up', 'rooms': {'join': {invited: {
                'timeline': {'limited': True,
                             'events': [event('$possibly-incomplete', 'Digest')]}}}}},
        ]
        with self.assertRaisesRegex(MatrixError, 'invalid_sync_response'):
            self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.state.snapshot()['since'], 's0')
        self.assertEqual(self.state.snapshot()['subscriptions'], {})

    def test_digest_command_immediately_sends_latest_newsletter_and_ris(self):
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
        self.assertEqual(len(self.bot.file_attempts), 1)
        self.assertEqual(self.bot.file_attempts[0][3], '2026-08-28-methoden-artikel.ris')
        self.assertEqual(self.bot.media_uploads[0][1], ris)

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

        response['next_batch'] = 's2'
        self.bot.sync_responses = [response]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(len(self.bot.created_rooms), 2)
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
        self.assertEqual(len(self.bot.file_attempts), 1)
        self.assertEqual(self.bot.file_attempts[0][3], '2026-08-28-methoden-artikel.ris')
        self.assertEqual(self.bot.media_uploads[0][1], ris.encode())

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


if __name__ == '__main__':
    unittest.main()
