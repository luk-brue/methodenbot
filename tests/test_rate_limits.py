import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from ai_summary import (AISettings, APIPacer, DEFAULT_MODEL, GWDGSummarizer, SummaryUnavailable,
                        prepare_input, validate_summary, render_unavailable)


class Clock:
    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class RateTests(unittest.TestCase):
    def setUp(self):
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('No real network'))
        guard.start()
        self.addCleanup(guard.stop)
        self.fixture = json.loads((Path(__file__).resolve().parents[1] / 'examples/quantitativ.json').read_text())
        self.clock = Clock()
        self.pacer = APIPacer(clock=self.clock.time, sleep=self.clock.sleep)

    def response(self, status=200, headers=None, content=None):
        body = {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(content or self.fixture['fixture_response'])}}]}
        return SimpleNamespace(status_code=status, headers=headers or {}, content=b'{}', json=lambda: body)

    def client(self, responses):
        pending, calls, reports = iter(responses), [], []

        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(inner, url, **kwargs):
                calls.append((self.clock.time(), kwargs))
                result = next(pending)
                if isinstance(result, Exception):
                    raise result
                return result

        client = GWDGSummarizer(AISettings(True, True, api_key='SYNTHETIC-KEY'),
                               session_factory=Session, pacer=self.pacer, report=reports.append)
        return client, calls, reports

    def test_two_distinct_requests_have_minimum_sixty_second_gap(self):
        client, calls, _ = self.client([self.response(), self.response()])
        client.summarize(self.fixture['request'])
        client.summarize(self.fixture['request'])
        self.assertGreaterEqual(calls[1][0] - calls[0][0], 60)
        self.assertTrue(all(seconds <= 15 for seconds in self.clock.sleeps))

    def test_http_500_retry_has_same_spacing_and_exact_status_recorded(self):
        client, calls, reports = self.client([self.response(500), self.response()])
        client.summarize(self.fixture['request'])
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1][0] - calls[0][0], 60)
        self.assertEqual(next(r for r in reports if r['phase'] == 'ai_attempt_failed')['http_status'], 500)

    def test_retry_after_and_gwdg_reset_are_respected(self):
        client, calls, _ = self.client([self.response(429, {'Retry-After': '70', 'ratelimit-reset': '90'}), self.response()])
        client.summarize(self.fixture['request'])
        self.assertGreaterEqual(calls[1][0] - calls[0][0], 91)

    def test_hour_quota_exhaustion_stops_all_further_calls_in_process(self):
        client, calls, _ = self.client([self.response(429, {'x-ratelimit-remaining-hour': '0'})])
        for _ in range(2):
            with self.assertRaisesRegex(SummaryUnavailable, 'api_rate_pause'):
                client.summarize(self.fixture['request'])
        self.assertEqual(len(calls), 1)

    def test_limit_headers_can_only_slow_down_the_conservative_default(self):
        self.pacer.observe({'x-ratelimit-limit-minute': '1000'}, 200)
        self.assertEqual(self.pacer.interval, 60)
        self.pacer.observe({'x-ratelimit-limit-minute': '1'}, 200)
        self.assertGreaterEqual(self.pacer.interval, 66)

    def test_daily_limit_is_monitored_but_not_smoothed_over_twenty_four_hours(self):
        pacer = APIPacer(min_interval=22, clock=self.clock.time, sleep=self.clock.sleep)
        pacer.observe({'x-ratelimit-limit-minute': '30',
                       'x-ratelimit-limit-hour': '200',
                       'x-ratelimit-limit-day': '1000',
                       'x-ratelimit-remaining-day': '999'}, 200)
        self.assertEqual(pacer.interval, 22)
        pacer.observe({'x-ratelimit-remaining-day': '0'}, 200)
        with self.assertRaisesRegex(SummaryUnavailable, 'api_rate_pause'):
            pacer.wait(lambda record: None)

    def test_schema_repair_also_waits_and_never_echoes_invalid_output(self):
        bad = json.loads(json.dumps(self.fixture['fixture_response']))
        bad['software']['beleg'] = 'INVENTED_PRIVATE_QUOTE'
        client, calls, reports = self.client([self.response(content=bad), self.response()])
        client.summarize(self.fixture['request'])
        self.assertGreaterEqual(calls[1][0] - calls[0][0], 60)
        feedback = calls[1][1]['json']['messages'][-1]['content']
        self.assertIn('software.beleg', feedback)
        self.assertNotIn('INVENTED_PRIVATE_QUOTE', feedback)
        self.assertEqual(next(r for r in reports if r['phase'] == 'ai_attempt_failed')['next_action'], 'repair')

    def test_invalid_output_after_one_repair_remains_rejected(self):
        bad = json.loads(json.dumps(self.fixture['fixture_response']))
        bad['software']['beleg'] = 'NOT_IN_INPUT'
        client, calls, _ = self.client([self.response(content=bad), self.response(content=bad)])
        with self.assertRaisesRegex(SummaryUnavailable, 'evidence_not_in_input'):
            client.summarize(self.fixture['request'])
        self.assertEqual(len(calls), 2)

    def test_auth_errors_do_not_trigger_extra_calls(self):
        client, calls, _ = self.client([self.response(401)])
        with self.assertRaises(SummaryUnavailable) as caught:
            client.summarize(self.fixture['request'])
        self.assertEqual(caught.exception.safe_details['http_status'], 401)
        self.assertEqual(len(calls), 1)

    def test_timeout_retry_obeys_interval_and_sanitizes_error(self):
        client, calls, reports = self.client([requests.Timeout('SENSITIVE'), self.response()])
        client.summarize(self.fixture['request'])
        self.assertGreaterEqual(calls[1][0] - calls[0][0], 60)
        self.assertNotIn('SENSITIVE', json.dumps(reports))

    def test_precise_length_diagnostics_and_failure_notice_do_not_leak_content(self):
        bad = self.fixture['fixture_response']
        bad['kurzfassung'] = 'x' * 241
        with self.assertRaises(SummaryUnavailable) as caught:
            validate_summary(json.dumps(bad), prepare_input(self.fixture['request']), DEFAULT_MODEL)
        self.assertEqual(caught.exception.safe_details, dict(error_code='invalid_output_schema',
                         field='kurzfassung', reason='too_long', limit=240, actual_length=241))
        text, formatted = render_unavailable({'error_code': 'api_http_error', 'http_status': 500,
                                             'field': 'SENSITIVE', 'reason': 'SENSITIVE'})
        self.assertIn('HTTP 500', text)
        self.assertNotIn('SENSITIVE', text + formatted)


if __name__ == '__main__':
    unittest.main()
