from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

import exchangemail
import main
from ai_summary import AISettings, DEFAULT_MODEL, SummaryUnavailable, prepare_input, validate_summary
from configuration import Configuration
from form_table_compat import FIELDS, FormShapeError
from stats_table_manager import StatsTableManager


class FakeMatrix:
    def __init__(self, fail_part=None):
        self.calls = []
        self.fail_part = fail_part

    def send_message(self, msg, room_id=None, thread_reply_to=None, html_msg=None, transaction_id=None):
        self.calls.append(dict(msg=msg, room_id=room_id, thread_reply_to=thread_reply_to,
                               html_msg=html_msg, transaction_id=transaction_id))
        if self.fail_part == len(self.calls):
            raise requests.Timeout('private Matrix exception')
        return '$event-' + str(len(self.calls))


def message(label_tag='th', missing=()):
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'examples/quantitativ.json').read_text())
    values = dict(art='Abschlussarbeit', datensatz='daten.csv',
                  rskript=fixture['request']['rskript'], fachgebiet='Methoden', fachsemester='4',
                  fragen=fixture['request']['fragen'], beschreibung=fixture['request']['beschreibung'],
                  betreuung='Beispielperson', präregistrierung='protokoll.pdf', studiengang='Psychologie')
    rows = [f'<tr><{label_tag}>{html.escape(label)}</{label_tag}><td>{html.escape(values[field])}</td></tr>'
            for field, label in FIELDS.items() if field not in missing]
    return SimpleNamespace(body='<table class="powermail_all">' + ''.join(rows) + '</table>',
                           sender=SimpleNamespace(name='Musterfrau, Erika'), subject='Synthetic request',
                           message_id='<fixture@example.invalid>',
                           datetime_received=datetime(2026, 8, 4, 11, 51, 29, tzinfo=timezone.utc),
                           datetime_sent=None, headers=[SimpleNamespace(name='X-Mailer', value='TYPO3')]), fixture


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = SimpleNamespace(ai=AISettings(enabled=True),
                                      processed_file=str(Path(self.directory.name) / 'processed.csv'),
                                      matrix_room_id='!fixture:example.invalid',
                                      google_form_link='https://example.invalid/form?')
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.stats = Mock(HEADERS=['message_id', 'tmid', 'sender_name'])

    def summary(self, item, fixture):
        return validate_summary(json.dumps(fixture['fixture_response']),
                                prepare_input(exchangemail.parse_email_data(item)), DEFAULT_MODEL)

    def test_ai_and_details_are_ordered_sibling_replies_under_original(self):
        item, fixture = message()
        bot = FakeMatrix()
        seen = set()
        with patch('summary_selection.BestOfThreeSummarizer.summarize', return_value=self.summary(item, fixture)) as summarize:
            self.assertTrue(exchangemail.process_email(self.config, None, item, seen, bot, self.stats))
        self.assertEqual(len(bot.calls), 3)
        self.assertTrue(bot.calls[1]['msg'].startswith('### KI-Zusammenfassung'))
        self.assertEqual(bot.calls[2]['thread_reply_to'], '$event-1')
        self.assertIsNone(bot.calls[0]['thread_reply_to'])
        self.assertEqual(bot.calls[1]['thread_reply_to'], '$event-1')
        self.assertIn(item.message_id, seen)
        self.stats.append_record.assert_called_once()
        self.assertEqual(self.stats.append_record.call_args.args[0]['tmid'], '$event-1')
        summarize.assert_called_once()
        # Compare both original message bodies/HTML with the AI-disabled path.
        self.config.ai = AISettings(enabled=False)
        without_ai = FakeMatrix()
        exchangemail.process_email(self.config, None, item, set(), without_ai, Mock(HEADERS=[]))
        self.assertEqual(len(without_ai.calls), 2)
        for actual, original in zip([bot.calls[0], bot.calls[2]], without_ai.calls):
            self.assertEqual(actual['msg'], original['msg'])
            self.assertEqual(actual['html_msg'], original['html_msg'])
            self.assertEqual(actual['thread_reply_to'], original['thread_reply_to'])

    def test_gwdg_failure_still_delivers_original_and_thread(self):
        item, _ = message()
        bot = FakeMatrix()
        with patch('summary_selection.BestOfThreeSummarizer.summarize', side_effect=SummaryUnavailable('api_network_error')):
            result = exchangemail.process_email(self.config, None, item, set(), bot, self.stats)
        self.assertTrue(result)
        self.assertEqual(len(bot.calls), 3)
        self.assertIn('nicht verfügbar', bot.calls[1]['msg'])
        self.assertIn('Musterfrau, Erika', bot.calls[0]['msg'])
        self.assertIn('Beschreibung:', bot.calls[2]['msg'])
        self.assertEqual(bot.calls[1]['thread_reply_to'], '$event-1')
        self.assertEqual(bot.calls[2]['thread_reply_to'], '$event-1')

    def test_failed_ai_thread_send_leaves_request_retryable(self):
        item, fixture = message()
        bot = FakeMatrix(fail_part=2)
        with patch('summary_selection.BestOfThreeSummarizer.summarize', return_value=self.summary(item, fixture)):
            seen = set()
            with self.assertRaises(exchangemail.DeliveryNotConfirmed):
                exchangemail.process_email(self.config, None, item, seen, bot, self.stats)
            self.assertNotIn(item.message_id, seen)
            self.assertFalse(Path(self.config.processed_file).exists())
            self.assertTrue(exchangemail.process_email(self.config, None, item, seen, bot, self.stats))
        self.assertEqual(len(bot.calls), 5)
        self.assertIn('Musterfrau, Erika', bot.calls[0]['msg'])
        self.assertTrue(bot.calls[-2]['msg'].startswith('### KI-Zusammenfassung'))
        self.assertIsNotNone(bot.calls[-1]['thread_reply_to'])

    def test_unconfirmed_original_prevents_ai_and_detail_send(self):
        item, _ = message()
        bot = FakeMatrix(fail_part=1)
        with patch('summary_selection.BestOfThreeSummarizer.summarize') as summarize:
            with self.assertRaisesRegex(RuntimeError, 'Originalnachricht nicht bestätigt'):
                exchangemail.process_email(self.config, None, item, set(), bot, self.stats)
            summarize.assert_not_called()
        self.assertEqual(len(bot.calls), 1)
        self.stats.append_record.assert_not_called()
        self.assertFalse(Path(self.config.processed_file).exists())

    def test_duplicate_notification_does_not_repeat_ai_or_original(self):
        item, fixture = message()
        bot, seen = FakeMatrix(), set()
        with patch('summary_selection.BestOfThreeSummarizer.summarize', return_value=self.summary(item, fixture)) as summarize:
            self.assertTrue(exchangemail.process_email(self.config, None, item, seen, bot, self.stats))
            self.assertFalse(exchangemail.process_email(self.config, None, item, seen, bot, self.stats))
            summarize.assert_called_once()
        self.assertEqual(len(bot.calls), 3)

    def test_stats_failure_leaves_request_retryable_with_same_matrix_transactions(self):
        item, _ = message()
        self.config.ai = AISettings(enabled=False)
        bot, seen = FakeMatrix(), set()
        failing = Mock(HEADERS=['message_id'])
        failing.append_record.side_effect = OSError('private path')
        with self.assertRaises(OSError):
            exchangemail.process_email(self.config, None, item, seen, bot, failing)
        self.assertNotIn(item.message_id, seen)
        self.assertFalse(Path(self.config.processed_file).exists())
        working = Mock(HEADERS=['message_id'])
        self.assertTrue(exchangemail.process_email(self.config, None, item, seen, bot, working))
        self.assertEqual([call['transaction_id'] for call in bot.calls[:2]],
                         [call['transaction_id'] for call in bot.calls[2:]])

    def test_startup_batch_propagates_delivery_failure_after_processing_other_mail(self):
        failed, _ = message()
        successful, _ = message()
        successful.message_id = '<second@example.invalid>'
        self.config.ai = AISettings(enabled=False)
        bot, seen = FakeMatrix(fail_part=1), set()
        with self.assertRaises(exchangemail.DeliveryNotConfirmed):
            exchangemail.process_many_emails(
                [failed, successful], self.config, None, seen, bot, self.stats)
        self.assertNotIn(failed.message_id, seen)
        self.assertIn(successful.message_id, seen)
        self.assertEqual(self.stats.append_record.call_count, 1)

    def test_stats_table_deduplicates_retried_message_id(self):
        path = Path(self.directory.name) / 'stats.csv'
        stats = StatsTableManager(str(path))
        record = {'message_id': '<same@example.invalid>', 'sender_name': 'Name'}
        self.assertTrue(stats.append_record(record))
        self.assertFalse(stats.append_record(record))
        self.assertEqual(len(stats.df), 1)

    def test_main_matrix_html_escapes_all_form_fields(self):
        data = {key: '<img src=x onerror=alert(1)>' for key in
                ('sender_name', 'art', 'betreuung', 'fachgebiet', 'studiengang', 'fachsemester')}
        data['received_date'] = '<img src=x onerror=alert(1)>'
        bot = FakeMatrix()
        exchangemail.matrix_post_message(bot, data)
        self.assertNotIn('<img', bot.calls[0]['html_msg'])
        self.assertIn('&lt;img', bot.calls[0]['html_msg'])

    def test_oversized_root_uses_short_confirmable_fallback(self):
        data = {key: '\U0001f600' * 5000 for key in
                ('sender_name', 'art', 'betreuung', 'fachgebiet', 'studiengang', 'fachsemester')}
        data['received_date'] = '2026-08-31'
        bot = FakeMatrix()
        self.assertEqual(exchangemail.matrix_post_message(bot, data), '$event-1')
        self.assertIn('Kurzübersicht war für Matrix zu lang', bot.calls[0]['msg'])
        self.assertLess(len(bot.calls[0]['msg']), 300)

    def test_oversized_detail_uses_short_confirmable_fallback(self):
        item, _ = message()
        data = exchangemail.parse_email_data(item)
        data['beschreibung'] = 'A' * 14500
        data['fragen'] = 'B' * 14500
        bot = FakeMatrix()
        result = exchangemail.matrix_post_detail_thread(
            bot, data, '$root', self.config, transaction_id='detail-fallback')
        self.assertEqual(result, '$event-1')
        self.assertIn('zu lang', bot.calls[0]['msg'])
        self.assertIsNone(bot.calls[0]['html_msg'])

    def test_detail_survives_empty_sender_and_missing_received_date(self):
        item, _ = message()
        data = exchangemail.parse_email_data(item)
        data['sender_name'] = ''
        data['received_date'] = None
        bot = FakeMatrix()
        self.assertEqual(exchangemail.matrix_post_detail_thread(
            bot, data, '$root', self.config), '$event-1')
        self.assertIn('Unbekannt', bot.calls[0]['msg'])

    def test_non_form_mail_does_not_call_ai_or_send(self):
        item, _ = message()
        item.headers = []
        bot = FakeMatrix()
        with patch('summary_selection.BestOfThreeSummarizer.summarize') as summarize:
            self.assertFalse(exchangemail.process_email(self.config, None, item, set(), bot, self.stats))
            summarize.assert_not_called()
        self.assertEqual(bot.calls, [])

    def test_message_without_stable_id_is_never_sent(self):
        item, _ = message()
        item.message_id = None
        bot = FakeMatrix()
        with self.assertRaisesRegex(ValueError, 'stabile Message-ID'):
            exchangemail.process_email(self.config, None, item, set(), bot, self.stats)
        self.assertEqual(bot.calls, [])

    def test_invalid_form_stops_before_ai_or_any_send(self):
        item, _ = message(missing=('fragen',))
        bot = FakeMatrix()
        with patch('summary_selection.BestOfThreeSummarizer.summarize') as summarize:
            with self.assertRaises(FormShapeError):
                exchangemail.process_email(self.config, None, item, set(), bot, self.stats)
            summarize.assert_not_called()
        self.assertEqual(bot.calls, [])

    def test_invalid_final_config_stops_before_matrix_exchange_and_csv_setup(self):
        invalid = Mock()
        invalid.validate_final_runtime.side_effect = RuntimeError('Fehlende Konfiguration')
        with patch('main.Configuration', return_value=invalid), \
                patch('main.matrixbot.MatrixBot') as bot, patch('main.StatsTableManager') as stats, \
                patch('main.exchangemail.init_exchange_connection') as exchange:
            with self.assertRaisesRegex(RuntimeError, 'Fehlende Konfiguration'):
                main.main()
        bot.assert_not_called()
        stats.assert_not_called()
        exchange.assert_not_called()

    def test_config_only_loads_explicit_copy_env_and_defaults_off(self):
        with patch.dict(os.environ, {}, clear=True), patch('configuration.load_dotenv') as dotenv:
            config = Configuration()
        self.assertFalse(config.allow_unencrypted_control_dm)
        self.assertFalse(config.allow_unencrypted_digest_dm)
        self.assertFalse(config.ai.enabled)
        self.assertFalse(config.ai.transfer_approved)
        self.assertIsNone(config.matrix_control_user)
        self.assertEqual(dotenv.call_args.args[0], Path(__file__).resolve().parents[1] / '.env')
        self.assertEqual(Path(config.processed_file).parent, Path(__file__).resolve().parents[1])

    def test_control_chat_consent_is_required_by_final_validation(self):
        config = Configuration.__new__(Configuration)
        for name in ('uk_nummer', 'email_address', 'email_password', 'ews_endpoint', 'matrix_server',
                     'matrix_user', 'matrix_password', 'matrix_room_id', 'matrix_console_room_id',
                     'google_form_link'):
            setattr(config, name, 'https://matrix.invalid' if name == 'matrix_server' else 'value')
        config.matrix_room_id, config.matrix_console_room_id = '!prod:x', '!control:x'
        config.matrix_control_user = '@controller:example.org'
        config.allow_unencrypted_control_dm = False
        with self.assertRaisesRegex(RuntimeError, 'nicht ausdrücklich freigegeben'):
            config.validate_final_runtime()

    def test_control_user_must_be_an_explicit_matrix_id(self):
        config = Configuration.__new__(Configuration)
        for name in ('uk_nummer', 'email_address', 'email_password', 'ews_endpoint', 'matrix_server',
                     'matrix_user', 'matrix_password', 'matrix_room_id', 'matrix_console_room_id',
                     'google_form_link'):
            setattr(config, name, 'https://matrix.invalid' if name == 'matrix_server' else 'value')
        config.matrix_room_id, config.matrix_console_room_id = '!prod:x', '!control:x'
        config.matrix_control_user = 'not-a-matrix-user'
        config.allow_unencrypted_control_dm = True
        with self.assertRaisesRegex(RuntimeError, 'keine gültige Matrix-Benutzer-ID'):
            config.validate_final_runtime()

    def test_digest_dm_consent_is_required_by_final_validation(self):
        config = Configuration.__new__(Configuration)
        for name in ('uk_nummer', 'email_address', 'email_password', 'ews_endpoint', 'matrix_server',
                     'matrix_user', 'matrix_password', 'matrix_room_id', 'matrix_console_room_id',
                     'google_form_link'):
            setattr(config, name, 'https://matrix.invalid' if name == 'matrix_server' else 'value')
        config.matrix_room_id, config.matrix_console_room_id = '!prod:x', '!control:x'
        config.matrix_control_user = '@controller:example.org'
        config.allow_unencrypted_control_dm = True
        config.allow_unencrypted_digest_dm = False
        with self.assertRaisesRegex(RuntimeError, 'Digest-PNs'):
            config.validate_final_runtime()


class ParserRegressionTests(unittest.TestCase):
    def test_th_td_and_td_td_are_equivalent(self):
        th, _ = message('th')
        td, _ = message('td')
        self.assertEqual(exchangemail.parse_email_data(th), exchangemail.parse_email_data(td))

    def test_mixed_newlines_and_quoted_printable_like_text_are_preserved(self):
        item, _ = message()
        soup = exchangemail.BeautifulSoup(item.body, 'html.parser')
        description = 'Zeile eins\r\nZeile zwei\n=AB =3D Grüße & < >\rEnde'
        for row in soup.find_all('tr'):
            cells = row.find_all(['th', 'td'])
            if cells[0].text == FIELDS['beschreibung']:
                cells[1].clear()
                cells[1].append(description)
        item.body = str(soup)
        self.assertEqual(exchangemail.parse_email_data(item)['beschreibung'], description)

    def test_optional_fields_can_be_absent(self):
        item, _ = message(missing=('rskript', 'datensatz', 'präregistrierung', 'betreuung'))
        parsed = exchangemail.parse_email_data(item)
        self.assertIsNone(parsed['rskript'])
        self.assertEqual(parsed['betreuung'], '...')


class ProcessedEmailStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'processed.csv'

    def test_corrupt_existing_ledger_fails_closed(self):
        self.path.write_text('wrong\nvalue\n', encoding='utf-8')
        self.path.chmod(0o600)
        with self.assertRaises(exchangemail.ProcessedEmailStateError):
            exchangemail.load_processed_emails(str(self.path))

    def test_atomic_save_roundtrip_and_mode(self):
        self.assertTrue(exchangemail.save_processed_email(str(self.path), '<one@example.invalid>'))
        self.assertFalse(exchangemail.save_processed_email(str(self.path), '<one@example.invalid>'))
        self.assertEqual(exchangemail.load_processed_emails(str(self.path)),
                         {'<one@example.invalid>'})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_failed_cleanup_preserves_file_and_in_memory_set(self):
        exchangemail.save_processed_email(str(self.path), '<one@example.invalid>')
        original = self.path.read_bytes()
        processed = {'<one@example.invalid>'}
        messages = [SimpleNamespace(message_id='<other@example.invalid>')]
        with patch('exchangemail.os.replace', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                exchangemail.clean_up_processed_file(str(self.path), messages, processed)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(processed, {'<one@example.invalid>'})

    def test_cleanup_ignores_inbox_message_without_optional_message_id(self):
        exchangemail.save_processed_email(str(self.path), '<one@example.invalid>')
        processed = {'<one@example.invalid>'}
        result = exchangemail.clean_up_processed_file(
            str(self.path), [SimpleNamespace(message_id=None)], processed)
        self.assertEqual(result, set())
        self.assertEqual(exchangemail.load_processed_emails(str(self.path)), set())
