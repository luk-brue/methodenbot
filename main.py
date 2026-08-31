"""Production entry point for the final Methodenbot."""

import logging
import os
from pathlib import Path
import threading
import traceback

from ai_service import AISummaryService
from configuration import Configuration
from control_state import ControlState
import exchangemail
from matrix_commands import MatrixCommandListener
import matrixbot
from stats_table_manager import StatsTableManager


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
    format="{asctime} - {levelname} - {message}",
    style="{",
    datefmt="%Y-%m-%d %H:%M",)


def main():
    os.umask(0o077)
    config = Configuration()
    config.validate_final_runtime()
    Path(config.state_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chdir(Path(__file__).resolve().parent)

    state = ControlState(config.control_state_dir, ai_default=config.ai_default_enabled)
    ai_service = AISummaryService(config.ai)
    if config.ai.api_key or not config.ai.api_key_file:
        raise RuntimeError('Finaler Dienst verlangt eine lokale Credential-Datei, keinen direkten API-Key')
    ai_service.check_available()
    config.control_state = state
    config.ai_service = ai_service

    bot = matrixbot.MatrixBot(config)
    listener = MatrixCommandListener(
        bot, config, state, ai_service,
        account_factory=lambda: exchangemail.init_exchange_connection(config))
    # Bootstrap synchronously: old personal messages must be skipped before the
    # normal mail service begins. Security failure stops the whole startup.
    state.acquire_process_lock()
    listener.bootstrap()

    account = exchangemail.init_exchange_connection(config)
    stats = StatsTableManager(config.stats_file)
    processed_emails = exchangemail.load_processed_emails(config.processed_file)

    control_thread = threading.Thread(target=listener.run_forever,
                                      name='matrix-control', daemon=True)
    control_thread.start()
    try:
        logger.info("Lade E-Mails aus der INBOX (max. 100)...")
        messages = list(account.inbox.all().order_by('-datetime_received')[:100])
        logger.info("%d E-Mails aus der INBOX geholt.", len(messages))
        exchangemail.clean_up_processed_file(config.processed_file, messages, processed_emails)
        exchangemail.process_many_emails(messages, config, account, processed_emails, bot, stats)
    except Exception as exc:
        logger.error("Fehler beim ersten Abruf: %s", type(exc).__name__)
        logger.error(traceback.format_exc())
        raise
    try:
        exchangemail.maintain_notification_streaming(
            account, config, processed_emails, stats, bot, 29)
    except Exception as exc:
        logger.error("Fehler beim Notification Streaming: %s", type(exc).__name__)
        logger.error(traceback.format_exc())
        raise
    finally:
        listener.stop_event.set()
        control_thread.join(timeout=5)


if __name__ == "__main__":
    main()
