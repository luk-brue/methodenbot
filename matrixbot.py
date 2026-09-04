"""Small, non-E2EE Matrix client used by the Methodenbot.

All retries of a send retain the same Matrix transaction identifier. Response
bodies are never logged because they can contain message text or credentials.
"""

import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid
from urllib.parse import quote, urlsplit

import requests


logger = logging.getLogger(__name__)
MAX_EVENT_CONTENT_BYTES = 48_000
MAX_MATRIX_FILE_BYTES = 1_000_000
MATRIX_RIS_FILE_NAME = re.compile(r'\d{4}-\d{2}-\d{2}-methoden-artikel\.ris')
MATRIX_MARKDOWN_FILE_NAME = re.compile(r'\d{4}-\d{2}-\d{2}-methoden-digest\.md')
MATRIX_CONTENT_URI = re.compile(r'mxc://([A-Za-z0-9._:-]+)/([A-Za-z0-9_-]+)')
DIGEST_FALLBACK_MARKER = 'de.uni-kassel.methodenbot.digest_fallback'


def matrix_file_mimetype(filename):
    if isinstance(filename, str):
        if MATRIX_RIS_FILE_NAME.fullmatch(filename):
            return 'application/x-research-info-systems'
        if MATRIX_MARKDOWN_FILE_NAME.fullmatch(filename):
            return 'text/markdown; charset=utf-8'
    return None


class MatrixError(RuntimeError):
    def __init__(self, code, *, status=None):
        super().__init__(code)
        self.status = status


def matrix_message_content(msg, html_msg=None, thread_reply_to=None):
    """Build and wire-measure exactly the content sent through requests' json= API."""
    if not isinstance(msg, str) or not msg:
        raise MatrixError('invalid_matrix_message')
    content = {'msgtype': 'm.text', 'body': msg}
    if thread_reply_to is not None:
        if not isinstance(thread_reply_to, str) or not thread_reply_to.startswith('$'):
            raise MatrixError('invalid_matrix_thread_root')
        content['m.relates_to'] = {'rel_type': 'm.thread', 'event_id': thread_reply_to}
    if html_msg is not None:
        if not isinstance(html_msg, str):
            raise MatrixError('invalid_matrix_html')
        content.update({'format': 'org.matrix.custom.html', 'formatted_body': html_msg})
    size = len(json.dumps(content, ensure_ascii=True, allow_nan=False).encode('utf-8'))
    return content, size


class MatrixBot:
    """Password-authenticated Matrix client without encryption support."""

    def __init__(self, envvars, *, session_factory=requests.Session, sleep=time.sleep):
        endpoint = urlsplit((envvars.matrix_server or '').rstrip('/'))
        if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment):
            raise MatrixError('invalid_matrix_endpoint')
        self.homeserver = (envvars.matrix_server or '').rstrip('/')
        self.username = envvars.matrix_user
        self.password = envvars.matrix_password
        self.room_id = envvars.matrix_room_id
        self.requested_device_id = getattr(envvars, 'matrix_device_id', None)
        token_file = getattr(envvars, 'matrix_token_file', None)
        self.token_file = Path(token_file) if isinstance(token_file, str) and token_file else None
        if not isinstance(self.requested_device_id, str) or not self.requested_device_id.strip():
            raise MatrixError('missing_matrix_device_id')
        self._session_factory = session_factory
        self._sleep = sleep
        self._login_lock = threading.Lock()
        self.access_token = None
        self.device_id = None
        self.user_id = None
        if not self._load_token():
            self.password_login()
        else:
            body = self.request_json('GET', '/account/whoami')
            if (body.get('user_id') != self.user_id
                    or body.get('device_id', self.device_id) != self.device_id):
                raise MatrixError('matrix_cached_identity_changed')

    def _session(self):
        session = self._session_factory()
        if hasattr(session, 'trust_env'):
            session.trust_env = False
        return session

    @staticmethod
    def _json(response):
        content = getattr(response, 'content', b'')
        if isinstance(content, (bytes, bytearray)) and len(content) > 2_000_000:
            raise MatrixError('matrix_response_too_large')
        try:
            value = response.json()
        except (ValueError, TypeError, RecursionError):
            raise MatrixError('matrix_invalid_json', status=getattr(response, 'status_code', None)) from None
        if not isinstance(value, dict):
            raise MatrixError('matrix_invalid_json', status=getattr(response, 'status_code', None))
        return value

    def _load_token(self):
        if self.token_file is None:
            return False
        try:
            os.lstat(self.token_file)
        except FileNotFoundError:
            return False
        try:
            fd = os.open(self.token_file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as handle:
                metadata = os.fstat(handle.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_uid != os.geteuid() or metadata.st_size > 20_000):
                    raise MatrixError('unsafe_matrix_token_file')
                raw = handle.read(20_001)
            value = json.loads(raw.decode('utf-8'))
        except MatrixError:
            raise
        except (OSError, UnicodeError, ValueError, RecursionError):
            raise MatrixError('matrix_token_file_unreadable') from None
        if (not isinstance(value, dict) or set(value) != {
                'version', 'access_token', 'device_id', 'user_id', 'login_user'}
                or value.get('version') != 2 or not isinstance(value.get('access_token'), str)
                or not value['access_token'] or value.get('device_id') != self.requested_device_id
                or value.get('login_user') != self.username
                or not isinstance(value.get('user_id'), str) or not value['user_id'].startswith('@')):
            raise MatrixError('matrix_token_file_invalid')
        self.access_token = value['access_token']
        self.device_id = value['device_id']
        self.user_id = value['user_id']
        return True

    def _store_token(self):
        if self.token_file is None:
            return
        self.token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = os.lstat(self.token_file.parent)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise MatrixError('unsafe_matrix_token_directory')
        try:
            existing = os.lstat(self.token_file)
            if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()
                    or stat.S_IMODE(existing.st_mode) != 0o600):
                raise MatrixError('unsafe_matrix_token_file')
        except FileNotFoundError:
            pass
        data = {'version': 2, 'access_token': self.access_token, 'login_user': self.username,
                'device_id': self.device_id, 'user_id': self.user_id}
        raw = (json.dumps(data, separators=(',', ':')) + '\n').encode('utf-8')
        temporary = self.token_file.parent / ('.matrix-session.' + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.token_file)
            directory_fd = os.open(self.token_file.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def password_login(self, expected_old_token=None):
        """Refresh once; another thread's newer token is never overwritten."""
        with self._login_lock:
            if expected_old_token is not None and self.access_token != expected_old_token:
                return
            payload = {
                'type': 'm.login.password',
                'identifier': {'type': 'm.id.user', 'user': self.username},
                'password': self.password,
                'device_id': self.requested_device_id,
                'initial_device_display_name': 'Methodenbot final',
            }
            try:
                with self._session() as session:
                    response = session.post(self.homeserver + '/_matrix/client/v3/login', json=payload,
                                            timeout=(5, 20), allow_redirects=False)
            except requests.RequestException:
                raise MatrixError('matrix_login_network_error') from None
            if response.status_code != 200:
                raise MatrixError('matrix_login_failed', status=response.status_code)
            body = self._json(response)
            token, device_id = body.get('access_token'), body.get('device_id')
            user_id = body.get('user_id', self.username)
            if (not isinstance(token, str) or not token or not isinstance(device_id, str)
                    or device_id != self.requested_device_id or not isinstance(user_id, str)
                    or not user_id.startswith('@')):
                raise MatrixError('matrix_login_response_invalid')
            self.access_token, self.device_id, self.user_id = token, device_id, user_id
            self._store_token()
            logger.info('Matrix-Bot erfolgreich angemeldet.')

    @staticmethod
    def _retry_delay(response):
        header = response.headers.get('Retry-After') if hasattr(response, 'headers') else None
        try:
            if header is not None:
                return max(0, math.ceil(float(header)))
        except (TypeError, ValueError):
            pass
        try:
            value = response.json().get('retry_after_ms')
            if type(value) in (int, float) and value >= 0:
                return math.ceil(value / 1000)
        except (ValueError, TypeError, AttributeError):
            pass
        return 1

    def request_json(self, method, path, *, payload=None, params=None, expected=(200,),
                     authenticated=True, idempotent=None, timeout=(5, 30)):
        if method not in ('GET', 'POST', 'PUT') or not isinstance(path, str) or not path.startswith('/'):
            raise MatrixError('matrix_request_not_allowed')
        if idempotent is None:
            idempotent = method in ('GET', 'PUT')
        refreshed = False
        attempts = 0
        while True:
            attempts += 1
            token = self.access_token
            headers = {'Authorization': 'Bearer ' + token} if authenticated else {}
            try:
                with self._session() as session:
                    response = session.request(method, self.homeserver + '/_matrix/client/v3' + path,
                                               headers=headers, json=payload, params=params, timeout=timeout,
                                               allow_redirects=False)
            except requests.RequestException:
                if idempotent and attempts < 3:
                    self._sleep(1)
                    continue
                raise MatrixError('matrix_network_error') from None
            if authenticated and response.status_code == 401 and not refreshed:
                self.password_login(expected_old_token=token)
                refreshed = True
                continue
            if response.status_code == 429 and idempotent and attempts < 3:
                delay = self._retry_delay(response)
                if delay <= 120:
                    self._sleep(delay)
                    continue
            if response.status_code not in expected:
                if idempotent and response.status_code in (500, 502, 503, 504) and attempts < 3:
                    self._sleep(1)
                    continue
                raise MatrixError('matrix_http_error', status=response.status_code)
            return self._json(response)

    def request_media_json(self, method, path, *, raw=None, content_type=None,
                           params=None, expected=(200,), idempotent=False,
                           timeout=(5, 30)):
        allowed = (path == '/_matrix/media/v1/create'
                   or path.startswith('/_matrix/media/v3/upload/'))
        if method not in ('POST', 'PUT') or not allowed:
            raise MatrixError('matrix_media_request_not_allowed')
        if raw is not None and (not isinstance(raw, bytes) or len(raw) > MAX_MATRIX_FILE_BYTES):
            raise MatrixError('invalid_matrix_media')
        refreshed = False
        attempts = 0
        while True:
            attempts += 1
            token = self.access_token
            headers = {'Authorization': 'Bearer ' + token}
            if content_type is not None:
                headers['Content-Type'] = content_type
            try:
                with self._session() as session:
                    response = session.request(
                        method, self.homeserver + path, headers=headers, data=raw,
                        params=params, timeout=timeout, allow_redirects=False)
            except requests.RequestException:
                if idempotent and attempts < 3:
                    self._sleep(1)
                    continue
                raise MatrixError('matrix_media_network_error') from None
            if response.status_code == 401 and not refreshed:
                self.password_login(expected_old_token=token)
                refreshed = True
                continue
            if response.status_code == 429 and idempotent and attempts < 3:
                delay = self._retry_delay(response)
                if delay <= 120:
                    self._sleep(delay)
                    continue
            if response.status_code not in expected:
                if idempotent and response.status_code in (500, 502, 503, 504) and attempts < 3:
                    self._sleep(1)
                    continue
                raise MatrixError('matrix_media_http_error', status=response.status_code)
            return self._json(response)

    def create_media_uri(self):
        body = self.request_media_json('POST', '/_matrix/media/v1/create')
        content_uri = body.get('content_uri')
        if not isinstance(content_uri, str) or MATRIX_CONTENT_URI.fullmatch(content_uri) is None:
            raise MatrixError('matrix_media_create_unconfirmed')
        return content_uri

    def upload_media(self, content_uri, raw, filename, content_type=None):
        match = MATRIX_CONTENT_URI.fullmatch(content_uri or '')
        expected_type = matrix_file_mimetype(filename)
        if (match is None or not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_MATRIX_FILE_BYTES
                or expected_type is None
                or content_type not in (None, expected_type)):
            raise MatrixError('invalid_matrix_media')
        content_type = expected_type
        path = ('/_matrix/media/v3/upload/' + quote(match.group(1), safe=':') + '/'
                + quote(match.group(2), safe=''))
        self.request_media_json('PUT', path, raw=raw, content_type=content_type,
                                params={'filename': filename}, expected=(200, 409),
                                idempotent=True, timeout=(5, 60))
        return content_uri

    def token_whoami(self):
        body = self.request_json('GET', '/account/whoami')
        user_id = body.get('user_id')
        if (not isinstance(user_id, str) or user_id != self.user_id
                or body.get('device_id', self.device_id) != self.device_id):
            raise MatrixError('matrix_identity_changed')
        return 200

    def _send_content(self, content, room_id, transaction_id):
        room_id = room_id or self.room_id
        if not isinstance(room_id, str) or not room_id.startswith('!'):
            raise MatrixError('invalid_matrix_room')
        transaction_id = transaction_id or ('auto-' + uuid.uuid4().hex)
        if (not isinstance(transaction_id, str) or not 1 <= len(transaction_id) <= 200
                or any(ord(char) < 33 or ord(char) > 126 for char in transaction_id)):
            raise MatrixError('invalid_matrix_transaction_id')
        content_size = len(json.dumps(
            content, ensure_ascii=True, allow_nan=False).encode('utf-8'))
        if content_size > MAX_EVENT_CONTENT_BYTES:
            raise MatrixError('matrix_message_too_large')
        path = ('/rooms/' + quote(room_id, safe='') + '/send/m.room.message/'
                + quote(transaction_id, safe=''))
        event_id = self.request_json('PUT', path, payload=content, idempotent=True).get('event_id')
        if not isinstance(event_id, str) or not event_id.startswith('$'):
            raise MatrixError('matrix_send_unconfirmed')
        logger.info('Matrix-Nachricht bestätigt.')
        return event_id

    def send_message(self, msg, room_id=None, thread_reply_to=None, html_msg=None,
                     transaction_id=None):
        content, _content_size = matrix_message_content(msg, html_msg, thread_reply_to)
        return self._send_content(content, room_id, transaction_id)

    def send_file(self, content_uri, filename, size, room_id=None, transaction_id=None):
        content_type = matrix_file_mimetype(filename)
        if (MATRIX_CONTENT_URI.fullmatch(content_uri or '') is None
                or content_type is None
                or type(size) is not int or not 0 < size <= MAX_MATRIX_FILE_BYTES):
            raise MatrixError('invalid_matrix_file')
        content = {
            'msgtype': 'm.file', 'body': filename, 'filename': filename,
            'url': content_uri,
            'info': {'mimetype': content_type, 'size': size},
        }
        return self._send_content(content, room_id, transaction_id)

    def read_event(self, room_id, event_id):
        if not isinstance(room_id, str) or not room_id.startswith('!'):
            raise MatrixError('invalid_matrix_room')
        if not isinstance(event_id, str) or not event_id.startswith('$'):
            raise MatrixError('invalid_matrix_event')
        return self.request_json('GET', '/rooms/' + quote(room_id, safe='') + '/event/'
                                 + quote(event_id, safe=''))

    def _request_array(self, path):
        token = self.access_token
        for refreshed in (False, True):
            try:
                with self._session() as session:
                    response = session.get(self.homeserver + '/_matrix/client/v3' + path,
                                           headers={'Authorization': 'Bearer ' + self.access_token},
                                           timeout=(5, 30), allow_redirects=False)
            except requests.RequestException:
                raise MatrixError('matrix_network_error') from None
            if response.status_code == 401 and not refreshed:
                self.password_login(expected_old_token=token)
                continue
            if response.status_code != 200:
                raise MatrixError('matrix_http_error', status=response.status_code)
            try:
                value = response.json()
            except (ValueError, TypeError, RecursionError):
                raise MatrixError('matrix_invalid_json') from None
            if not isinstance(value, list) or len(value) > 20_000:
                raise MatrixError('matrix_invalid_state')
            return value
        raise MatrixError('matrix_auth_failed')

    def get_room_state(self, room_id):
        return self._request_array('/rooms/' + quote(room_id, safe='') + '/state')

    def join_room(self, room_id):
        if not isinstance(room_id, str) or not room_id.startswith('!'):
            raise MatrixError('invalid_matrix_room')
        body = self.request_json('POST', '/join/' + quote(room_id, safe=''), payload={},
                                 idempotent=True)
        joined = body.get('room_id') if isinstance(body, dict) else None
        if joined != room_id:
            raise MatrixError('matrix_join_unconfirmed')
        return joined

    def reject_invitation(self, room_id):
        """Reject a pending room invitation without joining the room."""
        if not isinstance(room_id, str) or not room_id.startswith('!'):
            raise MatrixError('invalid_matrix_room')
        # ``leave`` has no transaction identifier.  Do not blindly retry an
        # ambiguous POST; the next Matrix sync reconciles whether the invite is
        # still pending.
        self.request_json(
            'POST', '/rooms/' + quote(room_id, safe='') + '/leave',
            payload={}, idempotent=False)
        logger.info('Verschlüsselte Matrix-Einladung abgelehnt.')
        return True

    def joined_room_ids(self):
        body = self.request_json('GET', '/joined_rooms')
        rooms = body.get('joined_rooms') if isinstance(body, dict) else None
        if (not isinstance(rooms, list) or len(rooms) > 20_000
                or any(not isinstance(room_id, str) or not room_id.startswith('!')
                       for room_id in rooms)
                or len(set(rooms)) != len(rooms)):
            raise MatrixError('matrix_joined_rooms_invalid')
        return rooms

    def invite_user(self, room_id, user_id):
        if (not isinstance(room_id, str) or not room_id.startswith('!')
                or not isinstance(user_id, str) or not user_id.startswith('@')
                or user_id == self.user_id):
            raise MatrixError('invalid_matrix_invite')
        self.request_json(
            'POST', '/rooms/' + quote(room_id, safe='') + '/invite',
            payload={'user_id': user_id}, idempotent=False)
        return True

    def create_digest_fallback_room(self, user_id, target_sha256):
        """Create one hardened, unencrypted private room for Digest commands.

        ``createRoom`` is intentionally non-idempotent. Callers must reconcile
        existing marker-bearing rooms before invoking this method and must not
        blindly repeat it after an ambiguous network result.
        """
        if (not isinstance(user_id, str) or not user_id.startswith('@')
                or user_id == self.user_id
                or not isinstance(target_sha256, str)
                or re.fullmatch(r'[0-9a-f]{64}', target_sha256) is None):
            raise MatrixError('invalid_digest_fallback_target')
        payload = {
            'visibility': 'private',
            'preset': 'private_chat',
            'is_direct': True,
            'invite': [user_id],
            'creation_content': {
                DIGEST_FALLBACK_MARKER: {
                    'version': 1,
                    'target_sha256': target_sha256,
                },
            },
            'initial_state': [
                {'type': 'm.room.join_rules', 'state_key': '',
                 'content': {'join_rule': 'invite'}},
                {'type': 'm.room.guest_access', 'state_key': '',
                 'content': {'guest_access': 'forbidden'}},
                {'type': 'm.room.history_visibility', 'state_key': '',
                 'content': {'history_visibility': 'shared'}},
            ],
            'power_level_content_override': {
                'users_default': 0,
                'events_default': 0,
                'state_default': 100,
                'invite': 100,
                'kick': 100,
                'ban': 100,
                'redact': 100,
                'events': {
                    'm.room.power_levels': 100,
                    'm.room.encryption': 100,
                    'm.room.join_rules': 100,
                    'm.room.guest_access': 100,
                    'm.room.history_visibility': 100,
                    'm.room.name': 100,
                    'm.room.topic': 100,
                    'm.room.third_party_invite': 100,
                    # Room v12+ requires tombstones above state_default.
                    'm.room.tombstone': 150,
                },
            },
        }
        body = self.request_json('POST', '/createRoom', payload=payload,
                                 idempotent=False)
        room_id = body.get('room_id') if isinstance(body, dict) else None
        if not isinstance(room_id, str) or not room_id.startswith('!'):
            raise MatrixError('matrix_create_room_unconfirmed')
        return room_id

    def direct_mapping(self):
        if not isinstance(self.user_id, str):
            raise MatrixError('matrix_identity_missing')
        return self.request_json('GET', '/user/' + quote(self.user_id, safe='')
                                 + '/account_data/m.direct', expected=(200, 404))

    def sync(self, *, since=None, room_id=None, timeout_ms=30_000):
        if since is not None and (not isinstance(since, str) or not since):
            raise MatrixError('invalid_sync_cursor')
        if not isinstance(timeout_ms, int) or not 0 <= timeout_ms <= 60_000:
            raise MatrixError('invalid_sync_timeout')
        room_filter = {'timeline': {'limit': 50}, 'state': {'lazy_load_members': False},
                       'ephemeral': {'types': []}, 'account_data': {'types': []}}
        if room_id is not None:
            room_filter['rooms'] = [room_id]
        params = {'timeout': timeout_ms, 'filter': json.dumps({
            'room': room_filter, 'presence': {'types': []}}, separators=(',', ':'))}
        if since is not None:
            params['since'] = since
        return self.request_json('GET', '/sync', params=params,
                                 timeout=(5, max(30, timeout_ms / 1000 + 10)))

    def room_messages(self, room_id, *, from_token, to_token, limit=50):
        """Read one forward page between two Matrix timeline tokens."""
        if (not isinstance(room_id, str) or not room_id.startswith('!')
                or not isinstance(from_token, str) or not 0 < len(from_token) <= 8192
                or not isinstance(to_token, str) or not 0 < len(to_token) <= 8192
                or type(limit) is not int or not 1 <= limit <= 100):
            raise MatrixError('invalid_matrix_messages_request')
        path = '/rooms/' + quote(room_id, safe='') + '/messages'
        body = self.request_json(
            'GET', path,
            params={'dir': 'f', 'from': from_token, 'to': to_token, 'limit': limit})
        chunk = body.get('chunk') if isinstance(body, dict) else None
        start = body.get('start') if isinstance(body, dict) else None
        if (not isinstance(chunk, list) or len(chunk) > limit or start != from_token
                or any(not isinstance(event, dict)
                       or event.get('room_id') != room_id for event in chunk)):
            raise MatrixError('invalid_matrix_messages_response')
        if 'end' in body:
            end = body['end']
            if (not isinstance(end, str) or not end or len(end) > 8192
                    or end == from_token):
                raise MatrixError('invalid_matrix_messages_response')
        return body

    def logout(self):
        try:
            self.request_json('POST', '/logout', payload={}, idempotent=False)
            return True
        except MatrixError:
            return False
