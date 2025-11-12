# own stuff
from configuration import Configuration
from stats_table_manager import StatsTableManager
import matrixbot
import exchangemail
# import exchangemail
# existing libraries
import logging
import html
import traceback
#from exchangelib import Configuration, Credentials, Account, DELEGATE, Message



logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
    format="{asctime} - {levelname} - {message}",
    style="{",
    datefmt="%Y-%m-%d %H:%M",)


def main():
    envvars = Configuration()
    logger.info("Starting matrix bot")
    bot = matrixbot.MatrixBot(envvars)
    # bot.send_message("Hallo! Das hier wurde von **Python** gesendet\n```r\nfunction() <- x\n```", 
    #     thread_reply_to="$VKK8yix7f9gXgC9Ki1ZeslGgG7bB5sBSbYp-uRvbakQ",
    #     html_msg=f'<p>Hi</p><code class="language-java">{html.escape('a <-> function(x){1+x}')}</code>')
    stats = StatsTableManager()
    try:
        account = exchangemail.init_exchange_connection(envvars)
        processed_emails=exchangemail.load_processed_emails(envvars.processed_file)
        logger.info("Lade max. 100 Emails aus der INBOX")
        messages = list(account.inbox.all().order_by('-datetime_received')[:100])
        logger.info(f"{len(messages)} Emails geladen.")
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
# end main