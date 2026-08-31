"""Opt-in Matrix subscriptions and idempotent weekly digest delivery."""

from datetime import date as Date
import hashlib
import logging
import os
from pathlib import Path
import re
import stat
import threading
import time
import unicodedata
import uuid

from digest_state import DigestStateError, MAX_DELIVERY_FAILURES
from matrixbot import MAX_EVENT_CONTENT_BYTES, MatrixError, matrix_message_content


logger = logging.getLogger(__name__)
COMMANDS = {'Digest', 'Digest aus'}
MAX_DIGEST_BYTES = 1_000_000
DIGEST_NAME = re.compile(r'(\d{4}-\d{2}-\d{2})-methoden-digest\.md')


class DigestServiceError(RuntimeError):
    pass


def digest_command_from_event(event):
    if not isinstance(event, dict) or event.get('type') != 'm.room.message':
        return None
    event_id, sender, content = event.get('event_id'), event.get('sender'), event.get('content')
    if (not isinstance(event_id, str) or not event_id.startswith('$')
            or not isinstance(sender, str) or not sender.startswith('@')
            or not isinstance(content, dict)
            or set(content) - {'msgtype', 'body', 'm.mentions'}
            or content.get('msgtype') != 'm.text'
            or content.get('m.mentions') not in (None, {})):
        return None
    body = content.get('body')
    if not isinstance(body, str):
        return None
    command = unicodedata.normalize('NFC', body).strip()
    return (event_id, sender, command) if command in COMMANDS else None


def validate_digest_room(bot, room_id, user_id):
    if (not isinstance(room_id, str) or not room_id.startswith('!')
            or not isinstance(user_id, str) or not user_id.startswith('@')
            or user_id == bot.user_id):
        raise DigestServiceError('invalid_digest_room_identity')
    state = bot.get_room_state(room_id)
    joined, singleton = set(), {}
    for event in state:
        if not isinstance(event, dict) or not isinstance(event.get('content'), dict):
            raise DigestServiceError('invalid_digest_room_state')
        if event.get('type') == 'm.room.encryption':
            raise DigestServiceError('encrypted_digest_room_unsupported')
        if event.get('type') == 'm.room.member':
            if event['content'].get('membership') == 'join':
                joined.add(event.get('state_key'))
        elif event.get('state_key') == '':
            singleton[event.get('type')] = event['content']
    if joined != {bot.user_id, user_id}:
        raise DigestServiceError('digest_room_not_private')
    if (singleton.get('m.room.join_rules', {}).get('join_rule') != 'invite'
            or singleton.get('m.room.guest_access', {}).get('guest_access', 'forbidden') != 'forbidden'
            or singleton.get('m.room.history_visibility', {}).get('history_visibility')
               == 'world_readable'):
        raise DigestServiceError('unsafe_digest_room_access')
    return True


def split_markdown(text):
    if not isinstance(text, str) or not text:
        raise DigestServiceError('empty_digest')
    parts, remaining = [], text
    while remaining:
        if matrix_message_content(remaining)[1] <= MAX_EVENT_CONTENT_BYTES:
            parts.append(remaining)
            break
        low, high, best = 1, len(remaining), 0
        while low <= high:
            middle = (low + high) // 2
            if matrix_message_content(remaining[:middle])[1] <= MAX_EVENT_CONTENT_BYTES:
                best, low = middle, middle + 1
            else:
                high = middle - 1
        if best == 0:
            raise DigestServiceError('digest_character_too_large')
        boundary = remaining.rfind('\n', 0, best + 1)
        if boundary > 0:
            best = boundary + 1
        parts.append(remaining[:best])
        remaining = remaining[best:]
    if ''.join(parts) != text or len(parts) > 100:
        raise DigestServiceError('digest_split_failed')
    return parts


def _read_digest(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            metadata = os.fstat(handle.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or not 0 < metadata.st_size <= MAX_DIGEST_BYTES):
                raise DigestServiceError('unsafe_digest_file')
            raw = handle.read(MAX_DIGEST_BYTES + 1)
        if len(raw) > MAX_DIGEST_BYTES:
            raise DigestServiceError('digest_too_large')
        text = raw.decode('utf-8')
    except DigestServiceError:
        raise
    except (OSError, UnicodeError):
        raise DigestServiceError('digest_file_unreadable') from None
    if not text.strip():
        raise DigestServiceError('empty_digest')
    return raw, text


def _atomic_private_write(directory, name, raw):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = os.lstat(directory)
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()):
        raise DigestServiceError('unsafe_digest_content_directory')
    target = directory / name
    if target.exists():
        existing, _text = _read_digest(target)
        if existing != raw:
            raise DigestServiceError('digest_content_conflict')
        return target
    temporary = directory / ('.' + name + '.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def _transaction(*values):
    material = '\0'.join(values).encode('utf-8')
    return hashlib.sha256(material).hexdigest()


class DigestService:
    def __init__(self, bot, config, state, *, sleep=time.sleep):
        self.bot, self.config, self.state = bot, config, state
        self.sleep = sleep
        self.stop_event = threading.Event()
        self.inbox = Path(config.digest_inbox_dir)

    def bootstrap(self):
        if self.state.snapshot()['since'] is not None:
            return False
        response = self.bot.sync(since=None, timeout_ms=0)
        cursor = response.get('next_batch') if isinstance(response, dict) else None
        if not isinstance(cursor, str) or not cursor:
            raise MatrixError('invalid_sync_response')
        self.state.bootstrap(cursor)
        logger.info('Digest-DM-Empfang initialisiert; vorhandene Historie wurde nicht ausgeführt.')
        return True

    def _invite_sender(self, room):
        invite_state = room.get('invite_state', {}) if isinstance(room, dict) else {}
        events = invite_state.get('events', []) if isinstance(invite_state, dict) else []
        if not isinstance(events, list) or len(events) > 100:
            return None
        encrypted = any(isinstance(event, dict) and event.get('type') == 'm.room.encryption'
                        for event in events)
        if encrypted:
            return None
        candidates = [event.get('sender') for event in events
                      if isinstance(event, dict) and event.get('type') == 'm.room.member'
                      and event.get('state_key') == self.bot.user_id
                      and isinstance(event.get('content'), dict)
                      and event['content'].get('membership') == 'invite']
        candidates = [value for value in candidates
                      if isinstance(value, str) and value.startswith('@')]
        if not candidates:
            return None
        sender = candidates[-1]
        joined = {event.get('state_key') for event in events
                  if isinstance(event, dict) and event.get('type') == 'm.room.member'
                  and isinstance(event.get('content'), dict)
                  and event['content'].get('membership') == 'join'}
        return sender if joined in (set(), {sender}) else None

    def _join_invitations(self, response):
        rooms = response.get('rooms', {}) if isinstance(response, dict) else {}
        invited = rooms.get('invite', {}) if isinstance(rooms, dict) else {}
        if not isinstance(invited, dict) or len(invited) > 50:
            raise MatrixError('invalid_sync_response')
        for room_id, room in invited.items():
            sender = self._invite_sender(room)
            if sender is None:
                logger.warning('Digest-Einladung ignoriert: verschlüsselt oder nicht prüfbar.')
                continue
            try:
                self.bot.join_room(room_id)
                logger.info('Privater Digest-Raumeinladung beigetreten; warte auf Befehl.')
            except MatrixError as exc:
                logger.warning('Digest-Raumeinladung konnte nicht angenommen werden: %s', str(exc))

    def _commands(self, response):
        rooms = response.get('rooms', {}) if isinstance(response, dict) else {}
        joined = rooms.get('join', {}) if isinstance(rooms, dict) else {}
        if not isinstance(joined, dict) or len(joined) > 20_000:
            raise MatrixError('invalid_sync_response')
        commands = []
        for room_id, room in joined.items():
            timeline = room.get('timeline', {}) if isinstance(room, dict) else {}
            events = timeline.get('events', []) if isinstance(timeline, dict) else []
            if not isinstance(events, list) or len(events) > 50:
                raise MatrixError('invalid_sync_response')
            for event in events:
                command = digest_command_from_event(event)
                if command is not None and command[1] != self.bot.user_id:
                    commands.append((room_id, *command))
        return commands

    def _verify_text(self, room_id, event_id, text):
        event = self.bot.read_event(room_id, event_id)
        content = event.get('content', {}) if isinstance(event, dict) else {}
        if (event.get('event_id', event_id) != event_id
                or event.get('type') != 'm.room.message'
                or event.get('sender') != self.bot.user_id
                or content.get('body') != text):
            raise MatrixError('matrix_readback_mismatch')

    def _process_command(self, room_id, event_id, user_id, command):
        if self.state.command_completed(event_id):
            return
        try:
            validate_digest_room(self.bot, room_id, user_id)
        except DigestServiceError as exc:
            logger.warning('Digest-Befehl außerhalb eines privaten Zweierraums ignoriert: %s',
                           str(exc))
            self.state.ignore_command(event_id)
            return
        if command == 'Digest aus':
            message = ('Digest deaktiviert. Du erhältst in diesem Raum keine weiteren '
                       'wöchentlichen Methoden-Journal-Digests.')
        else:
            message = ('Digest aktiviert. Du erhältst künftig den wöchentlichen '
                       'Methoden-Journal-Digest in diesem privaten Raum. Mit „Digest aus“ '
                       'kannst du ihn wieder abbestellen.')
        transaction = 'digest-command-' + _transaction(event_id)[:40]
        sent = self.bot.send_message(message, room_id=room_id, transaction_id=transaction)
        self._verify_text(room_id, sent, message)
        self.state.apply_command(event_id, command, user_id, room_id)

    def poll_once(self, *, timeout_ms=30_000):
        snapshot = self.state.snapshot()
        if snapshot['since'] is None:
            return self.bootstrap()
        response = self.bot.sync(since=snapshot['since'], timeout_ms=timeout_ms)
        cursor = response.get('next_batch') if isinstance(response, dict) else None
        if not isinstance(cursor, str) or not cursor:
            raise MatrixError('invalid_sync_response')
        self._join_invitations(response)
        commands = self._commands(response)
        for room_id, event_id, user_id, command in commands:
            self._process_command(room_id, event_id, user_id, command)
        self.state.advance_cursor(cursor)
        return bool(commands)

    def _stage_inbox(self):
        try:
            metadata = os.lstat(self.inbox)
        except FileNotFoundError:
            self.inbox.mkdir(mode=0o700, parents=True)
            metadata = os.lstat(self.inbox)
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()):
            raise DigestServiceError('unsafe_digest_inbox')
        for path in sorted(self.inbox.iterdir()):
            match = DIGEST_NAME.fullmatch(path.name)
            if match is None:
                continue
            try:
                Date.fromisoformat(match.group(1))
            except ValueError:
                raise DigestServiceError('invalid_digest_date') from None
            raw, text = _read_digest(path)
            digest_hash = hashlib.sha256(raw).hexdigest()
            content_name = match.group(1) + '-' + digest_hash + '.md'
            target = _atomic_private_write(self.state.content_directory, content_name, raw)
            parts = split_markdown(text)
            self.state.stage_digest(match.group(1), digest_hash, target.name, len(parts))
            # The protected content copy and delivery plan are durable. Remove
            # the one-shot inbox entry so old weeks cannot be restaged after
            # bounded state-history pruning.
            check_raw, _check_text = _read_digest(path)
            if hashlib.sha256(check_raw).hexdigest() != digest_hash:
                raise DigestServiceError('digest_inbox_changed_during_stage')
            path.unlink()
            inbox_fd = os.open(self.inbox, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(inbox_fd)
            finally:
                os.close(inbox_fd)
        self.state.finish_empty_digests()

    def _content_parts(self, digest):
        path = self.state.content_directory / digest['content_file']
        raw, text = _read_digest(path)
        if hashlib.sha256(raw).hexdigest() != digest['sha256']:
            raise DigestServiceError('digest_content_hash_mismatch')
        return split_markdown(text)

    def _deliver(self):
        snapshot = self.state.snapshot()
        for date in sorted(snapshot['digests']):
            digest = snapshot['digests'][date]
            if digest['status'] != 'pending':
                continue
            parts = self._content_parts(digest)
            for user_id, recipient in digest['recipients'].items():
                if recipient['status'] != 'pending':
                    continue
                try:
                    validate_digest_room(self.bot, recipient['room_id'], user_id)
                except DigestServiceError as exc:
                    logger.warning('Digest-Abo wegen ungültigem Privatraum entfernt: %s', str(exc))
                    self.state.remove_subscription(user_id, recipient['room_id'])
                    self.state.finish_recipient(date, user_id, 'room_invalid')
                    continue
                try:
                    for index, text in enumerate(parts):
                        event_id = recipient['parts'][index]
                        transaction = ('digest-' + date.replace('-', '') + '-'
                                       + digest['sha256'][:12] + '-'
                                       + _transaction(user_id)[:12] + '-' + str(index + 1))
                        if event_id is None:
                            event_id = self.bot.send_message(
                                text, room_id=recipient['room_id'], transaction_id=transaction)
                        self._verify_text(recipient['room_id'], event_id, text)
                        self.state.record_part(date, user_id, index, event_id)
                        recipient['parts'][index] = event_id
                    self.state.finish_recipient(date, user_id, 'delivered')
                except MatrixError as exc:
                    failures = self.state.record_failure(date, user_id)
                    logger.warning('Digest-Zustellung nicht bestätigt (%d/%d): %s',
                                   failures, MAX_DELIVERY_FAILURES, str(exc))

    def process_inbox(self):
        self._stage_inbox()
        self._deliver()

    def run_forever(self):
        self.state.acquire_process_lock()
        delay = 2
        while not self.stop_event.is_set():
            try:
                self.process_inbox()
                self.poll_once(timeout_ms=30_000)
                delay = 2
            except (DigestServiceError, DigestStateError, MatrixError) as exc:
                logger.error('Digest-Dienst pausiert: %s', str(exc))
                self.stop_event.wait(delay)
                delay = min(delay * 2, 60)
            except Exception as exc:
                logger.error('Digest-Dienst unerwartet pausiert: %s', type(exc).__name__)
                self.stop_event.wait(delay)
                delay = min(delay * 2, 60)
