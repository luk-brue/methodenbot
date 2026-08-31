# Übergabe: finaler Methodenbot

Stand: 31.08.2026

## Kurzfassung

Diese Fassung enthält sämtliche bisherigen Parser-/Matrix-Fixes, die
GWDG-KI-Zusammenfassung und vier persönliche Steuerbefehle für ausdrücklich
konfigurierte Kontrollpersonen. Der
Produktionsdienst soll aus einem unveränderlichen Release unter
`/srv/methodenbot-final/current` laufen. Zugangsdaten und Laufzeitdaten liegen
außerhalb des Releases.

Die fachliche KI-Ausgabe besteht aus:

- einer knappen Zusammenfassung des eigentlichen Anliegens;
- Analyseart (quantitativ, qualitativ oder Mixed Methods);
- Analyseschritt;
- genannter Software;
- betroffenem statistischen Modell bzw. Verfahren.

Defaultmodell ist `qwen3-30b-a3b-instruct-2507`. Bis zu drei brauchbare Entwürfe
werden gesammelt, mit höchstens zehn seriellen Aufrufen insgesamt. Der gemeinsame
Pacer hält mindestens 22 Sekunden Abstand und beachtet strengere Rate-Limit-Header.
Bei einem Fehler bleiben die Originaldetails erhalten.

## Persönliche Steuerung

Die primäre und jede in `MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON` ergänzte Person
darf in ihrer jeweils eigenen, fest konfigurierten privaten Zweierunterhaltung
exakt folgende Nachrichten senden:

- `KI an`
- `KI aus`
- `Test`
- `Test 2`

`Test` kopiert die letzten drei echten Anfragen ausschließlich in die PN der
auslösenden Person. `Test 2` kopiert
die neueste Anfrage mit dem sichtbaren Beginn `Techniktest` in den produktiven
Zielraum. Beide Pfade sind read-only gegenüber Exchange, Processed-CSV und
Statistik. `Test 2` ist trotzdem ein realer Matrix-Seiteneffekt.

Beide Testbefehle werden sofort im auslösenden Chat bestätigt, bevor Exchange und
gegebenenfalls die KI arbeiten. Eigene Poller halten alle Kontrollräume während
langer Tests ansprechbar; ein gemeinsamer Worker führt sämtliche Befehle seriell
aus. Der KI-Schalter ist global. Deshalb darf jede konfigurierte Person den
Produktionsmodus für alle ändern; ebenso darf jede mit `Test 2` eine echte,
nicht automatisch widerrufbare Nachricht im Zielraum auslösen.

Alte Chatnachrichten werden beim ersten Start nicht ausgeführt. Kontrollbefehle
werden mit Cursor, Journal, stabilen Transaktions-IDs, persistenter Matrix-Sitzung
und Event-Readback gegen Crash-Duplikate geschützt. Die KI-Einstellung ist global,
persistiert und beim ersten Start aus. Nach höchstens fünf vorübergehenden
Zustellfehlern wird ein einzelner Befehl sicher beendet und eine private Warnung
versucht; eine mögliche Teilzustellung wird nicht blind wiederholt und spätere
Befehle bleiben ausführbar.

## Wichtige Dateien

- `main.py` – produktiver Einstieg, getrennte Raumpoller und serieller Worker
- `matrix_commands.py` – Autorisierung, `/sync`, Befehlsausführung und Readback
- `control_state.py` – atomarer 0600-Zustand und Prozess-Lock
- `manual_delivery.py` – read-only Auswahl und Test-Rendering
- `matrixbot.py` – Matrix-Transport, stabile Sitzung, 401/429 und Transaktions-IDs
- `ai_service.py` – ausschließlich lokales Gateway und gemeinsamer Pacer
- `ai_summary.py`, `summary_selection.py` – Minimierung, Auswahl und Darstellung
- `form_table_compat.py` – kompatibler TH/TD- und TD/TD-Parser
- `exchangemail.py` – normaler produktiver Mailfluss

## Geheimnisse und Berechtigungen

Im Release befinden sich keine Geheimnisse. `runtime.env` liegt geschützt unter
`/etc/methodenbot/`. Der Methodenbot bekommt per systemd `LoadCredential` nur den
lokalen Token des Gateways auf `127.0.0.1:18765`. Der echte GWDG-Key verbleibt
ausschließlich im Gateway.

Kontrollzustand, CSVs und Matrix-Sitzung liegen unter `/var/lib/methodenbot`
mit 0700/0600. Das Journal kann während eines begonnenen Tests vorübergehend
personenbezogene Originaltexte enthalten und wird nach bestätigter Zustellung
bereinigt. Eine vorhandene beschädigte oder unsichere `processed_emails.csv`
stoppt den Dienst fail-closed; sie wird niemals als leere Historie behandelt.

## Prüfen und betreiben

```sh
sudo systemctl status methodenbot --no-pager
sudo journalctl -u methodenbot -n 100 --no-pager
sudo -u methodenbot test -r /var/lib/methodenbot/processed_emails.csv
sudo -u methodenbot test -r /var/lib/methodenbot/stats.csv
```

Keine Testhelfer aus alten `/var/tmp/methodenbot-inspect.*`-Verzeichnissen erneut
starten. Für Funktionstests die vier oben beschriebenen PN-Befehle benutzen.

Rollback muss nicht nur den alten Code/Unit-Einstieg wiederherstellen, sondern
auch die aktuellen `processed_emails.csv` und `stats.csv` zurückführen. Bereits
gesendete Matrix-Nachrichten werden durch ein Software-Rollback nicht gelöscht.
Der Manager bindet jedes Backup an die tatsächlich aktivierte Release-Linie;
ein zweiter oder fremder Rollback wird vor Dienststopp und CSV-Zugriff abgelehnt.
Eine nicht sicher verifizierbare Wiederherstellung lässt den Dienst bewusst
gestoppt, statt einen möglicherweise falschen Einstieg als erfolgreich zu melden.
