"""Protected persistent state for digest subscriptions and deliveries."""

import copy
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
import uuid


MAX_STATE_BYTES = 4_000_000
MAX_SUBSCRIPTIONS = 500
MAX_DIGESTS = 16
MAX_COMPLETED_COMMANDS = 2048
MAX_DELIVERY_FAILURES = 5


class DigestStateError(RuntimeError):
    pass


def _matrix_user(value):
    return isinstance(value, str) and re.fullmatch(r'@[^\s:]+:[^\s]+', value) is not None


def _matrix_room(value):
    return isinstance(value, str) and value.startswith('!') and len(value) <= 512


def _matrix_event(value):
    return isinstance(value, str) and value.startswith('$') and len(value) <= 1024


def _matrix_media(value):
    return (isinstance(value, str)
            and re.fullmatch(r'mxc://[A-Za-z0-9._:-]+/[A-Za-z0-9_-]+', value) is not None)


class DigestState:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'state.json'
        self.lock_path = self.directory / 'listener.lock'
        self.content_directory = self.directory / 'content'
        self._mutex = threading.RLock()
        self._process_lock = None
        self._default = {'version': 2, 'since': None, 'subscriptions': {},
                         'completed_commands': [], 'digests': {}}
        self._prepare_directory(self.directory)
        self._prepare_directory(self.content_directory)
        with self._mutex:
            try:
                os.lstat(self.path)
                data = self._read()
                if data['version'] == 1:
                    data['version'] = 2
                    for digest in data['digests'].values():
                        digest.update(ris_mxc=None, ris_uploaded=False)
                    self._write(data)
            except FileNotFoundError:
                self._write(copy.deepcopy(self._default))

    @staticmethod
    def _prepare_directory(directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = os.lstat(directory)
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()):
            raise DigestStateError('unsafe_digest_state_directory')

    def acquire_process_lock(self):
        if self._process_lock is not None:
            return
        fd = None
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_uid != os.geteuid()):
                raise DigestStateError('unsafe_digest_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            try:
                if fd is not None:
                    os.close(fd)
            except Exception:
                pass
            raise DigestStateError('digest_listener_already_running') from None
        self._process_lock = fd

    @staticmethod
    def _validate(data):
        if (not isinstance(data, dict)
                or set(data) != {'version', 'since', 'subscriptions',
                                 'completed_commands', 'digests'}
                or data.get('version') not in (1, 2)
                or data.get('since') is not None and not isinstance(data.get('since'), str)
                or not isinstance(data.get('subscriptions'), dict)
                or len(data['subscriptions']) > MAX_SUBSCRIPTIONS
                or not isinstance(data.get('completed_commands'), list)
                or len(data['completed_commands']) > MAX_COMPLETED_COMMANDS
                or any(not _matrix_event(value) for value in data['completed_commands'])
                or len(set(data['completed_commands'])) != len(data['completed_commands'])
                or not isinstance(data.get('digests'), dict)
                or len(data['digests']) > MAX_DIGESTS):
            raise DigestStateError('invalid_digest_state')
        for user_id, subscription in data['subscriptions'].items():
            if (not _matrix_user(user_id) or not isinstance(subscription, dict)
                    or set(subscription) != {'room_id', 'event_id'}
                    or not _matrix_room(subscription.get('room_id'))
                    or not _matrix_event(subscription.get('event_id'))):
                raise DigestStateError('invalid_digest_subscription')
        for date, digest in data['digests'].items():
            expected = ({'sha256', 'content_file', 'status', 'recipients'}
                        if data['version'] == 1 else
                        {'sha256', 'content_file', 'status', 'recipients',
                         'ris_mxc', 'ris_uploaded'})
            if (not isinstance(date, str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}', date) is None
                    or not isinstance(digest, dict)
                    or set(digest) != expected
                    or not isinstance(digest.get('sha256'), str)
                    or re.fullmatch(r'[0-9a-f]{64}', digest['sha256']) is None
                    or not isinstance(digest.get('content_file'), str)
                    or '/' in digest['content_file'] or '\\' in digest['content_file']
                    or digest.get('status') not in ('pending', 'complete')
                    or not isinstance(digest.get('recipients'), dict)
                    or len(digest['recipients']) > MAX_SUBSCRIPTIONS):
                raise DigestStateError('invalid_digest_record')
            if (data['version'] == 2
                    and (digest['ris_mxc'] is not None and not _matrix_media(digest['ris_mxc'])
                         or type(digest['ris_uploaded']) is not bool
                         or digest['ris_uploaded'] and digest['ris_mxc'] is None)):
                raise DigestStateError('invalid_digest_record')
            for user_id, recipient in digest['recipients'].items():
                if (not _matrix_user(user_id) or not isinstance(recipient, dict)
                        or set(recipient) != {'room_id', 'parts', 'failures', 'status'}
                        or not _matrix_room(recipient.get('room_id'))
                        or not isinstance(recipient.get('parts'), list)
                        or not 1 <= len(recipient['parts']) <= 100
                        or any(value is not None and not _matrix_event(value)
                               for value in recipient['parts'])
                        or type(recipient.get('failures')) is not int
                        or not 0 <= recipient['failures'] <= MAX_DELIVERY_FAILURES
                        or recipient.get('status') not in
                           ('pending', 'delivered', 'failed', 'room_invalid')):
                    raise DigestStateError('invalid_digest_recipient')
        return data

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as handle:
                metadata = os.fstat(handle.fileno())
                if (not stat.S_ISREG(metadata.st_mode)
                        or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_uid != os.geteuid()
                        or metadata.st_size > MAX_STATE_BYTES):
                    raise DigestStateError('unsafe_digest_state_file')
                raw = handle.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise DigestStateError('digest_state_too_large')
            return self._validate(json.loads(raw.decode('utf-8')))
        except DigestStateError:
            raise
        except (OSError, UnicodeError, ValueError, RecursionError):
            raise DigestStateError('digest_state_unreadable') from None

    def _write(self, data):
        self._validate(data)
        raw = (json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw) > MAX_STATE_BYTES:
            raise DigestStateError('digest_state_too_large')
        temporary = self.directory / ('.state.' + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def snapshot(self):
        with self._mutex:
            return copy.deepcopy(self._read())

    def _change(self, callback):
        with self._mutex:
            data = self._read()
            result = callback(data)
            self._write(data)
            return result, copy.deepcopy(data)

    def bootstrap(self, cursor):
        if not isinstance(cursor, str) or not cursor:
            raise DigestStateError('invalid_digest_sync_cursor')
        def update(data):
            if data['since'] is not None:
                raise DigestStateError('digest_already_bootstrapped')
            data['since'] = cursor
        self._change(update)

    def advance_cursor(self, cursor):
        if not isinstance(cursor, str) or not cursor:
            raise DigestStateError('invalid_digest_sync_cursor')
        self._change(lambda data: data.__setitem__('since', cursor))

    def command_completed(self, event_id):
        return event_id in self.snapshot()['completed_commands']

    def apply_command(self, event_id, command, user_id, room_id):
        if (not _matrix_event(event_id) or command not in ('Digest', 'Digest aus')
                or not _matrix_user(user_id) or not _matrix_room(room_id)):
            raise DigestStateError('invalid_digest_command')
        def update(data):
            if event_id in data['completed_commands']:
                return False
            if command == 'Digest':
                data['subscriptions'][user_id] = {'room_id': room_id, 'event_id': event_id}
            else:
                data['subscriptions'].pop(user_id, None)
            data['completed_commands'] = (
                data['completed_commands'] + [event_id])[-MAX_COMPLETED_COMMANDS:]
            return True
        return self._change(update)[0]

    def ignore_command(self, event_id):
        if not _matrix_event(event_id):
            raise DigestStateError('invalid_digest_command')
        def update(data):
            if event_id not in data['completed_commands']:
                data['completed_commands'] = (
                    data['completed_commands'] + [event_id])[-MAX_COMPLETED_COMMANDS:]
        self._change(update)

    def remove_subscription(self, user_id, room_id):
        def update(data):
            current = data['subscriptions'].get(user_id)
            if current is not None and current['room_id'] == room_id:
                del data['subscriptions'][user_id]
        self._change(update)

    def stage_digest(self, date, sha256, content_file, part_count):
        if (not isinstance(part_count, int) or not 1 <= part_count <= 100
                or not isinstance(content_file, str)):
            raise DigestStateError('invalid_digest_stage')
        def update(data):
            existing = data['digests'].get(date)
            if existing is not None:
                if existing['sha256'] != sha256:
                    raise DigestStateError('digest_date_hash_conflict')
                return False
            recipients = {
                user_id: {'room_id': value['room_id'], 'parts': [None] * part_count,
                          'failures': 0, 'status': 'pending'}
                for user_id, value in data['subscriptions'].items()
            }
            data['digests'][date] = {'sha256': sha256, 'content_file': content_file,
                                     'status': 'pending', 'recipients': recipients,
                                     'ris_mxc': None, 'ris_uploaded': False}
            while len(data['digests']) > MAX_DIGESTS:
                oldest = sorted(data['digests'])[0]
                if data['digests'][oldest]['status'] != 'complete':
                    raise DigestStateError('too_many_pending_digests')
                del data['digests'][oldest]
            return True
        return self._change(update)[0]

    def record_digest_media(self, date, mxc_uri):
        if not _matrix_media(mxc_uri):
            raise DigestStateError('invalid_digest_media')
        def update(data):
            digest = data['digests'][date]
            current = digest['ris_mxc']
            if current is not None and current != mxc_uri:
                raise DigestStateError('digest_media_changed')
            digest['ris_mxc'] = mxc_uri
            return mxc_uri
        return self._change(update)[0]

    def mark_digest_media_uploaded(self, date, mxc_uri):
        if not _matrix_media(mxc_uri):
            raise DigestStateError('invalid_digest_media')
        def update(data):
            digest = data['digests'][date]
            if digest['ris_mxc'] != mxc_uri:
                raise DigestStateError('digest_media_changed')
            digest['ris_uploaded'] = True
        self._change(update)

    def record_part(self, date, user_id, index, event_id):
        if not _matrix_event(event_id):
            raise DigestStateError('invalid_digest_delivery_event')
        def update(data):
            recipient = data['digests'][date]['recipients'][user_id]
            if recipient['status'] != 'pending' or not 0 <= index < len(recipient['parts']):
                raise DigestStateError('invalid_digest_delivery_part')
            current = recipient['parts'][index]
            if current is not None and current != event_id:
                raise DigestStateError('digest_delivery_event_changed')
            recipient['parts'][index] = event_id
        self._change(update)

    def record_failure(self, date, user_id):
        def update(data):
            recipient = data['digests'][date]['recipients'][user_id]
            if recipient['status'] != 'pending':
                return recipient['failures']
            recipient['failures'] = min(MAX_DELIVERY_FAILURES, recipient['failures'] + 1)
            if recipient['failures'] >= MAX_DELIVERY_FAILURES:
                recipient['status'] = 'failed'
            statuses = {value['status'] for value in data['digests'][date]['recipients'].values()}
            if 'pending' not in statuses:
                data['digests'][date]['status'] = 'complete'
            return recipient['failures']
        return self._change(update)[0]

    def finish_recipient(self, date, user_id, status):
        if status not in ('delivered', 'room_invalid'):
            raise DigestStateError('invalid_digest_recipient_status')
        def update(data):
            recipient = data['digests'][date]['recipients'][user_id]
            if recipient['status'] == 'pending':
                if status == 'delivered' and any(value is None for value in recipient['parts']):
                    raise DigestStateError('incomplete_digest_delivery')
                recipient['status'] = status
            statuses = {value['status'] for value in data['digests'][date]['recipients'].values()}
            if not statuses or 'pending' not in statuses:
                data['digests'][date]['status'] = 'complete'
        self._change(update)

    def finish_empty_digests(self):
        def update(data):
            for digest in data['digests'].values():
                if digest['status'] == 'pending' and not digest['recipients']:
                    digest['status'] = 'complete'
        self._change(update)
