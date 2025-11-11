import logging
import os
import json
import requests
import time
import uuid
from typing import Optional
# own stuff: 
from configuration import Configuration

# Logging konfigurieren
logger = logging.getLogger(__name__)

class MatrixBot:
    """
    Tries to connect the bot to the Client-Server-API, using either a cached AccessToken or
    tries to obtain a token using password login. 
    """
    def __init__(self, envvars: Configuration):
        # use botlib Creds class for encrypted storage
        self._session_stored_file = "matrix_token_cache.txt"
        self.homeserver = envvars.matrix_server
        self.username = envvars.matrix_user
        self.password = envvars.matrix_password
        self.room_id = envvars.matrix_room_id
        if not os.path.exists(self._session_stored_file):
            self.password_login()
            self.write_token_cache()
        else:
            self.read_token_cache()
        # Try to connect to API using cached token:
        status = self.token_whoami()
        # In case the token was expired:
        if status == 401:
            self.password_login()
            self.write_token_cache()
       
    def password_login(self):
        try: 
            response = requests.post(f"{self.homeserver}/_matrix/client/v3/login", 
                json = {
                "type": "m.login.password",
                "user": self.username,
                "password": self.password
                })
            response.raise_for_status()
        except requests.exceptions.HTTPError as e: 
            logger.exception("Fehler beim Login des Matrix Bots mit Passwort:")
            logger.info("Versuche erneute Anfrage zu senden in 10 Sekunden.")
            time.sleep(10)
            response = requests.post(f"{self.homeserver}/_matrix/client/v3/login", 
                json = {
                "type": "m.login.password",
                "user": self.username,
                "password": self.password
                })
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.exception("Nicht-HTTP-Fehler beim Request für Login des Matrix Bots mit Passwort:")

        try:
            jr = json.loads(response.text)
            self.access_token = jr['access_token']
            self.device_id = jr['device_id']
        except Exception:
            logger.exception("Error during parsing of request body after login of the matrix bot.")
            raise
        logger.debug("Matrix Bot erfolgreich eingeloggt mit Passwort.")

    def token_whoami(self):
        try:
            response = requests.get(f'{self.homeserver}/_matrix/client/v3/account/whoami',
                    headers={'Authorization': f'Bearer {self.access_token}'})
            response.raise_for_status()
            logger.info("Matrix Bot erfolgreich mit API verbunden über AccessToken.")
            return response.status_code
        except requests.exceptions.HTTPError as e: 
            logger.exception("Fehler bei Whoami-API Anfrage des Matrix Bots mit AccessToken")
            return response.status_code
    
    def write_token_cache(self):
        with open(self._session_stored_file, mode='w') as f:
            f.write(self.access_token)
        logger.debug("Matrix AccessToken in Cache Datei geschrieben.")
    
    def read_token_cache(self):
        with open(self._session_stored_file, mode='r') as f:
            self.access_token=f.read()
        logger.debug("Matrix AccessToken aus Cache Datei gelesen.")
    
    def send_message(self, msg):
        try:
            room_id = self.room_id
            randomuuid = str(uuid.uuid4())
            url = f'{self.homeserver}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{randomuuid}'
            headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
            payload = {"msgtype": "m.text", "body": msg}
            response = requests.put(url, headers=headers, json=payload, timeout=15)
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            logger.exception("Fehler beim Senden der Nachricht:")
        try:
            logger.info("Matrix-Bot hat Nachricht gesendet.")
            return response.json
        except Exception:
            logger.exception("Fehler beim Senden der Nachricht")
