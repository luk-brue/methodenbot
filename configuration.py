from dotenv import load_dotenv
import os
import logging
from pathlib import Path
import re
from ai_summary import AISettings

logger = logging.getLogger(__name__)

def _enabled(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() == 'true'

class Configuration:
    def __init__(self):
        # Never discover a production .env in a parent directory. Production may
        # explicitly point at a protected file outside the immutable release.
        code_dir = Path(__file__).resolve().parent
        env_file = Path(os.getenv('METHODENBOT_ENV_FILE', str(code_dir / '.env')))
        load_dotenv(env_file, override=False)
        self.env_file = str(env_file)
        self.state_dir = Path(os.getenv('METHODENBOT_STATE_DIR', str(code_dir)))
        self.ai = AISettings.from_environment()
        logger.debug("Umgebungsvariablen aus .env-Datei lesen...")
        self.uk_nummer = os.getenv("UK_NUMMER")
        self.email_address = os.getenv("EMAIL_ADDRESS")
        self.email_password = os.getenv("EMAIL_PASSWORD")
        self.matrix_password = os.getenv("MATRIX_PASSWORD")
        self.matrix_server = os.getenv("MATRIX_SERVER", "").rstrip('/')
        self.matrix_user = os.getenv("MATRIX_USER")
        self.processed_file = str(self.state_dir / 'processed_emails.csv')
        self.stats_file = str(self.state_dir / 'stats.csv')
        self.ews_endpoint = os.getenv("EWS_ENDPOINT")
        self.bot_command_prefix = os.getenv("BOT_COMMAND_PREFIX")
        self.matrix_room_id = os.getenv("MATRIX_ROOM_ID")
        self.matrix_console_room_id = os.getenv("MATRIX_CONSOLE_ROOM_ID")
        self.matrix_control_user = os.getenv("MATRIX_CONTROL_USER")
        self.matrix_device_id = os.getenv("MATRIX_DEVICE_ID", "METHODENBOT_FINAL_2026_08")
        self.matrix_token_file = os.getenv("MATRIX_TOKEN_FILE", str(self.state_dir / 'matrix-session.json'))
        self.allow_unencrypted_control_dm = _enabled("MATRIX_ALLOW_UNENCRYPTED_CONTROL_DM")
        self.control_state_dir = os.getenv("METHODENBOT_CONTROL_STATE_DIR", str(self.state_dir / 'control'))
        self.digest_state_dir = os.getenv("METHODENBOT_DIGEST_STATE_DIR", str(self.state_dir / 'digest'))
        self.digest_inbox_dir = os.getenv(
            "METHODENBOT_DIGEST_INBOX", str(self.state_dir / 'digest' / 'inbox'))
        self.allow_unencrypted_digest_dm = _enabled("MATRIX_ALLOW_UNENCRYPTED_DIGEST_DM")
        self.ai_default_enabled = _enabled("METHODENBOT_AI_DEFAULT_ENABLED")
        self.google_form_link = os.getenv("GOOGLE_FORM_LINK")
        self.dev_enable_token_cache = _enabled("DEV_ENABLE_TOKEN_CACHE")

    def validate_final_runtime(self):
        """Fail before external I/O when the final service is incompletely configured."""
        required = {
            'UK_NUMMER': self.uk_nummer,
            'EMAIL_ADDRESS': self.email_address,
            'EMAIL_PASSWORD': self.email_password,
            'EWS_ENDPOINT': self.ews_endpoint,
            'MATRIX_SERVER': self.matrix_server,
            'MATRIX_USER': self.matrix_user,
            'MATRIX_PASSWORD': self.matrix_password,
            'MATRIX_ROOM_ID': self.matrix_room_id,
            'MATRIX_CONSOLE_ROOM_ID': self.matrix_console_room_id,
            'MATRIX_CONTROL_USER': self.matrix_control_user,
            'GOOGLE_FORM_LINK': self.google_form_link,
        }
        missing = [name for name, value in required.items() if not isinstance(value, str) or not value.strip()]
        if missing:
            raise RuntimeError('Fehlende Konfiguration: ' + ', '.join(missing))
        if not re.fullmatch(r'@[^\s:]+:[^\s]+', self.matrix_control_user):
            raise RuntimeError('MATRIX_CONTROL_USER ist keine gültige Matrix-Benutzer-ID')
        if self.matrix_console_room_id == self.matrix_room_id:
            raise RuntimeError('Kontrollraum und produktiver Zielraum müssen verschieden sein')
        if not self.allow_unencrypted_control_dm:
            raise RuntimeError('Unverschlüsselte Kontroll-PN ist nicht ausdrücklich freigegeben')
        if not self.allow_unencrypted_digest_dm:
            raise RuntimeError('Unverschlüsselte Digest-PNs sind nicht ausdrücklich freigegeben')
        if not self.matrix_server.startswith('https://'):
            raise RuntimeError('MATRIX_SERVER muss HTTPS verwenden')
