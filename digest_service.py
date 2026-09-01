"""Opt-in Matrix subscriptions and idempotent weekly digest delivery."""

from datetime import date as Date
import hashlib
import html
import logging
import os
from pathlib import Path
import re
import stat
import threading
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

from digest_bundle import (DigestBundleError, MAX_BUNDLE_BYTES, MAX_MARKDOWN_BYTES,
                           MAX_RIS_BYTES, unpack_bundle, validate_markdown, validate_ris)
from digest_state import DigestStateError, MAX_DELIVERY_FAILURES
from matrixbot import (DIGEST_FALLBACK_MARKER, MAX_EVENT_CONTENT_BYTES, MatrixError,
                       matrix_message_content)


logger = logging.getLogger(__name__)
COMMANDS = {'Digest', 'Digest aus'}
DIGEST_FALLBACK_NOTICE = (
    'Deine bisherige persönliche Nachricht ist Ende-zu-Ende-verschlüsselt. '
    'Dieser Methodenbot kann verschlüsselte Nachrichten nicht lesen. Bitte nutze '
    'deshalb diesen privaten, aber nicht Ende-zu-Ende-verschlüsselten Raum und sende '
    'hier exakt „Digest“ oder „Digest aus“.')
MAX_FALLBACK_ROOM_SCAN = 500
DIGEST_BUNDLE_NAME = re.compile(r'(\d{4}-\d{2}-\d{2})-methoden-digest\.bundle')
PAIRED_CONTENT_NAME = re.compile(
    r'(\d{4}-\d{2}-\d{2})-([0-9a-f]{64})-([0-9a-f]{64})\.md')
REGULAR_NEWSLETTER = re.compile(r'\A# Methoden-Journal-Digest(?:\s|–|-)')
INLINE_MARKDOWN = re.compile(
    r'`([^`\n]+)`|\[([^\]\n]+)\]\(([^)\s]+)\)|\*\*([^*\n]+)\*\*|(?<!\*)\*([^*\n]+)\*(?!\*)')
HEADING = re.compile(r'^(#{1,6})[ \t]+(.+?)\s*$')
UNORDERED_ITEM = re.compile(r'^[ \t]*[-+*][ \t]+(.+?)\s*$')
ORDERED_ITEM = re.compile(r'^[ \t]*\d+\.[ \t]+(.+?)\s*$')


class DigestServiceError(RuntimeError):
    pass


def _safe_link(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None)


def _inline_markdown(text):
    """Render the digest's inline Markdown while treating all source HTML as text."""
    output, position = [], 0
    for match in INLINE_MARKDOWN.finditer(text):
        output.append(html.escape(text[position:match.start()]))
        if match.group(1) is not None:
            output.append('<code>' + html.escape(match.group(1)) + '</code>')
        elif match.group(2) is not None:
            label, url = match.group(2), match.group(3)
            if _safe_link(url):
                output.append('<a href="' + html.escape(url, quote=True) + '">'
                              + html.escape(label) + '</a>')
            else:
                output.append(html.escape(label))
        elif match.group(4) is not None:
            output.append('<strong>' + html.escape(match.group(4)) + '</strong>')
        else:
            output.append('<em>' + html.escape(match.group(5)) + '</em>')
        position = match.end()
    output.append(html.escape(text[position:]))
    return ''.join(output)


def _paragraph(lines):
    rendered = []
    for index, line in enumerate(lines):
        hard_break = line.endswith('  ')
        rendered.append(_inline_markdown(line[:-2] if hard_break else line))
        if index + 1 < len(lines):
            rendered.append('<br>' if hard_break else ' ')
    return ''.join(rendered)


def markdown_to_matrix_html(text):
    """Render a conservative Markdown subset accepted by Matrix clients."""
    if not isinstance(text, str) or not text:
        raise DigestServiceError('empty_digest')
    lines, blocks, index = text.splitlines(), [], 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith('```'):
            index += 1
            code = []
            while index < len(lines) and not lines[index].startswith('```'):
                code.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            blocks.append('<pre><code>' + html.escape('\n'.join(code)) + '</code></pre>')
            continue
        heading = HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            blocks.append(f'<h{level}>' + _inline_markdown(heading.group(2))
                          + f'</h{level}>')
            index += 1
            continue
        if re.fullmatch(r'[ \t]*([-*_])[ \t]*\1[ \t]*\1(?:[ \t]*\1)*[ \t]*', line):
            blocks.append('<hr>')
            index += 1
            continue
        unordered = UNORDERED_ITEM.match(line)
        if unordered:
            items = []
            while index < len(lines):
                item = UNORDERED_ITEM.match(lines[index])
                if not item:
                    break
                items.append('<li>' + _inline_markdown(item.group(1)) + '</li>')
                index += 1
            blocks.append('<ul>' + ''.join(items) + '</ul>')
            continue
        ordered = ORDERED_ITEM.match(line)
        if ordered:
            items = []
            while index < len(lines):
                item = ORDERED_ITEM.match(lines[index])
                if not item:
                    break
                items.append('<li>' + _inline_markdown(item.group(1)) + '</li>')
                index += 1
            blocks.append('<ol>' + ''.join(items) + '</ol>')
            continue
        if line.startswith('> '):
            quote = []
            while index < len(lines) and lines[index].startswith('> '):
                quote.append(lines[index][2:])
                index += 1
            blocks.append('<blockquote><p>' + _paragraph(quote) + '</p></blockquote>')
            continue
        paragraph = []
        while index < len(lines):
            candidate = lines[index]
            if not candidate.strip():
                break
            if paragraph and (candidate.startswith('```') or HEADING.match(candidate)
                              or UNORDERED_ITEM.match(candidate)
                              or ORDERED_ITEM.match(candidate)
                              or candidate.startswith('> ')):
                break
            paragraph.append(candidate)
            index += 1
        blocks.append('<p>' + _paragraph(paragraph) + '</p>')
    return ''.join(blocks)


def digest_command_from_event(event):
    if not isinstance(event, dict) or event.get('type') != 'm.room.message':
        return None
    event_id, sender, content = event.get('event_id'), event.get('sender'), event.get('content')
    if (not isinstance(event_id, str) or not event_id.startswith('$')
            or not isinstance(sender, str) or not sender.startswith('@')
            or not isinstance(content, dict)
            or set(content) - {
                'msgtype', 'body', 'm.mentions', 'format', 'formatted_body'}
            or content.get('msgtype') != 'm.text'
            or content.get('m.mentions') not in (None, {})):
        return None
    has_format = 'format' in content or 'formatted_body' in content
    if has_format and (content.get('format') != 'org.matrix.custom.html'
                       or not isinstance(content.get('formatted_body'), str)):
        return None
    body = content.get('body')
    if not isinstance(body, str):
        return None
    command = unicodedata.normalize('NFC', body).strip()
    return (event_id, sender, command) if command in COMMANDS else None


def _digest_room_state(bot, room_id, user_id, *, allow_user_invite=False,
                       allow_missing_user=False, state=None):
    if (not isinstance(room_id, str) or not room_id.startswith('!')
            or not isinstance(user_id, str) or not user_id.startswith('@')
            or user_id == bot.user_id):
        raise DigestServiceError('invalid_digest_room_identity')
    if state is None:
        state = bot.get_room_state(room_id)
    if not isinstance(state, list) or len(state) > 20_000:
        raise DigestServiceError('invalid_digest_room_state')
    joined, invited, memberships, singleton = set(), set(), {}, {}
    for event in state:
        if (not isinstance(event, dict) or not isinstance(event.get('type'), str)
                or not isinstance(event.get('state_key'), str)
                or not isinstance(event.get('content'), dict)):
            raise DigestServiceError('invalid_digest_room_state')
        if event.get('type') == 'm.room.encryption':
            raise DigestServiceError('encrypted_digest_room_unsupported')
        if event.get('type') == 'm.room.member':
            membership = event['content'].get('membership')
            memberships[event['state_key']] = membership
            if membership in ('join', 'invite') and not event['state_key'].startswith('@'):
                raise DigestServiceError('invalid_digest_room_state')
            if membership == 'join':
                joined.add(event['state_key'])
            elif membership == 'invite':
                invited.add(event['state_key'])
        elif event.get('state_key') == '':
            if event['type'] in singleton:
                raise DigestServiceError('invalid_digest_room_state')
            singleton[event['type']] = event
    active = joined | invited
    if allow_user_invite:
        if (bot.user_id not in joined or joined - {bot.user_id, user_id}
                or active - {bot.user_id, user_id}
                or (not allow_missing_user and active != {bot.user_id, user_id})):
            raise DigestServiceError('digest_room_not_private')
    elif joined != {bot.user_id, user_id} or invited:
        raise DigestServiceError('digest_room_not_private')
    contents = {event_type: value['content'] for event_type, value in singleton.items()}
    if (contents.get('m.room.join_rules', {}).get('join_rule') != 'invite'
            or contents.get('m.room.guest_access', {}).get(
                'guest_access', 'forbidden') != 'forbidden'
            or contents.get('m.room.history_visibility', {}).get('history_visibility')
               == 'world_readable'):
        raise DigestServiceError('unsafe_digest_room_access')
    return singleton, memberships


def validate_digest_room(bot, room_id, user_id, *, state=None):
    _digest_room_state(bot, room_id, user_id, state=state)
    return True


def _limited_room_definitely_not_digest(bot, room_id):
    """Return true only when current state rules out a valid Digest DM.

    A limited global sync timeline can omit an earlier command.  It is safe to
    advance past that room only when the complete current state proves that the
    command would be rejected by ``validate_digest_room`` anyway.  Malformed or
    internally inconsistent state remains fail-closed.
    """
    state = bot.get_room_state(room_id)
    if not isinstance(state, list) or len(state) > 20_000:
        raise MatrixError('invalid_digest_room_state')
    joined, invited, seen = set(), set(), set()
    allowed_memberships = {'ban', 'invite', 'join', 'knock', 'leave'}
    for event in state:
        if (not isinstance(event, dict) or not isinstance(event.get('type'), str)
                or not isinstance(event.get('state_key'), str)
                or not isinstance(event.get('content'), dict)):
            raise MatrixError('invalid_digest_room_state')
        identity = (event['type'], event['state_key'])
        if identity in seen:
            raise MatrixError('invalid_digest_room_state')
        seen.add(identity)
        if event['type'] != 'm.room.member':
            continue
        membership = event['content'].get('membership')
        if (not isinstance(membership, str) or membership not in allowed_memberships
                or not event['state_key'].startswith('@')):
            raise MatrixError('invalid_digest_room_state')
        if membership == 'join':
            joined.add(event['state_key'])
        elif membership == 'invite':
            invited.add(event['state_key'])
    if bot.user_id not in joined:
        raise MatrixError('inconsistent_digest_room_state')
    peers = joined - {bot.user_id}
    if len(peers) != 1 or invited:
        return True
    user_id = next(iter(peers))
    try:
        validate_digest_room(bot, room_id, user_id, state=state)
    except DigestServiceError as exc:
        if str(exc) in {
                'encrypted_digest_room_unsupported',
                'digest_room_not_private',
                'unsafe_digest_room_access'}:
            return True
        raise MatrixError('invalid_digest_room_state') from None
    return False


def _validate_fallback_room(bot, room_id, user_id, target_sha256, *,
                            allow_missing_user=False, state=None):
    singleton, memberships = _digest_room_state(
        bot, room_id, user_id, allow_user_invite=True,
        allow_missing_user=allow_missing_user, state=state)
    create = singleton.get('m.room.create')
    power = singleton.get('m.room.power_levels')
    marker = create.get('content', {}).get(DIGEST_FALLBACK_MARKER) if create else None
    if (not create or create.get('sender') != bot.user_id
            or marker != {'version': 1, 'target_sha256': target_sha256}
            or not power or power.get('sender') != bot.user_id):
        raise DigestServiceError('invalid_digest_fallback_marker')
    levels = power['content']
    users, events = levels.get('users', {}), levels.get('events', {})
    if (not isinstance(users, dict) or set(users) - {bot.user_id, user_id}
            or not isinstance(events, dict)):
        raise DigestServiceError('unsafe_digest_fallback_power_levels')

    def exact_level(value, expected):
        return type(value) is int and value == expected

    def at_least(value, minimum):
        return type(value) is int and value >= minimum

    target_level = users.get(user_id, levels.get('users_default'))
    protected_events = {
        'm.room.power_levels', 'm.room.encryption', 'm.room.join_rules',
        'm.room.guest_access', 'm.room.history_visibility', 'm.room.name',
        'm.room.topic', 'm.room.third_party_invite'}
    state_default = levels.get('state_default')
    tombstone = events.get('m.room.tombstone')
    if (not exact_level(levels.get('users_default'), 0)
            or not exact_level(levels.get('events_default'), 0)
            or not exact_level(target_level, 0)
            or not at_least(state_default, 100)
            or any(not at_least(levels.get(key), 100)
                   for key in ('invite', 'kick', 'ban', 'redact'))
            or any(not at_least(events.get(event_type), 100)
                   for event_type in protected_events)
            or not at_least(tombstone, 101) or tombstone <= state_default):
        raise DigestServiceError('unsafe_digest_fallback_power_levels')
    return memberships


def split_markdown(text):
    if not isinstance(text, str) or not text:
        raise DigestServiceError('empty_digest')
    parts, remaining = [], text
    while remaining:
        if matrix_message_content(remaining, markdown_to_matrix_html(remaining))[1] <= MAX_EVENT_CONTENT_BYTES:
            parts.append(remaining)
            break
        low, high, best = 1, len(remaining), 0
        while low <= high:
            middle = (low + high) // 2
            candidate = remaining[:middle]
            if matrix_message_content(candidate, markdown_to_matrix_html(candidate))[1] <= MAX_EVENT_CONTENT_BYTES:
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
    if ''.join(parts) != text or len(parts) > 99:
        raise DigestServiceError('digest_split_failed')
    return parts


def _read_private(path, limit):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            metadata = os.fstat(handle.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or not 0 < metadata.st_size <= limit):
                raise DigestServiceError('unsafe_digest_file')
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise DigestServiceError('digest_too_large')
    except DigestServiceError:
        raise
    except OSError:
        raise DigestServiceError('digest_file_unreadable') from None
    return raw


def _read_digest(path):
    raw = _read_private(path, MAX_MARKDOWN_BYTES)
    try:
        text = validate_markdown(raw)
    except DigestBundleError as exc:
        raise DigestServiceError(str(exc)) from None
    return raw, text


def _read_ris(path):
    raw = _read_private(path, MAX_RIS_BYTES)
    try:
        text = validate_ris(raw)
    except DigestBundleError as exc:
        raise DigestServiceError(str(exc)) from None
    return raw, text


def _read_bundle(path):
    raw = _read_private(path, MAX_BUNDLE_BYTES)
    try:
        markdown, ris = unpack_bundle(raw)
    except DigestBundleError as exc:
        raise DigestServiceError(str(exc)) from None
    return raw, markdown, ris


def _atomic_private_write(directory, name, raw):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = os.lstat(directory)
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()):
        raise DigestServiceError('unsafe_digest_content_directory')
    target = directory / name
    if target.exists():
        existing = _read_private(target, max(MAX_MARKDOWN_BYTES, MAX_RIS_BYTES))
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
        self._startup_recovery = None
        self._startup_joined = {}

    def bootstrap(self):
        if self.state.snapshot()['since'] is not None:
            self._startup_recovery = True
            return False
        response = self.bot.sync(since=None, timeout_ms=0)
        cursor = response.get('next_batch') if isinstance(response, dict) else None
        if not isinstance(cursor, str) or not cursor:
            raise MatrixError('invalid_sync_response')
        self.state.bootstrap(cursor)
        self._startup_recovery = False
        logger.info('Digest-DM-Empfang initialisiert; vorhandene Historie wurde nicht ausgeführt.')
        return True

    def _invite_details(self, room):
        invite_state = room.get('invite_state', {}) if isinstance(room, dict) else {}
        events = invite_state.get('events', []) if isinstance(invite_state, dict) else []
        if not isinstance(events, list) or len(events) > 100:
            return None
        encrypted = any(isinstance(event, dict) and event.get('type') == 'm.room.encryption'
                        for event in events)
        candidates = [event.get('sender') for event in events
                      if isinstance(event, dict) and event.get('type') == 'm.room.member'
                      and event.get('state_key') == self.bot.user_id
                      and isinstance(event.get('content'), dict)
                      and event['content'].get('membership') == 'invite']
        candidates = [value for value in candidates
                      if isinstance(value, str) and value.startswith('@')]
        candidates = set(candidates)
        if len(candidates) != 1:
            return None
        sender = candidates.pop()
        if sender == self.bot.user_id:
            return None
        active = {event.get('state_key') for event in events
                  if isinstance(event, dict) and event.get('type') == 'm.room.member'
                  and isinstance(event.get('content'), dict)
                  and event['content'].get('membership') in ('join', 'invite')}
        return ((sender, encrypted)
                if active - {sender, self.bot.user_id} == set() else None)

    @staticmethod
    def _target_hash(user_id):
        return hashlib.sha256(user_id.encode('utf-8')).hexdigest()

    def _existing_fallback_room(self, user_id, target_sha256):
        subscription = self.state.snapshot()['subscriptions'].get(user_id)
        subscription_room = (subscription.get('room_id')
                             if isinstance(subscription, dict) else None)
        if isinstance(subscription_room, str):
            try:
                validate_digest_room(self.bot, subscription_room, user_id)
                return subscription_room, False
            except DigestServiceError:
                pass

        room_ids = self.bot.joined_room_ids()
        if len(room_ids) > MAX_FALLBACK_ROOM_SCAN:
            raise DigestServiceError('too_many_rooms_for_digest_fallback')
        safe_rooms, marked_rooms = [], []
        for room_id in sorted(room_ids):
            state = self.bot.get_room_state(room_id)
            try:
                validate_digest_room(self.bot, room_id, user_id, state=state)
                safe_rooms.append(room_id)
            except DigestServiceError:
                pass
            try:
                _validate_fallback_room(
                    self.bot, room_id, user_id, target_sha256,
                    allow_missing_user=True, state=state)
                marked_rooms.append(room_id)
            except DigestServiceError:
                pass
        if safe_rooms:
            return safe_rooms[0], False
        if len(marked_rooms) > 1:
            raise DigestServiceError('duplicate_digest_fallback_rooms')
        return (marked_rooms[0], True) if marked_rooms else (None, False)

    def _ensure_fallback_room(self, user_id, *, allow_create):
        target_sha256 = self._target_hash(user_id)
        room_id, marked = self._existing_fallback_room(user_id, target_sha256)
        created = False
        if room_id is None:
            if not allow_create:
                raise DigestServiceError('digest_fallback_create_limit')
            # This POST is never retried blindly. On any ambiguous result the
            # marker scan above reconciles a server-created room on the next pass.
            room_id = self.bot.create_digest_fallback_room(user_id, target_sha256)
            created = True
            marked = True
        if marked:
            memberships = _validate_fallback_room(
                self.bot, room_id, user_id, target_sha256,
                allow_missing_user=True)
            if user_id not in memberships:
                self.bot.invite_user(room_id, user_id)
            elif memberships[user_id] not in ('invite', 'join'):
                raise DigestServiceError('digest_fallback_target_inactive')
            _validate_fallback_room(self.bot, room_id, user_id, target_sha256)
        else:
            validate_digest_room(self.bot, room_id, user_id)
        transaction = 'digest-e2ee-fallback-' + target_sha256[:40]
        sent = self.bot.send_message(
            DIGEST_FALLBACK_NOTICE, room_id=room_id, transaction_id=transaction)
        self._verify_text(room_id, sent, DIGEST_FALLBACK_NOTICE)
        logger.info('Unverschlüsselter privater Ersatzraum für Digest-Einladung bestätigt.')
        return room_id, created

    def _join_invitations(self, response):
        rooms = response.get('rooms', {}) if isinstance(response, dict) else {}
        invited = rooms.get('invite', {}) if isinstance(rooms, dict) else {}
        if not isinstance(invited, dict) or len(invited) > 50:
            raise MatrixError('invalid_sync_response')
        joined, changed, creates = [], False, 0
        for room_id, room in sorted(invited.items()):
            if not isinstance(room_id, str) or not room_id.startswith('!'):
                raise MatrixError('invalid_sync_response')
            details = self._invite_details(room)
            if details is None:
                logger.warning('Digest-Einladung ignoriert: Absender oder Privatraum nicht prüfbar.')
                continue
            sender, encrypted = details
            if encrypted:
                _fallback, created = self._ensure_fallback_room(
                    sender, allow_create=creates == 0)
                creates += int(created)
                changed = True
                continue
            self.bot.join_room(room_id)
            joined.append((room_id, sender))
            changed = True
            logger.info('Privater Digest-Raumeinladung beigetreten; prüfe ersten Befehl.')
        return joined, changed

    def _commands(self, response, *, only_room_id=None):
        rooms = response.get('rooms', {}) if isinstance(response, dict) else {}
        joined = rooms.get('join', {}) if isinstance(rooms, dict) else {}
        if not isinstance(joined, dict) or len(joined) > 20_000:
            raise MatrixError('invalid_sync_response')
        if (only_room_id is not None
                and any(room_id != only_room_id for room_id in joined)):
            raise MatrixError('unexpected_sync_room')
        commands = []
        for room_id, room in joined.items():
            if only_room_id is not None and room_id != only_room_id:
                continue
            if not isinstance(room_id, str) or not room_id.startswith('!'):
                raise MatrixError('invalid_sync_response')
            timeline = room.get('timeline', {}) if isinstance(room, dict) else None
            events = timeline.get('events', []) if isinstance(timeline, dict) else None
            limited = timeline.get('limited', False) if isinstance(timeline, dict) else None
            if (not isinstance(events, list) or len(events) > 50
                    or type(limited) is not bool):
                raise MatrixError('invalid_sync_response')
            if limited:
                if (only_room_id is not None
                        or not _limited_room_definitely_not_digest(self.bot, room_id)):
                    raise MatrixError('invalid_sync_response')
                logger.warning(
                    'Begrenzte Timeline eines eindeutig ungeeigneten Digest-Raums übersprungen.')
                continue
            for event in events:
                command = digest_command_from_event(event)
                if command is not None and command[1] != self.bot.user_id:
                    commands.append((room_id, *command))
        return commands

    def _catch_up_joined_rooms(self, joined, *, since):
        if not isinstance(since, str) or not since:
            raise MatrixError('invalid_sync_cursor')
        commands = []
        for room_id, _sender in joined:
            response = self.bot.sync(since=since, room_id=room_id, timeout_ms=0)
            cursor = response.get('next_batch') if isinstance(response, dict) else None
            if not isinstance(cursor, str) or not cursor:
                raise MatrixError('invalid_sync_response')
            commands.extend(self._commands(response, only_room_id=room_id))
        for room_id, event_id, user_id, command in commands:
            self._process_command(room_id, event_id, user_id, command)
        return bool(commands)

    def reconcile_invitations(self):
        """Sweep current invites without replacing the persisted Digest cursor."""
        response = self.bot.sync(since=None, timeout_ms=0)
        cursor = response.get('next_batch') if isinstance(response, dict) else None
        if not isinstance(cursor, str) or not cursor:
            raise MatrixError('invalid_sync_response')
        joined, changed = self._join_invitations(response)
        self._startup_joined.update(joined)
        for room_id, user_id in sorted(self._startup_joined.items()):
            try:
                validate_digest_room(self.bot, room_id, user_id)
            except DigestServiceError:
                continue
            notice = ('Ich habe deine persönliche Digest-Einladung jetzt angenommen. '
                      'Falls du „Digest“ oder „Digest aus“ bereits zusammen mit der '
                      'Einladung gesendet hast, sende den Befehl bitte hier noch einmal.')
            transaction = 'digest-invite-retry-' + _transaction(room_id)[:40]
            sent = self.bot.send_message(
                notice, room_id=room_id, transaction_id=transaction)
            self._verify_text(room_id, sent, notice)
        self._startup_joined.clear()
        return changed

    def _verify_text(self, room_id, event_id, text, formatted=None):
        event = self.bot.read_event(room_id, event_id)
        content = event.get('content', {}) if isinstance(event, dict) else {}
        if (event.get('event_id', event_id) != event_id
                or event.get('type') != 'm.room.message'
                or event.get('sender') != self.bot.user_id
                or content.get('body') != text
                or (formatted is not None
                    and (content.get('format') != 'org.matrix.custom.html'
                         or content.get('formatted_body') != formatted))):
            raise MatrixError('matrix_readback_mismatch')

    def _verify_file(self, room_id, event_id, content_uri, filename, size):
        event = self.bot.read_event(room_id, event_id)
        content = event.get('content', {}) if isinstance(event, dict) else {}
        expected = {
            'msgtype': 'm.file', 'body': filename, 'filename': filename,
            'url': content_uri,
            'info': {'mimetype': 'application/x-research-info-systems', 'size': size},
        }
        if (event.get('event_id', event_id) != event_id
                or event.get('type') != 'm.room.message'
                or event.get('sender') != self.bot.user_id
                or content != expected):
            raise MatrixError('matrix_file_readback_mismatch')

    def _digest_resources(self, digest):
        path = self.state.content_directory / digest['content_file']
        raw, text = _read_digest(path)
        if hashlib.sha256(raw).hexdigest() != digest['sha256']:
            raise DigestServiceError('digest_content_hash_mismatch')
        match = PAIRED_CONTENT_NAME.fullmatch(digest['content_file'])
        if match is None:
            return split_markdown(text), text, None, None
        if match.group(2) != digest['sha256']:
            raise DigestServiceError('digest_content_hash_mismatch')
        ris_path = path.with_suffix('.ris')
        ris_raw, _ris_text = _read_ris(ris_path)
        if hashlib.sha256(ris_raw).hexdigest() != match.group(3):
            raise DigestServiceError('digest_ris_hash_mismatch')
        return (split_markdown(text), text, ris_raw,
                match.group(1) + '-methoden-artikel.ris')

    def _ensure_ris_media(self, date, digest, ris_raw, ris_name):
        if ris_raw is None or ris_name is None:
            raise DigestServiceError('digest_ris_unavailable')
        content_uri = digest['ris_mxc']
        if content_uri is None:
            content_uri = self.bot.create_media_uri()
            self.state.record_digest_media(date, content_uri)
            digest = self.state.snapshot()['digests'][date]
        if not digest['ris_uploaded']:
            self.bot.upload_media(content_uri, ris_raw, ris_name)
            self.state.mark_digest_media_uploaded(date, content_uri)
        return content_uri

    def _latest_newsletter(self):
        snapshot = self.state.snapshot()
        for date in sorted(snapshot['digests'], reverse=True):
            digest = snapshot['digests'][date]
            parts, text, ris_raw, ris_name = self._digest_resources(digest)
            if ris_raw is not None and REGULAR_NEWSLETTER.match(text):
                return date, digest, parts, ris_raw, ris_name
        return None

    def _send_welcome_newsletter(self, room_id, event_id, latest):
        date, digest, parts, ris_raw, ris_name = latest
        content_uri = self._ensure_ris_media(date, digest, ris_raw, ris_name)
        transaction_root = 'digest-welcome-' + _transaction(event_id)[:36]
        for index, text in enumerate(parts):
            formatted = markdown_to_matrix_html(text)
            sent = self.bot.send_message(
                text, room_id=room_id, html_msg=formatted,
                transaction_id=transaction_root + '-m' + str(index + 1))
            self._verify_text(room_id, sent, text, formatted)
        sent = self.bot.send_file(
            content_uri, ris_name, len(ris_raw), room_id=room_id,
            transaction_id=transaction_root + '-ris')
        self._verify_file(room_id, sent, content_uri, ris_name, len(ris_raw))
        return True

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
            latest = None
            message = ('Digest deaktiviert. Du erhältst in diesem Raum keine weiteren '
                       'wöchentlichen Methoden-Journal-Digests.')
        else:
            latest = self._latest_newsletter()
            message = ('Digest aktiviert. Du erhältst künftig den wöchentlichen '
                       'Methoden-Journal-Digest in diesem privaten Raum. Mit „Digest aus“ '
                       'kannst du ihn wieder abbestellen. '
                       + ('Die letzte verfügbare Ausgabe folgt sofort.' if latest is not None
                          else 'Sobald eine Ausgabe verfügbar ist, wird sie hier zugestellt.'))
        transaction = 'digest-command-' + _transaction(event_id)[:40]
        sent = self.bot.send_message(message, room_id=room_id, transaction_id=transaction)
        self._verify_text(room_id, sent, message)
        if latest is not None:
            self._send_welcome_newsletter(room_id, event_id, latest)
        self.state.apply_command(event_id, command, user_id, room_id)

    def poll_once(self, *, timeout_ms=30_000):
        snapshot = self.state.snapshot()
        if snapshot['since'] is None:
            return self.bootstrap()
        startup_changed = False
        if self._startup_recovery is not None:
            startup_changed = self.reconcile_invitations()
            self._startup_recovery = None
        response = self.bot.sync(since=snapshot['since'], timeout_ms=timeout_ms)
        cursor = response.get('next_batch') if isinstance(response, dict) else None
        if not isinstance(cursor, str) or not cursor:
            raise MatrixError('invalid_sync_response')
        joined, invite_changed = self._join_invitations(response)
        catch_up_changed = (self._catch_up_joined_rooms(
            joined, since=snapshot['since']) if joined else False)
        commands = self._commands(response)
        for room_id, event_id, user_id, command in commands:
            self._process_command(room_id, event_id, user_id, command)
        self.state.advance_cursor(cursor)
        return bool(commands) or startup_changed or invite_changed or catch_up_changed

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
            match = DIGEST_BUNDLE_NAME.fullmatch(path.name)
            if match is None:
                continue
            try:
                Date.fromisoformat(match.group(1))
            except ValueError:
                raise DigestServiceError('invalid_digest_date') from None
            bundle_raw, markdown_raw, ris_raw = _read_bundle(path)
            digest_hash = hashlib.sha256(markdown_raw).hexdigest()
            ris_hash = hashlib.sha256(ris_raw).hexdigest()
            content_stem = match.group(1) + '-' + digest_hash + '-' + ris_hash
            content_name = content_stem + '.md'
            target = _atomic_private_write(
                self.state.content_directory, content_name, markdown_raw)
            _atomic_private_write(self.state.content_directory, content_stem + '.ris', ris_raw)
            _raw, text = _read_digest(target)
            parts = split_markdown(text)
            self.state.stage_digest(match.group(1), digest_hash, target.name, len(parts) + 1)
            # The protected content copy and delivery plan are durable. Remove
            # the one-shot inbox entry so old weeks cannot be restaged after
            # bounded state-history pruning.
            check_raw, _check_markdown, _check_ris = _read_bundle(path)
            if hashlib.sha256(check_raw).hexdigest() != hashlib.sha256(bundle_raw).hexdigest():
                raise DigestServiceError('digest_inbox_changed_during_stage')
            path.unlink()
            inbox_fd = os.open(self.inbox, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(inbox_fd)
            finally:
                os.close(inbox_fd)
        self.state.finish_empty_digests()

    def _deliver(self):
        snapshot = self.state.snapshot()
        for date in sorted(snapshot['digests']):
            digest = snapshot['digests'][date]
            if digest['status'] != 'pending':
                continue
            parts, _text, ris_raw, ris_name = self._digest_resources(digest)
            expected_parts = len(parts) + (1 if ris_raw is not None else 0)
            if any(len(recipient['parts']) != expected_parts
                   for recipient in digest['recipients'].values()):
                raise DigestServiceError('digest_delivery_plan_mismatch')
            content_uri = (self._ensure_ris_media(date, digest, ris_raw, ris_name)
                           if ris_raw is not None else None)
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
                        formatted = markdown_to_matrix_html(text)
                        event_id = recipient['parts'][index]
                        transaction = ('digest-' + date.replace('-', '') + '-'
                                       + digest['sha256'][:12] + '-'
                                       + _transaction(user_id)[:12] + '-' + str(index + 1))
                        if event_id is None:
                            event_id = self.bot.send_message(
                                text, room_id=recipient['room_id'], html_msg=formatted,
                                transaction_id=transaction)
                        self._verify_text(recipient['room_id'], event_id, text, formatted)
                        self.state.record_part(date, user_id, index, event_id)
                        recipient['parts'][index] = event_id
                    if ris_raw is not None:
                        index = len(parts)
                        event_id = recipient['parts'][index]
                        transaction = ('digest-' + date.replace('-', '') + '-'
                                       + digest['sha256'][:12] + '-'
                                       + _transaction(user_id)[:12] + '-ris')
                        if event_id is None:
                            event_id = self.bot.send_file(
                                content_uri, ris_name, len(ris_raw),
                                room_id=recipient['room_id'], transaction_id=transaction)
                        self._verify_file(recipient['room_id'], event_id, content_uri,
                                          ris_name, len(ris_raw))
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
