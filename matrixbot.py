import configparser
import logging
import os
import json
import requests
import time
import uuid

from oauthlib.oauth1.rfc5849.endpoints import access_token

# own stuff:
from configuration import Configuration

# Logging konfigurieren
logger = logging.getLogger(__name__)

class MatrixBot:
    """
    Tries to connect the bot to the Client-Server-API, using either a cached AccessToken or
    tries to obtain a token using password login. 
    No methods for cryptography provided here. 
    """
    def __init__(self, envvars: Configuration):
        logger.info("Starting matrix bot.")
        self.device_id = None
        self.access_token = None
        self.homeserver = envvars.matrix_server
        self.username = envvars.matrix_user
        self.password = envvars.matrix_password
        self.room_id = envvars.matrix_room_id

        self.password_login()
        if self.access_token is None:
            logger.exception("Matrix Access Token could not be obtained for some reason.") 

       
    def password_login(self):
        response = requests.post(f"{self.homeserver}/_matrix/client/v3/login",
            json = {
                "type": "m.login.password",
                "user": self.username,
                "password": self.password
            })
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as e: 
            logger.exception("Error at login with password:")
            logger.info("Retry in 10 seconds...")
            time.sleep(10)
            response = requests.post(f"{self.homeserver}/_matrix/client/v3/login", 
                json = {
                    "type": "m.login.password",
                    "user": self.username,
                    "password": self.password
                })
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.exception(f"Non-HTTP-Fehler at Matrix login with password: {e}",)

        try:
            response_body = json.loads(response.text)
            self.access_token = response_body['access_token']
            self.device_id = response_body['device_id']
        except Exception:
            logger.exception("Error during parsing of request body after login of the matrix bot.")
            raise
        logger.info("Matrix Bot erfolgreich eingeloggt mit Passwort.")
        
    def token_whoami(self):
        response = requests.get(
        f'{self.homeserver}/_matrix/client/v3/account/whoami',
            headers={'Authorization': f'Bearer {self.access_token}'}
        )
        try:
            response.raise_for_status()
            logger.info("Matrix Bot erfolgreich mit API verbunden über AccessToken.")
            return response.status_code
        except requests.exceptions.HTTPError as e: 
            logger.exception("Fehler bei Whoami-API Anfrage des Matrix Bots mit AccessToken")
            return response.status_code
    
    def send_message(self, msg, room_id=None, thread_reply_to=None, html_msg=None):
        """
        Send Matrix Message to room, optionally reply in thread. 
        Will try to reauthenticate if response returns 401 status code.

        Arguments:
        - msg: String, Message. Markdown formatting possible
        Optional:
        - room_id: String, The room id where the message should be sent to. 
        - thread_reply_to: String, a Matrix event_id of the message to which a thread should be opened or continued. Event_ids of messages that are already in a thread will lead to a 400 HTTPError
        """
        if room_id is None:
            room_id = self.room_id
        random_uuid = str(uuid.uuid4())
        url = f'{self.homeserver}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{random_uuid}'
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
        payload = {
            "msgtype": "m.text",
            "body": msg}
        if thread_reply_to is not None:
            payload.update({
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": thread_reply_to
                    }
            })
        if html_msg is not None:
            payload.update({
                "format": "org.matrix.custom.html",
                "formatted_body": html_msg
             })
        response = requests.put(url, headers=headers, json=payload, timeout=15)
        logger.debug(f"Response Text: {response.text}")

        try:
            response.raise_for_status()
            logger.info("Matrix-Bot hat Nachricht gesendet.")
            return json.loads(response.text)['event_id']
        except requests.exceptions.HTTPError:
            logger.exception("Fehler beim Senden der Nachricht:")
            # in case token expired:
            if response.status_code == 401:
                self.password_login()
                response = requests.put(url, headers=headers, json=payload, timeout=15)
                logger.debug(f"Response Text: {response.text}")
                response.raise_for_status()
                logger.info("Matrix-Bot hat Nachricht gesendet.")
                return None
            elif response.status_code == 400:
                logger.error("Möglicherweise wurde im Argument thread_reply_to eine event_id angegeben, die zu einer Nachricht gehört die bereits in einem Thread ist - Matrix erlaubt keine genesteten Threads.")
                raise
            else: 
                raise
