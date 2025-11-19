from dotenv import load_dotenv
import os

class Configuration:
    def __init__(self):
        load_dotenv()
        self.uk_nummer = os.getenv("UK_NUMMER")
        self.email_address = os.getenv("EMAIL_ADDRESS")
        self.email_password = os.getenv("EMAIL_PASSWORD")
        self.matrix_password = os.getenv("MATRIX_PASSWORD")
        self.matrix_server = os.getenv("MATRIX_SERVER", "").rstrip('/')
        self.matrix_user = os.getenv("MATRIX_USER")
        self.processed_file = 'processed_emails.csv'
        self.ews_endpoint = os.getenv("EWS_ENDPOINT")
        self.bot_command_prefix = os.getenv("BOT_COMMAND_PREFIX")
        self.matrix_room_id = os.getenv("MATRIX_ROOM_ID")
        self.matrix_console_room_id = os.getenv("MATRIX_CONSOLE_ROOM_ID")
        self.google_form_link = os.getenv("GOOGLE_FORM_LINK")
