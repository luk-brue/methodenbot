"""Runtime AI service: local gateway only, one shared paced request stream."""

import logging

from ai_summary import (AISettings, APIPacer, LOCAL_GATEWAY_URL, SummaryUnavailable,
                        render_summary, render_unavailable)
from summary_selection import BestOfThreeSummarizer


logger = logging.getLogger(__name__)


class AISummaryService:
    def __init__(self, settings, *, pacer=None, summarizer_factory=None):
        if not isinstance(settings, AISettings):
            raise TypeError('AISettings required')
        self.settings = settings
        self.pacer = pacer or APIPacer(min_interval=22)
        self._factory = summarizer_factory

    def check_available(self):
        """Validate administrative approval and the local token without exposing it."""
        self.settings.read_key()
        return True

    @staticmethod
    def _report(record):
        # Do not log minimized inputs, candidate summaries, response bodies or tokens.
        phase = record.get('phase') if isinstance(record, dict) else None
        status = record.get('http_status') if isinstance(record, dict) else None
        if isinstance(phase, str):
            logger.info('KI-Phase %s%s', phase, '' if status is None else ' (HTTP ' + str(status) + ')')

    def _summarizer(self):
        if self._factory is not None:
            return self._factory(self.settings)
        return BestOfThreeSummarizer(
            self.settings,
            api_url=LOCAL_GATEWAY_URL,
            max_attempts=10,
            pacer=self.pacer,
            report=self._report,
        )

    def render(self, email_data, *, enabled):
        if not enabled:
            return None
        try:
            summary = self._summarizer().summarize(email_data)
            text, formatted = render_summary(summary)
            return {'status': 'summary_ready', 'msg': text, 'html_msg': formatted}
        except SummaryUnavailable as exc:
            logger.warning('KI-Zusammenfassung nicht verfügbar: %s', str(exc))
            text, formatted = render_unavailable(exc.safe_details)
        except Exception as exc:
            logger.warning('KI-Zusammenfassung intern fehlgeschlagen: %s', type(exc).__name__)
            text, formatted = render_unavailable()
        return {'status': 'unavailable', 'msg': text, 'html_msg': formatted}

    def post_thread_reply(self, matrixbot, email_data, thread_root, *, enabled, transaction_id=None):
        rendered = self.render(email_data, enabled=enabled)
        if rendered is None:
            return 'disabled'
        try:
            options = dict(msg=rendered['msg'], html_msg=rendered['html_msg'],
                           thread_reply_to=thread_root)
            if transaction_id is not None:
                options['transaction_id'] = transaction_id
            event_id = matrixbot.send_message(**options)
        except Exception as exc:
            logger.warning('KI-Thread-Antwort nicht bestätigt: %s.', type(exc).__name__)
            return 'send_unconfirmed'
        return rendered['status'] if isinstance(event_id, str) and event_id.startswith('$') else 'send_unconfirmed'
