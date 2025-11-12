# methodenbot
Matrix bot. Bridge from Exchange mail inbox to Matrix channel. 

### Setup

- In einer Datei namens `.env` müssen Umgebungsvariablen definiert werden, die zur Konfiguration benötigt werden. Die Datei sollte im selben Ordner liegen wie die Skripte. Zum Aufbau der Datei siehe unten. Bitte Berechtigungen und Zugriff auf die Datei einschränken.
- `main.py` muss ausgeführt werden, um die Email-Matrix-Brücke laufen zu lassen.

### .env

Beispiel einer .env-Datei:
```
MATRIX_SERVER="https://..." # Home-Server des Bots
MATRIX_USER="uXXXX"         # Bot user
MATRIX_PASSWORD="..."
MATRIX_ROOM_ID = "!....:...." # ID des Raums, wohin der Bot Nachrichten senden soll.
EMAIL_ADDRESS = "....@....de"  #  Exchange-E-Mail-Adresse
EMAIL_PASSWORD = "..."              # Uni-Account Passwort
UK_NUMMER = "uk123456"
EWS_ENDPOINT = 'https://<mailserver>/EWS/Exchange.asmx'
BOT_COMMAND_PREFIX = "!" 
GOOGLE_FORM_LINK = "https://docs.google.com/forms/d/e/1FAIpQLSeXo2YhgmnH7VOIlu0sIIkDtsELVTVIlBhA_9olByY1UrhRwQ/viewform?" # Teilen-Link, bis viewform?
```

