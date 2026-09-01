import json
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest

from matrixbot import DIGEST_FALLBACK_MARKER, MatrixBot, MatrixError


class Response:
    def __init__(self, status, data, headers=None):
        self.status_code, self.data = status, data
        self.headers, self.content = headers or {}, b'{}'

    def json(self):
        return self.data


class API:
    def __init__(self):
        self.logins = 0
        self.requests = []
        self.send_responses = [Response(401, {'errcode': 'M_UNKNOWN_TOKEN'}),
                               Response(200, {'event_id': '$accepted'})]

    def factory(self):
        api = self

        class Session:
            trust_env = True

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, **kwargs):
                api.logins += 1
                return Response(200, {'access_token': 'token-' + str(api.logins),
                                      'device_id': 'STABLE', 'user_id': '@bot:example.invalid'})

            def request(self, method, url, **kwargs):
                api.requests.append((method, url, kwargs))
                return api.send_responses.pop(0)

        return Session()


class MatrixFinalTests(unittest.TestCase):
    @staticmethod
    def config():
        return SimpleNamespace(matrix_server='https://matrix.example.invalid',
                               matrix_user='@bot:example.invalid', matrix_password='secret',
                               matrix_room_id='!room:example.invalid', matrix_device_id='STABLE')

    def test_ris_media_is_created_uploaded_and_sent_as_matrix_file(self):
        api = API()
        api.send_responses = [
            Response(200, {'content_uri': 'mxc://matrix.example.invalid/ris123'}),
            Response(200, {}),
            Response(200, {'event_id': '$file'}),
        ]
        bot = MatrixBot(self.config(), session_factory=api.factory, sleep=lambda seconds: None)
        uri = bot.create_media_uri()
        raw = b'TY  - JOUR\nTI  - Beispiel\nER  -\n'
        filename = '2026-08-28-methoden-artikel.ris'
        self.assertEqual(bot.upload_media(uri, raw, filename), uri)
        self.assertEqual(bot.send_file(
            uri, filename, len(raw), transaction_id='ris-file'), '$file')
        self.assertEqual(api.requests[0][0:2], (
            'POST', 'https://matrix.example.invalid/_matrix/media/v1/create'))
        self.assertIn('/_matrix/media/v3/upload/matrix.example.invalid/ris123',
                      api.requests[1][1])
        self.assertEqual(api.requests[1][2]['data'], raw)
        self.assertEqual(api.requests[1][2]['headers']['Content-Type'],
                         'application/x-research-info-systems')
        content = api.requests[2][2]['json']
        self.assertEqual(content['msgtype'], 'm.file')
        self.assertEqual(content['url'], uri)
        self.assertEqual(content['info']['size'], len(raw))

    def test_markdown_media_is_uploaded_and_sent_as_matrix_file(self):
        api = API()
        api.send_responses = [
            Response(200, {}),
            Response(200, {'event_id': '$markdown-file'}),
        ]
        bot = MatrixBot(self.config(), session_factory=api.factory, sleep=lambda seconds: None)
        uri = 'mxc://matrix.example.invalid/markdown123'
        raw = b'# Methoden-Journal-Digest\n'
        filename = '2026-08-28-methoden-digest.md'
        self.assertEqual(bot.upload_media(uri, raw, filename), uri)
        self.assertEqual(bot.send_file(
            uri, filename, len(raw), transaction_id='markdown-file'), '$markdown-file')
        self.assertEqual(api.requests[0][2]['data'], raw)
        self.assertEqual(api.requests[0][2]['params'], {'filename': filename})
        self.assertEqual(api.requests[0][2]['headers']['Content-Type'],
                         'text/markdown; charset=utf-8')
        content = api.requests[1][2]['json']
        self.assertEqual(content, {
            'msgtype': 'm.file', 'body': filename, 'filename': filename,
            'url': uri,
            'info': {'mimetype': 'text/markdown; charset=utf-8', 'size': len(raw)},
        })

    def test_matrix_files_reject_unknown_names_and_mimetype_mismatches(self):
        bot = MatrixBot(self.config(), session_factory=API().factory,
                        sleep=lambda seconds: None)
        uri = 'mxc://matrix.example.invalid/file123'
        for filename in ('digest.md', '2026-08-28-methoden-digest.txt'):
            with self.subTest(filename=filename):
                with self.assertRaisesRegex(MatrixError, 'invalid_matrix_media'):
                    bot.upload_media(uri, b'data', filename)
                with self.assertRaisesRegex(MatrixError, 'invalid_matrix_file'):
                    bot.send_file(uri, filename, 4, transaction_id='file')
        with self.assertRaisesRegex(MatrixError, 'invalid_matrix_media'):
            bot.upload_media(
                uri, b'# Digest\n', '2026-08-28-methoden-digest.md',
                content_type='application/x-research-info-systems')
        with self.assertRaisesRegex(MatrixError, 'invalid_matrix_media'):
            bot.upload_media(
                uri, b'TY  - JOUR\nER  -\n', '2026-08-28-methoden-artikel.ris',
                content_type='text/markdown; charset=utf-8')

    def test_repeated_media_put_accepts_already_uploaded_response(self):
        api = API()
        api.send_responses = [Response(409, {'errcode': 'M_CANNOT_OVERWRITE_MEDIA'})]
        bot = MatrixBot(self.config(), session_factory=api.factory, sleep=lambda seconds: None)
        raw = b'TY  - JOUR\nTI  - Beispiel\nER  -\n'
        self.assertEqual(bot.upload_media(
            'mxc://matrix.example.invalid/already', raw,
            '2026-08-28-methoden-artikel.ris'),
            'mxc://matrix.example.invalid/already')

        api.send_responses = [Response(409, {'errcode': 'M_CANNOT_OVERWRITE_MEDIA'})]
        markdown = b'# Methoden-Journal-Digest\n'
        self.assertEqual(bot.upload_media(
            'mxc://matrix.example.invalid/already-markdown', markdown,
            '2026-08-28-methoden-digest.md'),
            'mxc://matrix.example.invalid/already-markdown')

    def test_join_room_requires_confirmed_matching_room(self):
        api = API()
        api.send_responses = [Response(200, {'room_id': '!private:example.invalid'})]
        config = SimpleNamespace(matrix_server='https://matrix.example.invalid',
                                 matrix_user='@bot:example.invalid', matrix_password='secret',
                                 matrix_room_id='!room:example.invalid', matrix_device_id='STABLE')
        bot = MatrixBot(config, session_factory=api.factory, sleep=lambda seconds: None)
        self.assertEqual(bot.join_room('!private:example.invalid'), '!private:example.invalid')
        self.assertTrue(api.requests[-1][1].endswith('/join/%21private%3Aexample.invalid'))

    def test_joined_rooms_are_strictly_validated(self):
        api = API()
        api.send_responses = [Response(200, {
            'joined_rooms': ['!one:example.invalid', '!two:example.invalid']})]
        bot = MatrixBot(self.config(), session_factory=api.factory, sleep=lambda seconds: None)
        self.assertEqual(bot.joined_room_ids(),
                         ['!one:example.invalid', '!two:example.invalid'])
        self.assertTrue(api.requests[-1][1].endswith('/joined_rooms'))

    def test_room_messages_reads_exact_forward_bounded_range(self):
        api = API()
        room_id = '!private:example.invalid'
        page = {
            'start': 'sync-old', 'end': 'timeline-start',
            'chunk': [{
                'room_id': room_id, 'event_id': '$one', 'type': 'm.room.message',
                'sender': '@reader:example.invalid',
                'content': {'msgtype': 'm.text', 'body': 'Digest'},
            }],
        }
        api.send_responses = [Response(200, page)]
        bot = MatrixBot(self.config(), session_factory=api.factory, sleep=lambda seconds: None)
        self.assertEqual(bot.room_messages(
            room_id, from_token='sync-old', to_token='timeline-start'), page)
        method, url, kwargs = api.requests[-1]
        self.assertEqual(method, 'GET')
        self.assertTrue(url.endswith(
            '/rooms/%21private%3Aexample.invalid/messages'))
        self.assertEqual(kwargs['params'], {
            'dir': 'f', 'from': 'sync-old', 'to': 'timeline-start', 'limit': 50})

    def test_room_messages_accepts_empty_page_with_progress_token(self):
        api = API()
        api.send_responses = [Response(200, {
            'start': 'sync-old', 'end': 'next-page', 'chunk': []})]
        bot = MatrixBot(self.config(), session_factory=api.factory, sleep=lambda seconds: None)
        self.assertEqual(bot.room_messages(
            '!private:example.invalid', from_token='sync-old',
            to_token='timeline-start')['end'], 'next-page')

    def test_room_messages_rejects_malformed_schema_and_no_progress(self):
        room_id = '!private:example.invalid'
        invalid_pages = [
            {'start': 'wrong', 'chunk': []},
            {'start': 'sync-old', 'chunk': {}},
            {'start': 'sync-old', 'end': 'sync-old', 'chunk': []},
            {'start': 'sync-old', 'chunk': [{'room_id': '!other:example.invalid'}]},
            {'start': 'sync-old', 'chunk': [
                {'room_id': room_id} for _index in range(51)]},
        ]
        for page in invalid_pages:
            with self.subTest(page=page):
                api = API()
                api.send_responses = [Response(200, page)]
                bot = MatrixBot(
                    self.config(), session_factory=api.factory,
                    sleep=lambda seconds: None)
                with self.assertRaisesRegex(
                        MatrixError, 'invalid_matrix_messages_response'):
                    bot.room_messages(
                        room_id, from_token='sync-old',
                        to_token='timeline-start')

    def test_private_room_invite_uses_fixed_user_payload_without_retry(self):
        api = API()
        api.send_responses = [Response(200, {})]
        bot = MatrixBot(self.config(), session_factory=api.factory, sleep=lambda seconds: None)
        self.assertTrue(bot.invite_user(
            '!fallback:example.invalid', '@reader:example.invalid'))
        method, url, kwargs = api.requests[-1]
        self.assertEqual(method, 'POST')
        self.assertTrue(url.endswith('/rooms/%21fallback%3Aexample.invalid/invite'))
        self.assertEqual(kwargs['json'], {'user_id': '@reader:example.invalid'})

    def test_digest_fallback_room_has_private_marker_and_hardened_power_levels(self):
        api = API()
        api.send_responses = [Response(200, {'room_id': '!fallback:example.invalid'})]
        bot = MatrixBot(self.config(), session_factory=api.factory, sleep=lambda seconds: None)
        target = '@reader:example.invalid'
        target_hash = 'a' * 64
        self.assertEqual(bot.create_digest_fallback_room(target, target_hash),
                         '!fallback:example.invalid')
        method, url, kwargs = api.requests[-1]
        self.assertEqual(method, 'POST')
        self.assertTrue(url.endswith('/createRoom'))
        payload = kwargs['json']
        self.assertEqual(payload['visibility'], 'private')
        self.assertEqual(payload['preset'], 'private_chat')
        self.assertTrue(payload['is_direct'])
        self.assertEqual(payload['invite'], [target])
        self.assertEqual(payload['creation_content'][DIGEST_FALLBACK_MARKER], {
            'version': 1, 'target_sha256': target_hash})
        states = {value['type']: value['content'] for value in payload['initial_state']}
        self.assertEqual(states['m.room.join_rules'], {'join_rule': 'invite'})
        self.assertEqual(states['m.room.guest_access'], {'guest_access': 'forbidden'})
        self.assertNotIn('m.room.encryption', states)
        levels = payload['power_level_content_override']
        self.assertEqual(levels['users_default'], 0)
        self.assertEqual(levels['events_default'], 0)
        self.assertEqual(levels['state_default'], 100)
        self.assertEqual(levels['invite'], 100)
        self.assertEqual(levels['events']['m.room.encryption'], 100)
        self.assertEqual(levels['events']['m.room.tombstone'], 150)

    def test_digest_fallback_create_is_never_retried_blindly(self):
        api = API()
        api.send_responses = [Response(500, {'errcode': 'M_UNKNOWN'})]
        bot = MatrixBot(self.config(), session_factory=api.factory, sleep=lambda seconds: None)
        with self.assertRaisesRegex(MatrixError, 'matrix_http_error'):
            bot.create_digest_fallback_room('@reader:example.invalid', 'a' * 64)
        self.assertEqual(len(api.requests), 1)

    def test_401_refresh_uses_new_header_and_identical_transaction_url(self):
        api = API()
        config = SimpleNamespace(matrix_server='https://matrix.example.invalid',
                                 matrix_user='@bot:example.invalid', matrix_password='secret',
                                 matrix_room_id='!room:example.invalid', matrix_device_id='STABLE')
        bot = MatrixBot(config, session_factory=api.factory, sleep=lambda seconds: None)
        self.assertEqual(bot.send_message('hello', transaction_id='same-tx'), '$accepted')
        self.assertEqual(api.logins, 2)
        self.assertEqual(api.requests[0][1], api.requests[1][1])
        self.assertTrue(api.requests[0][1].endswith('/same-tx'))
        self.assertEqual(api.requests[0][2]['headers']['Authorization'], 'Bearer token-1')
        self.assertEqual(api.requests[1][2]['headers']['Authorization'], 'Bearer token-2')
        self.assertEqual(api.requests[0][2]['json'], api.requests[1][2]['json'])

    def test_protected_session_is_reused_across_process_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = str(Path(directory) / 'matrix-session.json')
            api = API()
            api.send_responses = [Response(200, {'user_id': '@bot:example.invalid'})]
            config = SimpleNamespace(matrix_server='https://matrix.example.invalid',
                                     matrix_user='@bot:example.invalid', matrix_password='secret',
                                     matrix_room_id='!room:example.invalid', matrix_device_id='STABLE',
                                     matrix_token_file=token_file)
            MatrixBot(config, session_factory=api.factory, sleep=lambda seconds: None)
            self.assertEqual(api.logins, 1)
            self.assertEqual(Path(token_file).stat().st_mode & 0o777, 0o600)
            MatrixBot(config, session_factory=api.factory, sleep=lambda seconds: None)
            self.assertEqual(api.logins, 1)
            self.assertEqual(api.requests[-1][2]['headers']['Authorization'], 'Bearer token-1')

    def test_cached_session_cannot_be_reused_for_changed_login_user(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = str(Path(directory) / 'matrix-session.json')
            api = API()
            first = SimpleNamespace(matrix_server='https://matrix.example.invalid',
                                    matrix_user='@old:example.invalid', matrix_password='secret',
                                    matrix_room_id='!room:example.invalid', matrix_device_id='STABLE',
                                    matrix_token_file=token_file)
            MatrixBot(first, session_factory=api.factory, sleep=lambda seconds: None)
            changed = SimpleNamespace(**{**first.__dict__, 'matrix_user': '@new:example.invalid'})
            with self.assertRaisesRegex(MatrixError, 'matrix_token_file_invalid'):
                MatrixBot(changed, session_factory=api.factory, sleep=lambda seconds: None)

    def test_wire_encoded_non_ascii_payload_is_rejected_before_request(self):
        api = API()
        api.send_responses = []
        config = SimpleNamespace(matrix_server='https://matrix.example.invalid',
                                 matrix_user='@bot:example.invalid', matrix_password='secret',
                                 matrix_room_id='!room:example.invalid', matrix_device_id='STABLE')
        bot = MatrixBot(config, session_factory=api.factory, sleep=lambda seconds: None)
        with self.assertRaisesRegex(MatrixError, 'matrix_message_too_large'):
            bot.send_message('\U0001f600' * 5000, transaction_id='oversized')
        self.assertEqual(api.requests, [])

    def test_cached_401_refresh_is_persisted_for_next_process(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = str(Path(directory) / 'matrix-session.json')
            config = SimpleNamespace(matrix_server='https://matrix.example.invalid',
                                     matrix_user='@bot:example.invalid', matrix_password='secret',
                                     matrix_room_id='!room:example.invalid', matrix_device_id='STABLE',
                                     matrix_token_file=token_file)
            first_api = API()
            MatrixBot(config, session_factory=first_api.factory, sleep=lambda seconds: None)

            refresh_api = API()
            refresh_api.logins = 1
            refresh_api.send_responses = [
                Response(200, {'user_id': '@bot:example.invalid', 'device_id': 'STABLE'}),
                Response(401, {'errcode': 'M_UNKNOWN_TOKEN'}),
                Response(200, {'event_id': '$after-refresh'}),
            ]
            cached = MatrixBot(config, session_factory=refresh_api.factory,
                               sleep=lambda seconds: None)
            self.assertEqual(cached.send_message('hello', transaction_id='stable'), '$after-refresh')
            self.assertEqual(json.loads(Path(token_file).read_text())['access_token'], 'token-2')

            next_api = API()
            next_api.send_responses = [
                Response(200, {'user_id': '@bot:example.invalid', 'device_id': 'STABLE'})]
            reused = MatrixBot(config, session_factory=next_api.factory, sleep=lambda seconds: None)
            self.assertEqual(reused.access_token, 'token-2')
            self.assertEqual(next_api.logins, 0)

    def test_cached_device_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = str(Path(directory) / 'matrix-session.json')
            config = SimpleNamespace(matrix_server='https://matrix.example.invalid',
                                     matrix_user='@bot:example.invalid', matrix_password='secret',
                                     matrix_room_id='!room:example.invalid', matrix_device_id='STABLE',
                                     matrix_token_file=token_file)
            first_api = API()
            MatrixBot(config, session_factory=first_api.factory, sleep=lambda seconds: None)
            changed_api = API()
            changed_api.send_responses = [Response(200, {
                'user_id': '@bot:example.invalid', 'device_id': 'OTHER'})]
            with self.assertRaisesRegex(MatrixError, 'matrix_cached_identity_changed'):
                MatrixBot(config, session_factory=changed_api.factory, sleep=lambda seconds: None)


if __name__ == '__main__':
    unittest.main()
