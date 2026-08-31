import copy
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from digest_service import (DigestService, DigestServiceError, digest_command_from_event,
                            markdown_to_matrix_html, split_markdown)
from digest_state import DigestState, DigestStateError
import digest_upload_receiver
import digest_upload
from matrixbot import MAX_EVENT_CONTENT_BYTES, matrix_message_content


BOT = '@methodenbot:example.org'
USER = '@reader:example.org'
ROOM = '!digest:example.org'


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
        self.joined = []
        self.events = {}
        self.transactions = {}
        self.send_attempts = []

    def sync(self, **_kwargs):
        return copy.deepcopy(self.sync_responses.pop(0))

    def join_room(self, room_id):
        self.joined.append(room_id)
        return room_id

    def get_room_state(self, room_id):
        return copy.deepcopy(self.states[room_id])

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

    def test_only_exact_plain_digest_commands_are_accepted(self):
        self.assertEqual(digest_command_from_event(event('$1', ' Digest ')),
                         ('$1', USER, 'Digest'))
        self.assertEqual(digest_command_from_event(event('$2', 'Digest aus')),
                         ('$2', USER, 'Digest aus'))
        for candidate in (
                event('$3', 'digest'),
                event('$4', 'Digest', extra={'format': 'org.matrix.custom.html',
                                             'formatted_body': 'Digest'}),
                event('$5', 'Digest', extra={'m.relates_to': {'rel_type': 'm.replace'}}),
                {'event_id': '$6', 'type': 'm.reaction', 'sender': USER,
                 'content': {'body': 'Digest'}}):
            self.assertIsNone(digest_command_from_event(candidate))

    def test_bootstrap_ignores_old_commands(self):
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'join': {ROOM: {
            'timeline': {'events': [event('$old', 'Digest')]}}}}}]
        self.assertTrue(self.service.bootstrap())
        self.assertEqual(self.state.snapshot()['subscriptions'], {})
        self.assertEqual(self.bot.send_attempts, [])

    def test_user_room_is_joined_and_digest_command_subscribes(self):
        self.state.bootstrap('s0')
        invited = '!invite:example.org'
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {
            'invite': {invited: {'invite_state': {'events': [
                {'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                 'content': {'membership': 'invite'}}]}}},
            'join': {ROOM: {'timeline': {'events': [event('$subscribe', 'Digest')]}}}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.joined, [invited])
        self.assertEqual(self.state.snapshot()['subscriptions'][USER]['room_id'], ROOM)
        self.assertIn('Digest aktiviert', self.bot.send_attempts[-1][2])

    def test_encrypted_invite_is_not_joined(self):
        self.state.bootstrap('s0')
        invited = '!encrypted:example.org'
        self.bot.sync_responses = [{'next_batch': 's1', 'rooms': {'invite': {invited: {
            'invite_state': {'events': [
                {'type': 'm.room.encryption', 'state_key': '', 'content': {}},
                {'type': 'm.room.member', 'state_key': BOT, 'sender': USER,
                 'content': {'membership': 'invite'}}]}}}}}]
        self.service.poll_once(timeout_ms=0)
        self.assertEqual(self.bot.joined, [])

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
        source = inbox / '2026-08-28-methoden-digest.md'
        source.write_text(markdown, encoding='utf-8')
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
        source = inbox / '2026-08-28-methoden-digest.md'
        source.write_text('first\n')
        source.chmod(0o600)
        self.service.process_inbox()
        source.write_text('changed\n')
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
        raw = b'# Digest\n'
        digest = hashlib.sha256(raw).hexdigest()
        command = 'digest-upload 2026-08-28-methoden-digest.md ' + digest
        with (patch.dict(os.environ, {'SSH_ORIGINAL_COMMAND': command,
                                      'METHODENBOT_DIGEST_INBOX': str(inbox)}, clear=False),
              patch.object(digest_upload_receiver.sys, 'stdin', Stdin(raw))):
            self.assertEqual(digest_upload_receiver.main(), 0)
        target = inbox / '2026-08-28-methoden-digest.md'
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_uploader_requires_exact_remote_receipt(self):
        source = Path(self.directory.name) / '2026-08-28-methoden-digest.md'
        source.write_bytes(b'# Digest\n')
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        expected = ('digest_upload_ok ' + source.name + ' ' + digest + '\n').encode()
        completed = SimpleNamespace(returncode=0, stdout=expected, stderr=b'')
        with patch.object(digest_upload.subprocess, 'run', return_value=completed) as run:
            self.assertEqual(digest_upload.upload(source, 'methodenbot-digest-upload'), digest)
        self.assertEqual(run.call_args.args[0][0:5],
                         ['/usr/bin/ssh', '-T', '-o', 'BatchMode=yes',
                          'methodenbot-digest-upload'])
        self.assertEqual(run.call_args.kwargs['input'], source.read_bytes())


if __name__ == '__main__':
    unittest.main()
