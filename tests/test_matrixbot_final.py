import json
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest

from matrixbot import MatrixBot, MatrixError


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

    def test_repeated_media_put_accepts_already_uploaded_response(self):
        api = API()
        api.send_responses = [Response(409, {'errcode': 'M_CANNOT_OVERWRITE_MEDIA'})]
        bot = MatrixBot(self.config(), session_factory=api.factory, sleep=lambda seconds: None)
        raw = b'TY  - JOUR\nTI  - Beispiel\nER  -\n'
        self.assertEqual(bot.upload_media(
            'mxc://matrix.example.invalid/already', raw,
            '2026-08-28-methoden-artikel.ris'),
            'mxc://matrix.example.invalid/already')

    def test_join_room_requires_confirmed_matching_room(self):
        api = API()
        api.send_responses = [Response(200, {'room_id': '!private:example.invalid'})]
        config = SimpleNamespace(matrix_server='https://matrix.example.invalid',
                                 matrix_user='@bot:example.invalid', matrix_password='secret',
                                 matrix_room_id='!room:example.invalid', matrix_device_id='STABLE')
        bot = MatrixBot(config, session_factory=api.factory, sleep=lambda seconds: None)
        self.assertEqual(bot.join_room('!private:example.invalid'), '!private:example.invalid')
        self.assertTrue(api.requests[-1][1].endswith('/join/%21private%3Aexample.invalid'))

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
