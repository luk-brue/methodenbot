from configuration import Configuration
from stats_table_manager import StatsTableManager
import matrixbot
import exchangemail
import logging
import traceback


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
    format="{asctime} - {levelname} - {message}",
    style="{",
    datefmt="%Y-%m-%d %H:%M",)


def main():
    envvars = Configuration()
    bot = matrixbot.MatrixBot(envvars)
    stats = StatsTableManager()
    try:
        account = exchangemail.init_exchange_connection(envvars)
        processed_emails = exchangemail.load_processed_emails(envvars.processed_file)
        logger.info("Lade Emails aus der INBOX (max. 100)...")
        messages = list(account.inbox.all().order_by('-datetime_received')[:100])
        logger.info(f"{len(messages)} Emails aus INBOX geholt.")
        exchangemail.clean_up_processed_file(envvars.processed_file, messages, processed_emails)
        exchangemail.process_many_emails(messages, envvars, account, processed_emails, bot, stats)
    except Exception as e:
        logger.error(f"Fehler beim ersten Abruf: {e}")
    try:
        exchangemail.maintain_notification_streaming(account, envvars, processed_emails, stats, bot, 29)    
    except Exception as e:
        logger.error(f"Fehler beim Notification Streaming und Verarbeiten neuer Mails: {e}")
        logger.error(traceback.format_exc())


if __name__ == "__main__":
    main()
