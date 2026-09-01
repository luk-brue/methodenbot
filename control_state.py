"""Protected, atomic state for Matrix control commands."""

import copy
import fcntl
import json
import os
from pathlib import Path
import stat
import threading
import uuid


MAX_STATE_BYTES = 2_000_000
COMMANDS = {'Test', 'Test 2', 'KI an', 'KI aus'}
MAX_DELIVERY_FAILURES = 5


class ControlStateError(RuntimeError):
    pass


class ControlState:
    def __init__(self, directory, *, ai_default=False):
        self.directory = Path(directory)
        self.path = self.directory / 'state.json'
        self.lock_path = self.directory / 'listener.lock'
        self._mutex = threading.RLock()
        self._process_lock = None
        self._default = {'version': 1, 'since': None, 'ai_enabled': bool(ai_default),
                         'queue': [], 'completed': []}
        self._prepare_directory()
        with self._mutex:
            try:
                os.lstat(self.path)
                self._read()
            except FileNotFoundError:
                self._write(copy.deepcopy(self._default))

    def _prepare_directory(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = os.lstat(self.directory)
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()):
            raise ControlStateError('unsafe_control_state_directory')

    def acquire_process_lock(self):
        if self._process_lock is not None:
            return
        fd = None
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_uid != os.geteuid()):
                raise ControlStateError('unsafe_control_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            try:
                if fd is not None:
                    os.close(fd)
            except Exception:
                pass
            raise ControlStateError('control_listener_already_running') from None
        self._process_lock = fd

    @staticmethod
    def _validate(data):
        if (not isinstance(data, dict) or set(data) != {'version', 'since', 'ai_enabled', 'queue', 'completed'}
                or data.get('version') != 1 or type(data.get('ai_enabled')) is not bool
                or (data.get('since') is not None and not isinstance(data.get('since'), str))
                or not isinstance(data.get('queue'), list) or len(data['queue']) > 100
                or not isinstance(data.get('completed'), list) or len(data['completed']) > 512
                or any(not isinstance(value, str) or not value.startswith('$') for value in data['completed'])):
            raise ControlStateError('invalid_control_state')
        seen = set(data['completed'])
        for job in data['queue']:
            if (not isinstance(job, dict) or job.get('event_id') in seen
                    or not isinstance(job.get('event_id'), str) or not job['event_id'].startswith('$')
                    or job.get('command') not in COMMANDS or job.get('status') not in ('queued', 'planned')
                    or set(job) - {'event_id', 'command', 'status', 'ai_enabled', 'parts',
                                   'delivery_failures', 'reporting_failure'}):
                raise ControlStateError('invalid_control_job')
            seen.add(job['event_id'])
            if job['status'] == 'planned':
                if (type(job.get('ai_enabled')) is not bool or not isinstance(job.get('parts'), list)
                        or type(job.get('delivery_failures', 0)) is not int
                        or not 0 <= job.get('delivery_failures', 0) <= MAX_DELIVERY_FAILURES
                        or type(job.get('reporting_failure', False)) is not bool):
                    raise ControlStateError('invalid_control_plan')
        return data

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as handle:
                metadata = os.fstat(handle.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_uid != os.geteuid() or metadata.st_size > MAX_STATE_BYTES):
                    raise ControlStateError('unsafe_control_state_file')
                raw = handle.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ControlStateError('control_state_too_large')
            return self._validate(json.loads(raw.decode('utf-8')))
        except ControlStateError:
            raise
        except (OSError, UnicodeError, ValueError, RecursionError):
            raise ControlStateError('control_state_unreadable') from None

    def _write(self, data):
        self._validate(data)
        raw = (json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw) > MAX_STATE_BYTES:
            raise ControlStateError('control_state_too_large')
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
            callback(data)
            self._write(data)
            return copy.deepcopy(data)

    def bootstrap(self, cursor):
        if not isinstance(cursor, str) or not cursor:
            raise ControlStateError('invalid_sync_cursor')
        def update(data):
            if data['since'] is not None:
                raise ControlStateError('control_already_bootstrapped')
            data['since'] = cursor
        self._change(update)

    def record_sync(self, cursor, jobs):
        if not isinstance(cursor, str) or not cursor or not isinstance(jobs, list):
            raise ControlStateError('invalid_sync_batch')
        def update(data):
            known = set(data['completed']) | {job['event_id'] for job in data['queue']}
            for event_id, command in jobs:
                if (not isinstance(event_id, str) or not event_id.startswith('$') or command not in COMMANDS):
                    raise ControlStateError('invalid_control_event')
                if event_id not in known:
                    data['queue'].append({'event_id': event_id, 'command': command, 'status': 'queued'})
                    known.add(event_id)
            data['since'] = cursor
        self._change(update)

    def head(self):
        state = self.snapshot()
        return state['queue'][0] if state['queue'] else None

    def plan_head(self, event_id, ai_enabled, parts):
        def update(data):
            if not data['queue'] or data['queue'][0]['event_id'] != event_id:
                raise ControlStateError('control_queue_changed')
            job = data['queue'][0]
            job.update(status='planned', ai_enabled=bool(ai_enabled), parts=parts,
                       delivery_failures=0, reporting_failure=False)
        self._change(update)

    def record_delivery_failure(self, event_id):
        def update(data):
            if not data['queue'] or data['queue'][0]['event_id'] != event_id:
                raise ControlStateError('control_queue_changed')
            job = data['queue'][0]
            if job.get('status') != 'planned':
                raise ControlStateError('control_job_not_planned')
            failures = job.get('delivery_failures', 0)
            if failures >= MAX_DELIVERY_FAILURES:
                raise ControlStateError('delivery_failure_limit_exceeded')
            job['delivery_failures'] = failures + 1
        return self._change(update)['queue'][0]['delivery_failures']

    def replace_head_with_failure_notice(self, event_id, parts):
        def update(data):
            if not data['queue'] or data['queue'][0]['event_id'] != event_id:
                raise ControlStateError('control_queue_changed')
            job = data['queue'][0]
            if job.get('status') != 'planned' or job.get('reporting_failure', False):
                raise ControlStateError('control_failure_notice_invalid')
            job.update(parts=parts, delivery_failures=0, reporting_failure=True)
        self._change(update)

    def record_part(self, event_id, index, matrix_event_id):
        def update(data):
            if not data['queue'] or data['queue'][0]['event_id'] != event_id:
                raise ControlStateError('control_queue_changed')
            parts = data['queue'][0].get('parts')
            if not isinstance(parts, list) or not 0 <= index < len(parts):
                raise ControlStateError('invalid_control_part')
            current = parts[index].get('event_id')
            if current is not None and current != matrix_event_id:
                raise ControlStateError('matrix_event_id_changed')
            parts[index]['event_id'] = matrix_event_id
        self._change(update)

    def set_ai(self, event_id, enabled):
        def update(data):
            if not data['queue'] or data['queue'][0]['event_id'] != event_id:
                raise ControlStateError('control_queue_changed')
            data['ai_enabled'] = bool(enabled)
        return self._change(update)['ai_enabled']

    def set_ai_enabled(self, enabled):
        """Set the shared AI switch without coupling it to one room queue."""
        if type(enabled) is not bool:
            raise ControlStateError('invalid_ai_state')
        def update(data):
            data['ai_enabled'] = enabled
        return self._change(update)['ai_enabled']

    def complete_head(self, event_id):
        def update(data):
            if not data['queue'] or data['queue'][0]['event_id'] != event_id:
                raise ControlStateError('control_queue_changed')
            data['queue'].pop(0)
            data['completed'] = (data['completed'] + [event_id])[-512:]
        self._change(update)
