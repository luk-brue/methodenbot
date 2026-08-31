import logging
import os
import csv
import quopri
import html
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import json
import hashlib
import io
from pathlib import Path
import stat
import uuid
from exchangelib import Configuration, Credentials, Account, DELEGATE, Message
from exchangelib.services import SubscribeToStreaming
from exchangelib.properties import NewMailEvent
from typing import Set, Dict, Optional
# own stuff:
from configuration import Configuration as LocalConfig
from stats_table_manager import StatsTableManager
from matrixbot import MAX_EVENT_CONTENT_BYTES, MatrixBot, matrix_message_content
from types import SimpleNamespace
from form_table_compat import parse_compatible
from ai_summary import post_ai_thread_reply

logger = logging.getLogger(__name__)
# The inherited INFO messages contain mailbox identifiers and form contents.
logger.setLevel(logging.WARNING)

MAX_PROCESSED_FILE_BYTES = 5_000_000


class ProcessedEmailStateError(RuntimeError):
    pass


class DeliveryNotConfirmed(RuntimeError):
    pass

def init_exchange_connection(config: LocalConfig) -> Account:
    """Initialisiert die Verbindung zu Exchange. Sollten hier mit der Konfigurations jemals
     Fehler auftreten, in account() autodiscover auf True setzen und
     config wegnehmen und dafür credentials hineintun. 
     Autodiscover ist nur aus um Ressourcen zu sparen"""
    try:
        credentials = Credentials(username=config.uk_nummer, password=config.email_password)
        exconfig = Configuration(credentials = credentials,
                                service_endpoint=config.ews_endpoint,
                                auth_type='NTLM',
                                max_connections=2)
        account = Account(primary_smtp_address=config.email_address,
                             config=exconfig,
                             autodiscover=False,
                             access_type=DELEGATE)
        logger.info("Exchange-Verbindung erfolgreich hergestellt")
        return account
    except Exception as e:
        logger.error("Fehler beim Verbinden mit Exchange: %s", type(e).__name__)
        raise

def load_processed_emails(filename: str) -> Set[str]:
    """Load the delivery ledger strictly; corruption must never mean "nothing sent"."""
    path = Path(filename)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return set()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as raw:
            metadata = os.fstat(raw.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size > MAX_PROCESSED_FILE_BYTES):
                raise ProcessedEmailStateError('unsafe_processed_email_file')
            payload = raw.read(MAX_PROCESSED_FILE_BYTES + 1)
        if len(payload) > MAX_PROCESSED_FILE_BYTES:
            raise ProcessedEmailStateError('processed_email_file_too_large')
        text = payload.decode('utf-8')
        reader = csv.DictReader(io.StringIO(text, newline=''))
        if reader.fieldnames != ['message_id']:
            raise ProcessedEmailStateError('processed_email_header_invalid')
        processed = set()
        for row in reader:
            if set(row) != {'message_id'}:
                raise ProcessedEmailStateError('processed_email_row_invalid')
            message_id = row.get('message_id')
            if (not isinstance(message_id, str) or not message_id or len(message_id) > 2000
                    or '\n' in message_id or '\r' in message_id):
                raise ProcessedEmailStateError('processed_email_id_invalid')
            processed.add(message_id)
        return processed
    except ProcessedEmailStateError:
        raise
    except (OSError, UnicodeError, csv.Error):
        raise ProcessedEmailStateError('processed_email_file_unreadable') from None


def _write_processed_emails(filename: str, message_ids: Set[str]):
    path = Path(filename)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        metadata = os.lstat(path.parent)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise ProcessedEmailStateError('unsafe_processed_email_directory')
    except OSError:
        raise ProcessedEmailStateError('processed_email_directory_unreadable') from None
    for message_id in message_ids:
        if (not isinstance(message_id, str) or not message_id or len(message_id) > 2000
                or '\n' in message_id or '\r' in message_id):
            raise ProcessedEmailStateError('processed_email_id_invalid')
    temporary = path.parent / ('.' + path.name + '.new.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['message_id'])
            writer.writeheader()
            writer.writerows({'message_id': value} for value in sorted(message_ids))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            existing = os.lstat(path)
            if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()
                    or stat.S_IMODE(existing.st_mode) != 0o600):
                raise ProcessedEmailStateError('unsafe_processed_email_file')
        except FileNotFoundError:
            pass
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

def save_processed_email(filename: str, message_id):
    processed = load_processed_emails(filename)
    if message_id in processed:
        return False
    processed.add(message_id)
    _write_processed_emails(filename, processed)
    return True

def clean_up_processed_file(filename: str, messages: list, processed_emails: Set[str]):
    """Routine to prevent the csv file to grow larger and larger: Restrict the possible IDs that the file may
    contain to those who are present in the INBOX. 
    :param filename: The name, defined in Configuration (processed_emails.csv)
    :param messages: A list of exchangelib Message objects, each having a message_id attribute
    :param processed_emails: The result of reading the csv file with load_processed_emails()
    """
    logger.info("Finde obsolete Message-IDs in csv-Datei...")
    message_ids = set()
    for message in messages:
        message_id = getattr(message, 'message_id', None)
        if not isinstance(message_id, str) or not message_id:
            # Internet Message-ID is optional in Exchange. Such a message cannot
            # match a ledger entry and must not block the entire startup cleanup.
            logger.warning('Inbox-Nachricht ohne stabile Message-ID beim Cleanup übersprungen.')
            continue
        message_ids.add(message_id)
    retained = processed_emails & message_ids
    _write_processed_emails(filename, retained)
    processed_emails.clear()
    processed_emails.update(retained)
    return processed_emails

def check_typo3_x_mailer(message: Message) -> Optional[str]:
    """Prüft, ob ein TYPO3 X-Mailer Header vorhanden ist und gibt den Wert zurück."""
    if not hasattr(message, 'headers') or not message.headers:
        logger.info("Keine Headers verfügbar")
        return None

    for header in message.headers:
        if hasattr(header, 'name') and hasattr(header, 'value') and header.name.lower() == 'x-mailer':
            logger.info(f"✅ X-Mailer gefunden: '{header.value}'")
            return header.value

    logger.info("Kein X-Mailer Header gefunden")
    return None

def is_typo3_contact_form(message: Message) -> bool:
    """Prüft, ob es sich um eine TYPO3-Kontaktformular-E-Mail handelt."""
    logger.info("🔍 Starte TYPO3-Kontaktformular-Prüfung...")

    # Prüfung auf TYPO3 X-Mailer Header
    x_mailer_value = check_typo3_x_mailer(message)
    if not x_mailer_value == 'TYPO3':
        return False

    # Prüfung, dass es sich NICHT um eine Antwort handelt
    subject = message.subject or ""
    reply_prefixes = ['AW:', 'RE:', 'Aw:', 'Re:', 'aw:', 're:']
    if any(subject.strip().startswith(prefix) for prefix in reply_prefixes):
        logger.info("Subject hat ein Antwort-Präfix")
        return False

    # Prüfung auf References oder In-Reply-To Header (deutet auf Antwort hin)
    if hasattr(message, 'headers') and message.headers:
        for header in message.headers:
            if hasattr(header, 'name') and hasattr(header, 'value'):
                header_name = header.name.lower()
                if header_name in ['references', 'in-reply-to']:
                    logger.info(f"{header.name} Header gefunden")
                    return False

    # Alle verfügbaren Body-Inhalte sammeln
    all_body_content = ""
    for body_attr in ['html_body', 'text_body', 'body']:
        if hasattr(message, body_attr):
            body = getattr(message, body_attr)
            if body:
                body_str = str(body)
                all_body_content += body_str + "\n"
                logger.info(f"✅ {body_attr} gefunden (Länge: {len(body_str)})")

    if not all_body_content:
        logger.info("Kein Body-Inhalt verfügbar")
        return False

    # Prüfung auf TYPO3-Kontaktformular-Kennzeichen
    if 'powermail_all' in all_body_content:
        logger.info("🎯 'powermail_all' gefunden - TYPO3 Kontaktformular erkannt!")
        return True

    typo3_indicators = [
        'Name, Vorname',
        'E-Mail-Adresse',
        'Studiengang',
        'Empra/',
        'Projekt',
        'Methodenberatung',
        'Captcha'
    ]
    found_indicators = sum(1 for indicator in typo3_indicators if indicator in all_body_content)
    logger.info(f"Gefundene Indikatoren: {found_indicators}")

    if found_indicators >= 3:
        logger.info("🎯 Genug Indikatoren gefunden - TYPO3 Kontaktformular erkannt!")
        return True

    logger.info("Finale Entscheidung: Nicht als TYPO3-Kontaktformular erkannt")
    return False

def _parse_email_data_legacy(item: Message) -> Dict[str, str]:
    """Parst die relevanten Daten aus der TYPO3 E-Mail und extrahiert die HTML-Tabelle mit BeautifulSoup."""
    logger = logging.getLogger(__name__ + '.legacy_parser')
    logger.disabled = True
    logger.info("🔍 Starte E-Mail-Parsing (HTML-Tabelle mit BeautifulSoup)...")

    # E-Mail-Datum extrahieren
    email_date = None
    if hasattr(item, 'datetime_received') and item.datetime_received:
        email_date = item.datetime_received.isoformat()
        logger.info(f"📅 E-Mail-Datum gefunden: {email_date}")
    elif hasattr(item, 'datetime_sent') and item.datetime_sent:
        email_date = item.datetime_sent.isoformat()
        logger.info(f"📅 E-Mail-Datum (gesendet): {email_date}")
    else:
        email_date = datetime.now().isoformat() + 'Z'
        logger.info(f"📅 Fallback E-Mail-Datum: {email_date}")

    # Absendername extrahieren
    sender_name = item.sender.name if item.sender and item.sender.name else "Unbekannt"
    logger.info(f"👤 Absendername: {sender_name}")

    # Body extrahieren und HTML-Tabelle mit BeautifulSoup extrahieren
    table_content = ""
    if hasattr(item, 'body'):
        body = item.body
        try:
            # Quoted-printable dekodieren
            body_bytes = body.encode('utf-8')  # In Bytes kodieren
            body = quopri.decodestring(body_bytes).decode('utf-8') # in UTF-8 kodieren
            logger.info(f"✅ Body gefunden (Länge: {len(body)})")

            # BeautifulSoup verwenden, um den HTML-Code zu parsen
            soup = BeautifulSoup(body, 'html.parser')

            # Tabelle mit der Klasse "powermail_all" finden
            table = soup.find('table', class_='powermail_all')
            if table:
                table_content = str(table)  # HTML-Code der Tabelle extrahieren
                logger.info("✅ HTML-Tabelle extrahiert")
                results = {}
                # Extract (1st and 2nd columns) 
                for row in table.find_all('tr'):  
                    columns = row.find_all('td')
                    feld = columns[0].text
                    inhalt = columns[1].text
                    results[feld] = inhalt
            else:
                logger.info("Keine HTML-Tabelle gefunden")
                logger.info("Keine passende HTML-Tabelle gefunden; Inhalt wird nicht protokolliert")
                results = None
        except Exception as e:
            logger.error(f"Fehler beim Verarbeiten des Body: {e}")
    else:
        logger.info("Kein Body Attribut verfügbar")
    
    try: # Optionales Feld
        betreuung = results['Name der Betreuungsperson '].strip()
    except KeyError:
        betreuung = "..."
    try: # optionales Feld.
        rskript = results['Bei R Fragen: R Skript (bitte Code einfach in das Feld kopieren)\n']
        rskript = '\n'.join(rskript.splitlines()) # remove \r\n (Windows type Line Endings) and replace with \n
    except KeyError:
        rskript = None
    try:
        dataname = results['Bei R Fragen: Datensatz° ']
        if "." in dataname: 
            dataname_components = dataname.split(".")
            if len(dataname_components) > 1:
                datensatz = f"✉️ Datei mit Endung .{dataname_components[1]}"
        else:
            datensatz = f"✉️ {dataname}"
    except KeyError:
        datensatz = None
    try: 
        pname = results['Präregistrierung ']
        if "." in pname:
            pname_components = pname.split(".")
            if len(pname_components) > 1:
                präregistrierung  = f"✉️ Datei mit Endung .{pname_components[1]}"
        else:
            präregistrierung = f"✉️ {pname}"


    except KeyError:
        präregistrierung = None

    beschreibung = results['Kurze Beschreibung des Projekts (Hypothesen, Ablauf, erhobene Variablen, Datenstruktur, geplante Analyse)\n']
    fragen = results['Konkreten Fragen + Eigene Lösungsansätze? ° ']

    

    parsed_data = {
        'sender_name': sender_name,  # Nur den Namen
        #'email_content': table_content,  # Nur die HTML-Tabelle
        'subject': item.subject,
        'sender': str(item.sender) if item.sender else 'Unbekannt',
        'received_date': email_date,
        'message_id': item.message_id,
        'fachsemester': results['Fachsemester '].strip(),
        'art': results['Art der Arbeit (Empra/ WHA/ Projekt/- oder Abschlussarbeit...)\n'].strip(),
        'betreuung': betreuung,
        'studiengang': results['Studiengang '].strip(),
        'fachgebiet': results['Fachgebiet, dem die Betreuungsperson angehört (z.B. "Entwicklungspsychologie")\n'].strip(),
        'beschreibung': beschreibung,
        'fragen': fragen,
        'rskript': rskript,
        'präregistrierung': präregistrierung,
        'datensatz': datensatz
    }

    #parsed_data.update(results) # append the html table parsed dict
    #logger.info(f"Parsed data content:{pformat(parsed_data)}")
    return parsed_data

def parse_email_data(item: Message) -> Dict[str, str]:
    """Validated TH/TD compatibility; original formatting remains unchanged."""
    api = SimpleNamespace(BeautifulSoup=BeautifulSoup, parse_email_data=_parse_email_data_legacy)
    data, _ = parse_compatible(api, item)
    return data

def matrix_post_message(matrixbot: MatrixBot, email_data: Dict[str, str], transaction_id=None) -> Optional[str]:
    """Postet eine Message in Rocket Chat"""
    logger.info("Erstelle Matrix-Nachricht...")
    # extrahiere Felder aus Dict
    fachsemester = f"{email_data['fachsemester']}"
    sender = f"{email_data['sender_name']}"  # Absendername
    art = f"{email_data['art']}"
    betreuung = f"{email_data['betreuung']}"
    studiengang = f"{email_data['studiengang']}"
    fachgebiet = f"{email_data['fachgebiet']}"
    #description = f"{pprint(email_data)}"
    # poste Nachricht
    try:
        payload = dict(
            msg=f"{sender}\n{art} bei {betreuung} ({fachgebiet})\n{studiengang}, {fachsemester}. FS.",
            html_msg=(f"<b>{html.escape(sender)}</b><br>{html.escape(art)} bei "
                      f"{html.escape(betreuung)} ({html.escape(fachgebiet)})<br>"
                      f"{html.escape(studiengang)}, {html.escape(fachsemester)}. FS."))
        if matrix_message_content(payload['msg'], payload['html_msg'])[1] > MAX_EVENT_CONTENT_BYTES:
            payload = {
                'msg': ('Neue Methodenberatungsanfrage. Die Kurzübersicht war für Matrix zu lang; '
                        'bitte Originalangaben im Postfach prüfen.'),
                'html_msg': ('<p><strong>Neue Methodenberatungsanfrage.</strong> Die Kurzübersicht '
                             'war für Matrix zu lang; bitte Originalangaben im Postfach prüfen.</p>'),
            }
        if transaction_id is not None:
            payload['transaction_id'] = transaction_id
        event_id = matrixbot.send_message(**payload)
        return event_id
    except Exception:
        logger.exception(f"❌ Unerwarteter Fehler bei der Matrix API-Anfrage:")
        return None

def matrix_post_detail_thread(matrixbot: MatrixBot, email_data: Dict[str, str], event_id: str,
                              config: LocalConfig, transaction_id=None) -> Optional[str]:
    beschreibung = email_data['beschreibung']
    fragen = email_data['fragen']
    rskript = email_data['rskript']
    datensatz = email_data['datensatz']
    prägregistrierung = email_data['präregistrierung']
    received = email_data.get('received_date')
    start_date_parsed = (str(received).replace("T", " ")[:16]
                         if received not in (None, '') else "Unbekannt")
    
    # get the pre-filled protocol url
    try:
        anfrage=""
        sender = str(email_data.get('sender_name') or 'Unbekannt')
        # Name abbreviation for privacy reasons. Discard the last name. However, some people don't fill 
        # the form appropriately - they put their whole name in the last name field. 
        if ", " in sender: # sign that form was appropriately filled
            sender = sender.split(", ", 1)[1] or 'Unbekannt' # This is to only keep the first name
        else: # if form was not appropriately filled, name will likely be in "first name whitespace last name" format
            sender = sender.split()[0] if sender.split() else 'Unbekannt'
        sender_name=requests.utils.quote(sender)
        fachsemester = requests.utils.quote(email_data['fachsemester'])
        art = requests.utils.quote(email_data['art'])
        if email_data['betreuung'] is not None:
            betreuung = requests.utils.quote(email_data['betreuung'])
        else:
            betreuung = requests.utils.quote('Keine Angabe')
        studiengang = requests.utils.quote(email_data['studiengang'])
        fachgebiet = requests.utils.quote(email_data['fachgebiet'])
        start_date = requests.utils.quote(start_date_parsed)
        message_id = requests.utils.quote(email_data['message_id'])
        url=f"{config.google_form_link}usp=pp_url&entry.1084327688={anfrage}&entry.1339219203={sender_name}&entry.1526227417={studiengang}&entry.760579146={betreuung}&entry.302223532={fachgebiet}&entry.1573426724={art}&entry.1469014536={fachsemester}&entry.701693485={message_id}&entry.1479923903={start_date}"
        html_protocol_url=f'<a href="{url}">{html.escape("Protokoll-Link vorausgefüllt (Google Forms)")}</a>'
        text_protocol_url=f"Protokoll-Link vorausgefüllt:\n\n{url}"
    except Exception as e:
        logger.warning("Protokoll-URL konnte nicht erstellt werden: %s", type(e).__name__)
        html_protocol_url=f'{html.escape("Protokoll-Link vorausgefüllt: Konnte nicht erstellt werden - Verwende ")}<a href="{config.google_form_link}">normalen Protokoll-Link</a>'
        text_protocol_url=f"Konnte nicht erstellt werden, verwende normalen Protokoll-Link: {config.google_form_link}"

    try:
        logger.info("Poste Details in bestaetigten Thread")
        # prepare the raw text for clients who don't support html rendering
        detailtext=f"Beschreibung:\n{beschreibung}\n\nFragen:\n{fragen}\n\nR-Skript:\n```r\n{rskript}\n```\n\nDatensatz:\n{datensatz}\n\nPräregistrierung:\n{prägregistrierung}\n\nProtokoll-Link vorausgefüllt:\n{text_protocol_url}\n\nEingangsdatum:\n{start_date_parsed}"
        msg_len = len(detailtext)
        # matrix has a message length limit of 60 something kilobytes with is about 20k - 25k characters in utf-8
        if msg_len <= 20000:
            croppedtext = detailtext
        else:
            croppedtext = detailtext[:20000] + f"\n\n[...] {msg_len - 20000} weitere Zeichen"

        # prepare the html text for clients like element & co who render html
        # equalize newline characters from different os --> all newlines become <br> (\r\n | \n) -> <br>
        # escape html < > & signs
        beschreibung=html.escape(beschreibung)
        beschreibung='<br>'.join(beschreibung.splitlines())
        fragen=html.escape(fragen)
        fragen='<br>'.join(fragen.splitlines())

        if rskript is not None:
            if len(rskript) < 19500:
                script_escaped = html.escape(rskript)
            else:
                script_escaped = html.escape(rskript)[:19500] + f"<br><br><b>[...] {len(rskript) - 19500} weitere Zeichen</b>"
            rskript = f'<b>R-Skript:</b><br><pre><code class="language-r">{script_escaped}</code></pre>'
        else:
            rskript = f'<b>R-Skript: </b><br>{html.escape("Nicht angegeben")}'
        
        if datensatz is not None:
            datensatz = html.escape(datensatz)
        else:
            datensatz = html.escape("Nicht angegeben")
        
        if prägregistrierung is not None:
            prägregistrierung = html.escape(prägregistrierung)
        else: 
            prägregistrierung = html.escape("Nicht angegeben")
        
        start_date_parsed=html.escape(start_date_parsed)

        html_text = f'<b>Beschreibung:</b><br>{beschreibung}<br><br><b>Fragen:</b><br>{fragen}<br><br>{rskript}<br><br><b>Datensatz:</b><br>{datensatz}<br><br><b>Präregistrierung:</b><br>{prägregistrierung}<br><br><b>Eingangsdatum:</b><br>{start_date_parsed}<br><br><b>{html_protocol_url}</b>'
        

        # send the message
        send_options = {'thread_reply_to': event_id}
        if transaction_id is not None:
            send_options['transaction_id'] = transaction_id
        if matrix_message_content(croppedtext, html_text, event_id)[1] <= MAX_EVENT_CONTENT_BYTES:
            # send normally
            return matrixbot.send_message(msg=croppedtext, html_msg=html_text, **send_options)
        else:
            return matrixbot.send_message(
                msg="Die Details der Anfrage waren zu lang, um sie über Matrix zu senden. Bitte im Postfach nachschauen.",
                **send_options)


    except Exception as e:
        logger.error("Unerwarteter Fehler beim Erstellen des Matrix-Threads: %s", type(e).__name__)
        return None

def process_email(config: LocalConfig, account: Account, message: Message, processed_emails: Set[str], matrixbot: MatrixBot, stats: StatsTableManager) -> bool:
    """Verarbeitet eine einzelne E-Mail."""
    message_id = getattr(message, 'message_id', None)
    if not isinstance(message_id, str) or not message_id or len(message_id) > 2000:
        raise ValueError('E-Mail ohne stabile Message-ID')
    logger.info("Verarbeite E-Mail")

    if message_id in processed_emails:
        logger.info(f"⏭️ Überspringe - bereits zu Matrix gesendet")
        return False

    logger.info(f"Führe TYPO3-Prüfung durch...")

    if not is_typo3_contact_form(message):
        logger.info(f"Nicht als TYPO3-Kontaktformular erkannt")
        return False

    logger.info("TYPO3-Kontaktformular gefunden")

    email_data = parse_email_data(message)
    # try except weil Matrix Session Tokens ablaufen
    # unbekannt wie lange in unserer Installation gültig.

    stable = hashlib.sha256(message_id.encode('utf-8')).hexdigest()[:32]
    event_id = matrix_post_message(matrixbot=matrixbot, email_data=email_data,
                                   transaction_id='mail-' + stable + '-root')

    if not isinstance(event_id, str) or not event_id.startswith('$') or len(event_id) < 2:
        raise DeliveryNotConfirmed("Originalnachricht nicht bestätigt; kein Thread-Versand")

    # AI overview and original details are sibling replies under the same root.
    # AI/network/send failures are absorbed; original details still follow.
    state = getattr(config, 'control_state', None)
    ai_enabled = (state.snapshot()['ai_enabled'] if state is not None
                  else bool(getattr(getattr(config, 'ai', None), 'enabled', False)))
    ai_service = getattr(config, 'ai_service', None)
    if ai_service is not None:
        ai_status = ai_service.post_thread_reply(
            matrixbot, email_data, event_id, enabled=ai_enabled,
            transaction_id='mail-' + stable + '-ai')
    elif ai_enabled:
        ai_status = post_ai_thread_reply(matrixbot, email_data, config, thread_root=event_id)
    else:
        ai_status = 'disabled'
    if ai_enabled and ai_status not in ('summary_ready', 'unavailable'):
        raise DeliveryNotConfirmed('KI-Nachricht nicht bestätigt; Anfrage bleibt unverarbeitet')

    detail_id = matrix_post_detail_thread(matrixbot=matrixbot, email_data=email_data, event_id=event_id,
                                          config=config, transaction_id='mail-' + stable + '-details')
    if not isinstance(detail_id, str) or not detail_id.startswith('$'):
        raise DeliveryNotConfirmed("Detailnachricht nicht bestätigt; Anfrage bleibt unverarbeitet")
    # collect keys and data to be saved in stats.csv
    allowed_keys = set(stats.HEADERS)
    mail_record = {k: v for k, v in email_data.items() if k in allowed_keys}
    mail_record.update({'tmid': event_id}) # add the thread message id
    stats.append_record(mail_record) # save data to stats.csv
    save_processed_email(filename=config.processed_file, message_id=message_id)
    processed_emails.add(message_id)
    return True

def process_many_emails(messages: list, config: LocalConfig, account: Account, processed_emails: Set[str],
                    matrixbot: MatrixBot, stats: StatsTableManager):
    """Verarbeitet viele E-Mails."""
    try:
        logger.info(f"Verarbeite {len(messages)} E-Mails...")
        delivery_error = None
        for message in messages:
            try:
                process_email(config, account, message, processed_emails, matrixbot, stats)
            except (ProcessedEmailStateError, OSError):
                raise
            except DeliveryNotConfirmed as exc:
                # Continue the bounded startup batch so one bad request cannot
                # starve newer ones, then make systemd restart and retry it.
                logger.error("Matrix-Zustellung einer E-Mail nicht bestätigt.")
                delivery_error = delivery_error or exc
            except Exception as e:
                logger.error("Fehler beim Verarbeiten einer E-Mail: %s", type(e).__name__)
        if delivery_error is not None:
            raise delivery_error
        logger.info(f"Verarbeitung der Mails abgeschlossen.")
    except Exception as e:
        logger.error("Fehler beim Verarbeiten der E-Mails: %s", type(e).__name__)
        raise

def maintain_notification_streaming(account: Account,
                                    config: LocalConfig,
                                    processed_emails: Set[str],
                                    stats: StatsTableManager,
                                    matrixbot: MatrixBot,
                                    timeout_minutes: int=29,
                                    only_fields = ['headers', 'subject', 'sender', 'datetime_received', 'datetime_sent', 'body', 'message_id']):
    """:params: inbox = Account.inbox
    :params: timeout_minutes = Positive integer between 1 and 29 (internally, 1 minute is added)
    """
    inbox = account.inbox
    while True:  
        try:
            logger.info("🛜 Starte Notification Streaming Subscription.")  
            with inbox.streaming_subscription() as subscription_id: 
                logger.info("📭 Warte auf neue Mails.")
                for notification in inbox.get_streaming_events(subscription_id, connection_timeout=timeout_minutes):  
                    for event in notification.events:  
                        if isinstance(event, NewMailEvent):  
                            # Get the specific new mail item by ID
                            logger.info("📬 Neue Mail!") 
                            # time.sleep(1) # wait for the server to completely put the mail in INBOX
                            # Fetch only this specific item with only the fields we need  
                            items = list(account.fetch([event.item_id], only_fields=only_fields))
                            item = items[0]
                            #logger.info(f"Inhalt von items:\n{pformat(items)}")
                            logger.info("Starte Verarbeitung der neuen Mail") 
                            process_email(config=config, 
                                            account=account, 
                                            message=item,
                                            processed_emails=processed_emails,
                                            matrixbot=matrixbot,
                                            stats=stats)
                            logger.info("📭 Warte auf weitere neue Mails.")
        except (ConnectionError, TimeoutError) as e:  
            logger.error(f"Verbindungsfehler oder Timeout während des Empfangens der Notifications: Verbindung wird in 5s wieder hergestellt: {e}")  
            time.sleep(5)  # Brief pause before reconnecting  
            continue
