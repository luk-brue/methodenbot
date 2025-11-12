# methodenbot
Bridge from Exchange mail inbox to Matrix channel. 

### Beschreibung

Kann ein Exchange-Gruppen-Email-Postfach auf neue Nachrichten in der INBOX abhören. Neue Nachrichten werden gefiltert, je nachdem ob es sich um ein von TYPO3 versendetes Kontaktformular handelt oder nicht. Wenn es ein Kontaktformular ist, wird der relevante Inhalt ausgelesen. Über Matrix wird dann eine formatierte Nachricht mit dem Inhalt des Formulars versendet, in einen vorgegebenen Raum. 
Dabei werden zunächst die wichtigsten Informationen zusammengefasst in einer kurzen Nachricht und die Details werden gepostet in eine Thread-Antwort darauf. Schließlich wird ein Google-Forms Link erzeugt, der die Informationen aus dem Kontaktformular vorausgefüllt hat. Der wird dann auch in den Thread gepostet. 

### Setup

**Voraussetzungen**

- optimal: dauerhaft laufender Rechner, der das Skript kontinuierlich ausführt. Aber auch nach Ausfällen bei Neustarts werden die 100 neuesten Emails im INBOX-Ordner geholt.
- Der Rechner muss nur Anfragen ins Internet senden können, selbst aber keine Anfragen entgegennehmen. Optimal für strenge Firewalls wie z.B. in der Uni. 
- Es wird ein Matrix Account benötigt mit Passwort und Username
- Adresse des Matrix-Homeservers
- Raum-ID eines Matrix Raums, in welchen die Nachrichten gepostet werden.
- Exchange-Email-Adresse - muss kein Gruppenpostfach sein, kann aber eins sein.
- Zugangsdaten für diese Exchange-Mail-Adresse, bei Gruppenpostfach die normalen Exchange Zugangsdaten einer Person die für das Postfach freigeschaltet ist. 
- Serveradresse des Mailservers
- Link zu einem passenden Google-Formular

**Vorgehensweise**

- Klone dieses Repository.
- Erstelle `.env` Datei darin. Enthält Konfigurationsvariablen, die als Umgebungsvariablen ausgelesen werden. Zum Aufbau der Datei siehe unten. Bitte Berechtigungen und Zugriff auf die Datei einschränken.
- Erstelle ein virtuelles Environment für Python (`venv` oder `conda`)
- Installiere Dependencies
- `main.py` muss ausgeführt werden, um die Email-Matrix-Brücke laufen zu lassen. Es bietet sich an, auf Linux einen `systemd` Service einzurichten, der sich selbst neu startet bei Fehlern und beim Bootvorgang gestartet wird. 

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
### Funktionsweise


