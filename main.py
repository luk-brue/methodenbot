# own stuff
from configuration import Configuration
import matrixbot
# import exchangemail
# existing libraries
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG,
    format="{asctime} - {levelname} - {message}",
    style="{",
    datefmt="%Y-%m-%d %H:%M",)

envvars = Configuration()

def main():
    logger.info("Starting matrix bot")
    bot = matrixbot.MatrixBot(envvars)
    bot.send_message("Hallo! Das hier wurde von Python gesendet")

if __name__ == "__main__":
    main()
# end main