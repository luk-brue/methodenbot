# methodenbot
Bridge from Exchange mail inbox to Matrix channel. 

### Beschreibung

Kann ein Exchange-Gruppen-Email-Postfach auf neue Nachrichten in der INBOX abhören. Neue Nachrichten werden gefiltert, je nachdem ob es sich um ein von TYPO3 versendetes Kontaktformular handelt oder nicht. Wenn es ein Kontaktformular ist, wird der relevante Inhalt ausgelesen. Über Matrix wird dann eine formatierte Nachricht mit dem Inhalt des Formulars versendet, in einen vorgegebenen Raum. 
Dabei werden zunächst die wichtigsten Informationen zusammengefasst in einer kurzen Nachricht und die Details werden gepostet in eine Thread-Antwort darauf. Schließlich wird ein Google-Forms Link erzeugt, der die Informationen aus dem Kontaktformular vorausgefüllt hat. Der wird dann auch in den Thread gepostet. 

### Setup

**Voraussetzungen**

- Python3, pip
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
- Installiere Dependencies aus `requirements.txt`
- `main.py` muss ausgeführt werden, um die Email-Matrix-Brücke laufen zu lassen. Es bietet sich an, auf Linux einen `systemd` Service einzurichten, der sich selbst neu startet bei Fehlern und nach dem Bootvorgang gestartet wird. 

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
### Details about email filtering and processing

- The script only checks for mails in the INBOX folder (Posteingang). 
- It does not discern between read and unread mails (it is a group folder, so another person could have read the mail already)
- Typo3 contact form emails are discerned from other mail traffic, using a few criteria such as
    - X-Mailer Header used by Typo3
    - All sorts of replies containing the orginal contact form are filtered out
    - the mail body is scanned and expected to contain a few field names from the contact form
    - The email message ID (unique across all emails) is used to recognize emails that have already been sent to RocketChat as messages. 
        - To achieve this, the file `processed_emails.csv` is read if it exists. Otherwise it will be created later by the script. 
- If an email is identified as Typo3 contact form, it is parsed. 
    - The mail contains a HTML Table of the filled out Typo3 contact form. 
    - This table is parsed into a python dict
    - Some additional details, such as email subject, sender and date are parsed from other sources
- A message is posted to the specified Matrix room, containing a few key fields from the dict. 
    - The message is formatted using markdown. 
- A second message with details is posted as a thread under the first message, in order to clean up the channel the remaining fields are posted as a thread message. 
    - To achieve this, the event ID of the first message is retained and given as an argument to the thread posting function.
- A third message with the pre-filled google-forms link is sent to the thread.
- If posting was successful, a record of the processed email is created. The email is identified by its unique email message ID. This record is written to a `.csv` file named `processed_emails.csv`, which will be created in the same directory as the script. 

### Flow

1. Startup: Connect to Accounts
1. Read CSV file of processed emails and create one if it does not exist.
1. Fetch 100 newest emails from INBOX
1. Clean up the CSV file - remove email IDs that are no longer in INBOX.
1. Process all emails from INBOX, unless their ID is already in the CSV file
1. Start a 'streaming subscription' to get notifications for new emails.
1. Listen and wait for notifications
1. In case an email arrives, process it and listen for more notifications.
1. Renew the subscription after 30 minutes (maximum allowed connection time by EWS). 

### Efficiency concerns

The script does try to minimize resource usage. For example, by using the streaming notification system, only new emails are fetched. `exchangelib` offers `item_sync`, which does work fine but not for this use case, as we need the `headers` field to stay intact - and as it turns out, it is silently removed by the sync functionality. So this implementation relies on classic `fetch` instead of syncing, which was made more efficient by:

- only fetching the new email
- only fetching select fields which are necessary, and not getting attachments for example. 

### Known Shortcomings

- Credentials are stored in clear text in a config file and as environment variables. Which is ok, but not totally secure. 
- If the contact form field names are updated, the script has to be updated as well. Otherwise it breaks and the contact form is not transported correctly. If you were eager, you could implement a fallback for this, which automatically posts the whole email to RocketChat and does not discern between field names. 

### Matrix Bot Details

In `matrixbot.py` wird eine Klasse erstellt, die sich per Username und Passwort bei der Matrix Client-Server API anmelden kann und das dabei erhaltene AccessToken lokal speichert. Diese Implementierung ist rudimentär und greift weder auf `async` Prinzipien noch auf kryptografische Verschlüsselung des Caches für AccessTokens zurück. Das erlauben wir uns hier, da der Server nicht von außen zugänglich ist. Eine Implementierung mittels existierender Clients wie `matrix-nio` oder darauf aufbauenden Client-Bibliotheken war zu kompliziert / mit zu viel Overhead verbunden dafür, einzig nur den Send-Message Endpoint der API nutzen zu wollen. Nichtsdestotrotz, besser geht es natürlich immer. 



