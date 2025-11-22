"""
Für die Jahresstatistik:
Hole alle Kontaktformulare aus dem Mailpostfach
Ermittele die Anzahl der gesendeten Mails
Ermittele die Anzahl der eingegangenen Kontaktformulare
"""
import logging
from exchangelib import Account, Message
import datetime
# own stuff
from exchangemail import init_exchange_connection, is_typo3_contact_form, check_typo3_x_mailer
from configuration import Configuration as LocalConfig

logger = logging.getLogger(__name__)


config = LocalConfig()
a = init_exchange_connection(config)
# user input
# y = True
# while y == True:
#   jahr = input("Bitte Jahr eingeben, für welches Statistiken erstellt werden sollen.\nBeispiel: 2025\n:")
#   try:
#     jahr = int(jahr)
#     y = False
#   except:
#     print("Bitte eine Jahreszahl eingeben. Probier es nochmal")
jahr=2025
start = datetime.datetime(jahr, 1, 1, tzinfo=a.default_timezone)
end = datetime.datetime(jahr+1, 1, 1, tzinfo=a.default_timezone)
# Filter by a date range
sent_messages = a.sent.filter(datetime_received__range=(start, end))
print(f"Hole Anzahl der {jahr} gesendeten Nachrichten")
# print(f"Betreff der in {jahr} gesendeten Nachrichten:")
# for message in sent_messages:
#     print(message.subject)
print(f"Anzahl: {sent_messages.count()}")
print(f"Hole Anzahl der {jahr} empfangenen Nachrichten")
# print(a.root.tree())
# received_messages = a..filter(datetime_received__range=(start, end)).order_by('-datetime_received')
kor=a.root // "Oberste Ebene des Informationsspeichers" // "Korrespondenz"
# walk all child folders
messages = kor.walk().filter(datetime_received__range=(start, end))
received_n = messages.count()
print(f"Anzahl: {received_n}")
print(f"Zähle davon die Kontaktformular-Anfragen... \n(zeitintensiv, da jede Mail abgerufen und geprüft wird)")
# counter for contact forms
x=0
for m in messages:
    #print(m.subject)
    if check_typo3_x_mailer(m) is not None:
        #print("✅ X-Mailer")
        if is_typo3_contact_form(m):
            #print("✅ Kontaktformular-Anfrage")
            x += 1
            #print(x)
print(f"{x} von {received_n} eingegangenen Mails in {jahr} waren initiale Kontaktformular-Anfragen.") 