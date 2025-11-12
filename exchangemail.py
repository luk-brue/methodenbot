import logging
import os
import csv
import quopri
import re
import html
import time
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
from pprint import pprint, pformat
import json
import traceback
import pandas as pd
from exchangelib import Configuration, Credentials, Account, DELEGATE, Message
from exchangelib.services import SubscribeToStreaming
from exchangelib.properties import NewMailEvent
from typing import Set, Dict, Optional, List
# own stuff:
from configuration import Configuration as LocalConfig
from stats_table_manager import glimpse, StatsTableManager
from matrixbot import MatrixBot

logger = logging.getLogger(__name__)

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
        logger.error(f"Fehler beim Verbinden mit Exchange: {e}")
        raise

def load_processed_emails(filename: str) -> Set[str]:
    """Lädt bereits verarbeitete E-Mail-IDs aus der CSV-Datei."""
    processed = set()
    if os.path.exists(filename):
        try:
            with open(filename, 'r', encoding='utf-8') as file:
                reader = csv.DictReader(file)
                for row in reader:
                    processed.add(row['message_id'])
                logger.info(f"{len(processed)} Einträge in processed_emails.csv enthalten")
        except Exception as e:
            logger.error(f"Fehler beim Laden der processed_emails.csv: {e}")
    return processed

def save_processed_email(filename: str, message_id):
    file_exists = os.path.exists(filename)
    try:
        with open(filename, 'a', newline='', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=['message_id'])
            # Create file + header if it does not exist
            if not file_exists:
                writer.writeheader()
                logger.info("Erstelle CSV-Datei.")
            # Check if the file is empty and create header if necessary
            if file.tell() == 0:
                writer.writeheader()
                logger.info("Leere CSV-Datei - erstelle Header")
            writer.writerow({'message_id': message_id})

        logger.info("Eintrag in processed_emails.csv gesetzt.")
    except Exception as e:
        logger.error(f"Fehler beim Speichern in processed_emails.csv: {e}")

def clean_up_processed_file(filename: str, messages: list, processed_emails: Set[str]):
    """Routine to prevent the csv file to grow larger and larger: Restrict the possible IDs that the file may
    contain to those who are present in the INBOX. 
    :param filename: The name, defined in Configuration (processed_emails.csv)
    :param messages: A list of exchangelib Message objects, each having a message_id attribute
    :param processed_emails: The result of reading the csv file with load_processed_emails()
    """
    logger.info("Finde obsolete Message-IDs in csv-Datei...")
    message_ids = set()
    file_exists = os.path.exists(filename)
    for message in messages:
        try:
            message_ids.add(message.message_id)
        except Exception as e:
            logger.error(e)
    #logger.info(pformat(message_ids))
    obsolete_ids = processed_emails - message_ids # calculate set difference
    processed_emails.difference_update(obsolete_ids) # update the set to contain in-obsolete ids
    try:
        with open(filename, 'w', encoding='utf-8') as file:
            writer = csv.DictWriter(file, fieldnames=['message_id'])
            if not file_exists:
                logger.info("CSV-Datei erstellt.")
                writer.writeheader()
            # Check if the file is empty and create header if necessary
            if file.tell() == 0:
                writer.writeheader()
                logger.info("Leere CSV-Datei - erstelle Header")
            writer.writerows([{'message_id': mid} for mid in processed_emails])
        logger.info(f"{len(obsolete_ids)} obsolete Message-IDs entfernt aus CSV-Datei.")
        return processed_emails
    except Exception as e:
        logger.error(f"Fehler beim Speichern in CSV-Datei: {e}")
        return None

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
        logger.info(f"Subject hat Antwort-Präfix: '{subject}'")
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

def parse_email_data(item: Message) -> Dict[str, str]:
    """Parst die relevanten Daten aus der TYPO3 E-Mail und extrahiert die HTML-Tabelle mit BeautifulSoup."""
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
                logger.info(f"Gesamter Body-Inhalt: {body}")  # Protokolliere den gesamten Body
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
        'rskript': rskript
    }

    #parsed_data.update(results) # append the html table parsed dict
    #logger.info(f"Parsed data content:{pformat(parsed_data)}")
    return parsed_data

def matrix_post_message(matrixbot: MatrixBot, email_data: Dict[str, str]) -> Optional[str]:
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
    start_date = f"{email_data['received_date']}"
    # poste Nachricht
    try:
        event_id = matrixbot.send_message(
            msg = f"{sender}\n{art} bei {betreuung} ({fachgebiet})\n{studiengang}, {fachsemester}. FS.",
            html_msg=f"<b>{sender}</b><br>{art} bei {betreuung} ({fachgebiet})<br>{studiengang}, {fachsemester}. FS."
            )
        return event_id
    except Exception:
        logger.exception(f"❌ Unerwarteter Fehler bei der Matrix API-Anfrage:")
        return None

def matrix_post_detail_thread(matrixbot: MatrixBot, email_data: Dict[str, str], event_id: str) -> Optional[str]:
    beschreibung = email_data['beschreibung']
    fragen = email_data['fragen']
    rskript = email_data['rskript']
    try:
        logger.info(f"🚀 Poste Details in Thread unter Nachricht mit ID {event_id}")
        detailtext=f"Beschreibung:\n{beschreibung}\n\nFragen:\n{fragen}\n\nR-Skript:\n```r\n{rskript}\n```"
        msg_len = len(detailtext)
        if msg_len <= 5000:
            croppedtext = detailtext
        elif rskript == None: # Don't add closing backticks if Rscript is not there
            croppedtext = detailtext[:4975] + f"\n[...] {msg_len - 4994} weitere Zeichen"
        else: # Do add closing backticks if Rscript exists
            croppedtext = detailtext[:4965] + f"\n[...] {msg_len - 4994} weitere Zeichen\n```"
        
        # equalize newline characters (\r\n | \n) -> <br>
        # escape html < > & signs
        beschreibung=html.escape(beschreibung)
        beschreibung='<br>'.join(beschreibung.splitlines())
        fragen=html.escape(fragen)
        fragen='<br>'.join(fragen.splitlines())

        html_text = f'<h3>Beschreibung:</h3>{beschreibung}<br><h3>Fragen:</h3>{fragen}<br><h3>R-Skript:</h3><pre><code class="language-r">{html.escape(rskript)}</code></pre>'
        logger.info(f"Beschreibugn: {beschreibung}")
        matrixbot.send_message(msg=croppedtext, thread_reply_to=event_id, html_msg=html_text)

    except Exception as e:
        logger.error(f"❌ Unerwarteter Fehler beim Erstellen des Matrix Threads: {e}")
        logger.error(traceback.format_exc())
        return None

def process_email(config: LocalConfig, account: Account, message: Message, processed_emails: Set[str], matrixbot: MatrixBot, stats: StatsTableManager) -> bool:
    """Verarbeitet eine einzelne E-Mail."""
    try:
        message_id = message.message_id
        subject = message.subject or "Kein Subject"
        logger.info(f"\n=== Verarbeite E-Mail ===")
        logger.info(f"Message ID: {message_id}")
        logger.info(f"Subject: {subject}")
    except Exception as e:
        logger.error(f"Fehler beim Extrahieren der Message ID oder Subject Fields: {e}")

    if message_id in processed_emails:
        logger.info(f"⏭️ Überspringe - bereits zu Matrix gesendet")
        return False

    logger.info(f"Führe TYPO3-Prüfung durch...")

    if not is_typo3_contact_form(message):
        logger.info(f"Nicht als TYPO3-Kontaktformular erkannt")
        return False

    logger.info(f"🎯 TYPO3-Kontaktformular gefunden: {subject}")

    email_data = parse_email_data(message)
    # try except weil Matrix Session Tokens ablaufen
    # unbekannt wie lange in unserer Installation gültig.

    event_id = matrix_post_message(matrixbot=matrixbot, email_data=email_data)

    if event_id is None:
        logger.error("Fehler - Thread-ID ist None")
        raise

    matrix_post_detail_thread(matrixbot = matrixbot, email_data = email_data, event_id = event_id)

    save_processed_email(filename=config.processed_file, message_id=message_id)
    # collect keys and data to be saved in stats.csv
    try:
        allowed_keys = set(stats.HEADERS)
        mail_record = {k: v for k, v in email_data.items() if k in allowed_keys}
        mail_record.update({'tmid': event_id}) # add the thread message id
    except Exception as e:
        logger.error("Fehler beim Sammeln der Email-Daten für die Statistik", e)
    stats.append_record(mail_record) # save data to stats.csv
    return True

def process_many_emails(messages: list, config: LocalConfig, account: Account, processed_emails: Set[str],
                    matrixbot: MatrixBot, stats: StatsTableManager):
    """Verarbeitet viele E-Mails."""
    try:
        logger.info(f"Verarbeite {len(messages)} E-Mails...")
        for message in messages:
            try:
                process_email(config, account, message, processed_emails, matrixbot, stats)
            except Exception as e:
                logger.error(f"Fehler beim Verarbeiten der E-Mail {message.sender_name}: {e}")
                logger.error(traceback.format_exc())
        logger.info(f"Verarbeitung der Mails abgeschlossen.")
    except Exception as e:
        logger.error(f"Fehler beim Verarbeiten der E-Mails: {e}")
        import traceback
        logger.error(traceback.format_exc())
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