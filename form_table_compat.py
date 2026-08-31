"""In-memory HTML compatibility adapter; no credentials, I/O or delivery.

Accept a label in either TH or TD, followed by a TD value. Only whitespace in
known field labels is normalized. All form values stay unchanged. The actual
production parser and renderers still produce the outgoing message.
"""

import quopri
from types import SimpleNamespace


FIELDS = {
    'art': 'Art der Arbeit (Empra/ WHA/ Projekt/- oder Abschlussarbeit...)\n',
    'datensatz': 'Bei R Fragen: Datensatz° ',
    'rskript': 'Bei R Fragen: R Skript (bitte Code einfach in das Feld kopieren)\n',
    'fachgebiet': 'Fachgebiet, dem die Betreuungsperson angehört (z.B. "Entwicklungspsychologie")\n',
    'fachsemester': 'Fachsemester ',
    'fragen': 'Konkreten Fragen + Eigene Lösungsansätze? ° ',
    'beschreibung': 'Kurze Beschreibung des Projekts (Hypothesen, Ablauf, erhobene Variablen, Datenstruktur, geplante Analyse)\n',
    'betreuung': 'Name der Betreuungsperson ',
    'präregistrierung': 'Präregistrierung ',
    'studiengang': 'Studiengang ',
}
REQUIRED = {'art', 'fachgebiet', 'fachsemester', 'fragen', 'beschreibung', 'studiengang'}


class FormShapeError(ValueError):
    def __init__(self, reason, **safe_details):
        # Callers pass only fixed labels, booleans and counts, never form values.
        super().__init__(reason)
        self.safe_details = {'reason': reason, **safe_details}


def encode_qp_exact(raw):
    """Quoted-printable without the stdlib encoder's newline normalization."""
    encoded = ''.join(f'={byte:02X}' for byte in raw)
    wrapped = '=\n'.join(encoded[offset:offset + 72] for offset in range(0, len(encoded), 72))
    if quopri.decodestring(wrapped.encode('ascii')) != raw:
        raise FormShapeError('quoted_printable_roundtrip_failed')
    return wrapped


def assert_value_unchanged(field, expected, actual, report):
    if actual == expected:
        return
    normalize = lambda text: text.replace('\r\n', '\n').replace('\r', '\n')
    raise FormShapeError(
        'parsed_value_changed', field=field, **report,
        comparison={'expected_length': len(expected),
                    'actual_length': len(actual) if isinstance(actual, str) else None,
                    'same_after_newline_normalization': isinstance(actual, str)
                    and normalize(actual) == normalize(expected),
                    'expected_cr_count': expected.count('\r'),
                    'actual_cr_count': actual.count('\r') if isinstance(actual, str) else None})


def parse_compatible(api, item):
    soup = api.BeautifulSoup(str(item.body or ''), 'html.parser')
    tables = soup.find_all('table', class_='powermail_all')
    if len(tables) != 1:
        raise FormShapeError('expected_one_form_table', form_table_count=len(tables))
    table = tables[0]
    if table.find('table') is not None:
        raise FormShapeError('nested_table_not_supported')
    normalize = lambda label: ' '.join(label.split())
    known = {normalize(label): field for field, label in FIELDS.items()}
    report = {'rows': 0, 'th_td_rows': 0, 'td_td_rows': 0, 'labels_whitespace_normalized': 0}
    seen = set()
    original_values = {}
    for row in table.find_all('tr'):
        cells = row.find_all(['th', 'td'], recursive=False)
        if not cells:
            continue
        report['rows'] += 1
        if len(cells) != 2 or cells[1].name != 'td':
            raise FormShapeError('expected_label_and_value_cells', row_number=report['rows'],
                                 cell_count=len(cells))
        report['th_td_rows' if cells[0].name == 'th' else 'td_td_rows'] += 1
        label = cells[0].text
        normalized = normalize(label)
        if normalized in seen:
            raise FormShapeError('duplicate_form_label', row_number=report['rows'])
        seen.add(normalized)
        cells[0].name = 'td'
        field = known.get(normalized)
        if field is not None:
            original_values[field] = cells[1].text
            canonical = FIELDS[field]
            if label != canonical:
                report['labels_whitespace_normalized'] += 1
            cells[0].clear()
            cells[0].append(canonical)
    missing = sorted(REQUIRED - original_values.keys())
    if missing:
        raise FormShapeError('required_form_fields_not_found', missing_fields=missing, **report)
    report['known_fields_present'] = sorted(original_values)
    report['required_fields_present'] = True
    # Exchange Body is already decoded. Encode exactly once because the legacy
    # parser unconditionally decodes quoted-printable; this preserves e.g. =AB
    # in R code and URLs rather than treating it as an encoded byte by accident.
    encoded = encode_qp_exact(str(table).encode('utf-8'))
    report['quoted_printable_roundtrip_verified'] = True
    proxy = SimpleNamespace(body=encoded, **{
        name: getattr(item, name, None) for name in
        ('sender', 'subject', 'datetime_received', 'datetime_sent', 'message_id')})
    try:
        parsed = api.parse_email_data(proxy)
    except Exception as exc:
        details = {'error_type_only': type(exc).__name__}
        if (isinstance(exc, KeyError) and len(exc.args) == 1
                and isinstance(exc.args[0], str) and exc.args[0] in FIELDS.values()):
            details['missing_label_from_code'] = exc.args[0]
        raise FormShapeError('production_parser_failed_after_adaptation', **details) from None
    # Check the parser has not lost/altered the substantive free-text fields.
    for field in ('beschreibung', 'fragen'):
        assert_value_unchanged(field, original_values[field], parsed.get(field), report)
    for field in ('art', 'fachgebiet', 'fachsemester', 'studiengang', 'betreuung'):
        if field in original_values:
            assert_value_unchanged(field, original_values[field].strip(), parsed.get(field), report)
    if 'rskript' in original_values:
        assert_value_unchanged('rskript', '\n'.join(original_values['rskript'].splitlines()),
                               parsed.get('rskript'), report)
    report['parsed_values_verified'] = True
    return parsed, report
