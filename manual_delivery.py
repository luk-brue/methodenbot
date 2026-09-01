"""Read-only request selection and rendering for explicit Matrix commands."""

import hashlib
import html
import itertools

import exchangemail


class ManualDeliveryError(RuntimeError):
    pass


FIELDS = ('headers', 'subject', 'sender', 'datetime_received', 'datetime_sent', 'body', 'message_id')


def source_folders(exchange):
    folders = [('inbox', exchange.inbox)]
    correspondence = exchange.root // 'Oberste Ebene des Informationsspeichers' // 'Korrespondenz'
    seen = {exchange.inbox.id}
    for folder in itertools.chain((correspondence,), correspondence.walk()):
        if folder.id in seen:
            continue
        seen.add(folder.id)
        if getattr(folder, 'folder_class', None) not in (None, 'IPF.Note'):
            continue
        folders.append(('correspondence', folder))
        if len(folders) > 150:
            raise ManualDeliveryError('too_many_source_folders')
    return folders


def _identity(item):
    value = getattr(item, 'message_id', None)
    if not isinstance(value, str) or not value.strip():
        raise ManualDeliveryError('request_without_stable_id')
    return value


def select_latest_requests(exchange, count):
    """Return globally newest distinct requests, newest first; fail closed on limits."""
    if type(count) is not int or not 1 <= count <= 3:
        raise ManualDeliveryError('invalid_request_count')
    selected, fingerprints, examined = {}, {}, 0
    rank = lambda item: (item.datetime_received, _identity(item))
    for _scope, folder in source_folders(exchange):
        query = folder.all()
        if len(selected) == count:
            query = query.filter(datetime_received__gte=min(x.datetime_received for x in selected.values()))
        for position, item in enumerate(query.order_by('-datetime_received').only(*FIELDS)[:501], 1):
            examined += 1
            if position > 500 or examined > 5000:
                raise ManualDeliveryError('request_search_limit')
            if not getattr(item, 'datetime_received', None):
                raise ManualDeliveryError('request_without_received_time')
            if len(selected) == count and item.datetime_received < min(x.datetime_received for x in selected.values()):
                break
            if not exchangemail.is_typo3_contact_form(item):
                continue
            identity = _identity(item)
            fingerprint = hashlib.sha256(str(item.body).encode('utf-8')).hexdigest()
            if identity in fingerprints and fingerprints[identity] != fingerprint:
                raise ManualDeliveryError('conflicting_request_copies')
            fingerprints[identity] = fingerprint
            previous = selected.get(identity)
            if previous is None or rank(item) > rank(previous):
                selected[identity] = item
            if len(selected) > count:
                del selected[_identity(min(selected.values(), key=rank))]
    if len(selected) != count:
        raise ManualDeliveryError('not_enough_distinct_requests')
    return sorted(selected.values(), key=rank, reverse=True)


class _Capture:
    def __init__(self):
        self.parts = []

    def send_message(self, msg, room_id=None, thread_reply_to=None, html_msg=None, transaction_id=None):
        self.parts.append({'msg': msg, 'html_msg': html_msg, 'thread_reply_to': thread_reply_to})
        return '$capture-' + str(len(self.parts))


def render_original(email_data, config):
    capture = _Capture()
    root = exchangemail.matrix_post_message(capture, email_data)
    detail = exchangemail.matrix_post_detail_thread(capture, email_data, root, config)
    if (root != '$capture-1' or detail != '$capture-2' or len(capture.parts) != 2
            or capture.parts[0]['thread_reply_to'] is not None
            or capture.parts[1]['thread_reply_to'] != root):
        raise ManualDeliveryError('original_render_incomplete')
    return capture.parts[0], capture.parts[1]


def _transaction(command_event_id, index):
    digest = hashlib.sha256(command_event_id.encode('utf-8')).hexdigest()[:40]
    return 'control-' + digest + '-' + str(index)


def _part(msg, html_msg, room_id, thread_root_part, transaction_id):
    if (not isinstance(msg, str) or not msg or html_msg is not None and not isinstance(html_msg, str)
            or not isinstance(room_id, str) or not room_id.startswith('!')
            or thread_root_part is not None and type(thread_root_part) is not int):
        raise ManualDeliveryError('invalid_rendered_part')
    return {'msg': msg, 'html_msg': html_msg, 'room_id': room_id,
            'thread_root_part': thread_root_part, 'transaction_id': transaction_id,
            'event_id': None}


def build_test_plan(exchange, config, ai_service, *, command, command_event_id, ai_enabled,
                    reply_room_id=None):
    """Freeze all test content before the first test-content Matrix side effect."""
    if command not in ('Test', 'Test 2'):
        raise ManualDeliveryError('invalid_test_command')
    count = 3 if command == 'Test' else 1
    items = select_latest_requests(exchange, count)
    # Test displays the three requests chronologically; Test 2 uses the newest.
    if command == 'Test':
        items = list(reversed(items))
    parsed = [exchangemail.parse_email_data(item) for item in items]
    rendered = []
    for email_data in parsed:
        root, detail = render_original(email_data, config)
        ai = ai_service.render(email_data, enabled=ai_enabled)
        rendered.append((root, ai, detail))

    reply_room_id = (config.matrix_console_room_id
                     if reply_room_id is None else reply_room_id)
    room_id = reply_room_id if command == 'Test' else config.matrix_room_id
    parts = []
    for request_index, (root, ai, detail) in enumerate(rendered, 1):
        label = ('Test · ' + str(request_index) + '/3') if command == 'Test' else 'Techniktest'
        text = label + '\n\n' + root['msg']
        formatted = '<h2>' + html.escape(label) + '</h2>' + (root['html_msg'] or '')
        root_index = len(parts)
        parts.append(_part(text, formatted, room_id, None,
                           _transaction(command_event_id, len(parts))))
        if ai is not None:
            parts.append(_part(ai['msg'], ai['html_msg'], room_id, root_index,
                               _transaction(command_event_id, len(parts))))
        parts.append(_part(detail['msg'], detail['html_msg'], room_id, root_index,
                           _transaction(command_event_id, len(parts))))
    if command == 'Test 2':
        notice = 'Techniktest im echten Zielkanal wurde vollständig zugestellt und zurückgelesen.'
        parts.append(_part(notice, '<p>' + html.escape(notice) + '</p>',
                           reply_room_id, None,
                           _transaction(command_event_id, len(parts))))
    return parts


def build_test_wait_ack(config, command_event_id, command, ai_enabled, *, reply_room_id=None):
    if command not in ('Test', 'Test 2'):
        raise ManualDeliveryError('invalid_test_command')
    if command == 'Test':
        text = 'Test angenommen. Ich bereite die letzten drei Anfragen vor.'
    else:
        text = ('Test 2 angenommen. Ich bereite die letzte Anfrage für den Techniktest '
                'im echten Zielkanal vor.')
    text += (' Je nach globaler KI-Einstellung kann die Vorbereitung mehrere Minuten dauern. '
             'Weitere Befehle werden seriell bearbeitet.')
    digest = hashlib.sha256(command_event_id.encode('utf-8')).hexdigest()[:40]
    reply_room_id = (config.matrix_console_room_id
                     if reply_room_id is None else reply_room_id)
    return _part(text, '<p>' + html.escape(text) + '</p>', reply_room_id,
                 None, 'control-' + digest + '-accepted')


def build_toggle_ack(config, command_event_id, enabled, *, reply_room_id=None):
    text = ('KI-Zusammenfassungen sind jetzt eingeschaltet.' if enabled
            else 'KI-Zusammenfassungen sind jetzt ausgeschaltet.')
    reply_room_id = (config.matrix_console_room_id
                     if reply_room_id is None else reply_room_id)
    return [_part(text, '<p>' + html.escape(text) + '</p>', reply_room_id,
                  None, _transaction(command_event_id, 0))]


def build_failure_ack(config, command_event_id, code, *, reply_room_id=None):
    safe_codes = {
        'ai_unavailable': 'KI konnte nicht eingeschaltet werden; Freigabe oder lokaler Zugang fehlen.',
        'test_failed': ('Der Test konnte vor dem Versand nicht vollständig vorbereitet werden. '
                        'Es wurden keine Testinhalte versendet.'),
    }
    text = safe_codes.get(code, 'Der Befehl konnte sicher nicht ausgeführt werden.')
    reply_room_id = (config.matrix_console_room_id
                     if reply_room_id is None else reply_room_id)
    return [_part(text, '<p>' + html.escape(text) + '</p>', reply_room_id,
                  None, _transaction(command_event_id, 0))]


def build_delivery_failure_ack(config, command_event_id, command, *, reply_room_id=None):
    if command in ('KI an', 'KI aus'):
        state = 'eingeschaltet' if command == 'KI an' else 'ausgeschaltet'
        text = ('Der KI-Schalter wurde ' + state
                + ', aber die ursprüngliche Bestätigung konnte nicht zugestellt werden.')
    else:
        text = ('Der Testbefehl wurde nach wiederholtem Zustellfehler beendet. '
                'Eine Teilzustellung ist möglich; es erfolgt keine automatische Wiederholung.')
    digest = hashlib.sha256(command_event_id.encode('utf-8')).hexdigest()[:40]
    reply_room_id = (config.matrix_console_room_id
                     if reply_room_id is None else reply_room_id)
    return [_part(text, '<p>' + html.escape(text) + '</p>', reply_room_id,
                  None, 'control-' + digest + '-delivery-failed')]
