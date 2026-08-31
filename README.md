# Methodenbot – finale Fassung

Stand: 31.08.2026. Diese Arbeitskopie verbindet den bisherigen produktiven
Methodenbot mit der getesteten GWDG-KI-Zusammenfassung und einer explizit
freigegebenen persönlichen Matrix-Steuerung für mehrere Verantwortliche.

## Verhalten

Normale TYPO3-Beratungsanfragen werden weiterhin im produktiven Matrix-Raum
veröffentlicht. Die Hauptnachricht erhält darunter Geschwisterantworten:

1. optional die klar als KI-generiert markierte Zusammenfassung;
2. die unveränderten Originaldetails mit Protokoll-Link.

Die KI-Zusammenfassung nennt zuerst das eigentliche Beratungsanliegen und ordnet
danach Analyseart, Analyseschritt, Software und statistisches Modell ein. Es werden
bis zu drei brauchbare Entwürfe gesammelt und höchstens zehn gepacete Modellaufrufe
pro Anfrage verwendet. Typisch sind vier Aufrufe. Sämtliche Aufrufe laufen seriell
über `http://127.0.0.1:18765/v1/chat/completions`; der Bot erhält nur den lokalen
Gateway-Token, nie den GWDG-Upstream-Schlüssel.

Der persistente KI-Schalter startet bei der ersten Inbetriebnahme **aus**. Ein
KI-Fehler unterdrückt niemals die Originaldetails; stattdessen erscheint ein
sichtbarer Hinweis „KI-Zusammenfassung nicht verfügbar“. Ist dagegen die
Matrix-Zustellung dieses KI-Beitrags selbst unbestätigt, bleibt die gesamte
Anfrage unverarbeitet und wird mit denselben Transaktions-IDs erneut versucht;
sie wird nicht ohne den vorgesehenen KI-Beitrag als erledigt markiert.

## Persönliche Befehle

Die primäre Kontrollperson und jede zusätzliche, ausdrücklich konfigurierte
Kontrollperson dürfen dieselben vier Befehle verwenden. Jede Person arbeitet in
einer eigenen exklusiven Bot-Zweier-PN; private Ergebnisse und Fehler werden nie
in den Chat einer anderen Kontrollperson umgeleitet.

Nur exakt diese vier reinen Textnachrichten werden akzeptiert:

- `KI an` – prüft die administrative Freigabe und schaltet KI global ein;
- `KI aus` – schaltet KI global aus;
- `Test` – sendet die letzten drei unterschiedlichen TYPO3-Anfragen aus Inbox
  und Korrespondenz chronologisch in die private Unterhaltung. Jede Hauptnachricht
  beginnt mit `Test · n/3`;
- `Test 2` – sendet die neueste Anfrage einmal in den echten Zielkanal. Die
  Hauptnachricht beginnt exakt mit `Techniktest`; anschließend folgt eine private
  Zustellbestätigung.

Der zum Zeitpunkt des Befehls gültige KI-Schalter gilt für den ganzen Test. Bei
`KI aus` erfolgen keinerlei KI-Aufrufe. `Test` und `Test 2` schreiben weder
`processed_emails.csv` noch `stats.csv` und verändern keine Exchange-Nachricht.
`Test 2` erzeugt jedoch absichtlich eine echte, nicht automatisch widerrufbare
Matrix-Nachricht.

`Test` und `Test 2` werden vor Exchange- oder KI-Arbeit sofort in der jeweiligen
privaten Unterhaltung bestätigt. Eigene Raumpoller bleiben auch während eines
laufenden Tests ansprechbar. Ein gemeinsamer Worker verarbeitet alle Befehle
seriell, damit sich KI-Aufrufe, globale Schalteränderungen und echte
`Test 2`-Zustellungen nicht überholen.

## Sicherheitsgrenzen der Steuerung

Ein Befehl gilt ausschließlich, wenn er

- von der für genau diesen Raum explizit konfigurierten Person stammt;
- im zu dieser Person gehörenden Kontrollraum eintrifft;
- eine unveränderte `m.text`-Nachricht ist, nicht Edit, Antwort oder Thread;
- aus einem unverschlüsselten Zweierraum mit genau Bot und Kontrollperson stammt;
- in einem Raum mit Einladungs- und Power-Level-Schutz liegt, in dem die Kontrollperson
  weder weitere Mitglieder einladen noch Sicherheitszustände ändern kann.

Das primäre Paar wird mit `MATRIX_CONTROL_USER` und `MATRIX_CONSOLE_ROOM_ID`
konfiguriert. Weitere Paare stehen als User-zu-Raum-Objekt in
`MATRIX_ADDITIONAL_CONTROL_ROOMS_JSON`. Doppelte Personen oder Räume sowie eine
Kollision mit dem produktiven Zielraum werden abgelehnt. Die fehlende
Ende-zu-Ende-Verschlüsselung muss mit
`MATRIX_ALLOW_UNENCRYPTED_CONTROL_DM=true` ausdrücklich freigegeben sein. Beim
allerersten Start eines Raums wird nur dessen aktueller `/sync`-Cursor gespeichert;
vorhandene alte Chatnachrichten werden nicht ausgeführt. Begrenzte Timelines oder
ein unsicherer Raum führen für diesen Raum zu einem fail-closed Stopp. Start und
Live-Preflight validieren alle konfigurierten Räume.

Nach der idempotenten privaten Wartebestätigung werden alle Testinhalte vor ihrem
ersten Matrix-Versand vollständig eingefroren und in einem raumgebundenen Journal
mit Modus 0600 gespeichert. Der bisherige primäre Zustand bleibt unverändert unter
`/var/lib/methodenbot/control/state.json`; zusätzliche Zustände liegen unter
gehashten Unterverzeichnissen.
Deterministische Matrix-Transaktions-IDs, Readback und ein geschützter persistenter
Matrix-Token verhindern Wiederholungen nach einem Crash. Erledigte Nachrichteninhalte
werden aus dem Journal entfernt. Vorübergehende Zustellfehler werden höchstens fünfmal
mit derselben Transaktions-ID versucht. Bei einem dauerhaften Fehler wird nur der
betroffene Befehl beendet und eine private Warnung versucht; danach blockiert er keine
späteren Steuerbefehle. Eine mögliche Teilzustellung wird dabei nicht blind wiederholt.

Auch `processed_emails.csv` ist ein fail-closed Zustelljournal: vorhandene beschädigte,
unsichere oder unlesbare Dateien stoppen den Dienst, statt als leere Liste interpretiert
zu werden. Änderungen erfolgen atomar und werden auf Datei und Verzeichnis synchronisiert.

## Laufzeitpfade

- Code: `/srv/methodenbot-final/releases/<release>/`
- aktiver Symlink: `/srv/methodenbot-final/current`
- Konfiguration: `/etc/methodenbot/runtime.env`
- lokaler Gateway-Token: systemd-Credential `gwdg-local-token`
- verarbeitete IDs, Statistik, Matrix-Sitzung und Kontrollzustand:
  `/var/lib/methodenbot/`
- bestehende virtuelle Umgebung: `/home/methodenbot/methodenbot/venv`

Der Releasebaum enthält keine `.env`, CSV, Logs, Token oder Schlüssel.

## Bereitstellung

Lokal wird ein manifestiertes, geheimnisfreies Bundle erzeugt:

```sh
python3 deployment/build_bundle.py
```

Nach dem Kopieren und sicheren Entpacken auf den vorgesehenen Linux-Host werden
dessen erwarteter Kurzname und der berechtigte sudo-Login explizit gesetzt. Der
im Bundle enthaltene Manager wird dann schrittweise ausgeführt:

```sh
export METHODENBOT_DEPLOY_HOST='<server-kurzname>'
export METHODENBOT_DEPLOY_ADMIN='<sudo-login>'
python3 deployment/manage.py plan
sudo env METHODENBOT_DEPLOY_HOST="$METHODENBOT_DEPLOY_HOST" METHODENBOT_DEPLOY_ADMIN="$METHODENBOT_DEPLOY_ADMIN" \
  python3 deployment/manage.py stage --confirm-data-transfer
sudo env METHODENBOT_DEPLOY_HOST="$METHODENBOT_DEPLOY_HOST" METHODENBOT_DEPLOY_ADMIN="$METHODENBOT_DEPLOY_ADMIN" \
  python3 deployment/manage.py live-preflight --confirm-data-transfer
sudo env METHODENBOT_DEPLOY_HOST="$METHODENBOT_DEPLOY_HOST" METHODENBOT_DEPLOY_ADMIN="$METHODENBOT_DEPLOY_ADMIN" \
  python3 deployment/manage.py activate --confirm-restart
```

`stage` verändert den laufenden Dienst nicht. `live-preflight` liest Matrix-Raum,
Gateway-Modellliste und die letzten drei Exchange-Anfragen, sendet aber nichts.
Erst `activate` stoppt den bestehenden Dienst kurz, legt ein rootgeschütztes
Backup an, migriert CSVs und setzt das systemd-Drop-in. Ein Fehler während der
Aktivierung stellt den vorherigen Einstieg nur dann automatisch wieder her, wenn
Codepfad, Prozess und stabile PID anschließend verifiziert sind; andernfalls bleibt
der Dienst absichtlich gestoppt und meldet den Restore-Fehler eindeutig. Der
ausgegebene Backupname kann zusätzlich nur in der dazu passenden aktiven Release-Linie
für `rollback --backup NAME --confirm-restart` verwendet werden. Folgereleases
bewahren die kanonische `runtime.env`, den lokalen Token, CSVs und den globalen
KI-Schalter. Die Aktivierung gilt erst als bereit, wenn alle konfigurierten
Raumpoller derselben stabilen Dienst-PID initialisiert wurden.

## Tests

Offline, ohne Netzwerk:

```sh
/home/methodenbot/methodenbot/venv/bin/python -B -m unittest discover -s tests -v
/home/methodenbot/methodenbot/venv/bin/python -m pip check
```

Abgedeckt sind unter anderem Parserkompatibilität, KI-Redaktion und -Pacing,
exakte raumgebundene Befehlsautorisierung, Erststart ohne History-Replay,
Mehrraum-Polling, globale Toggle-Reihenfolge, falsche/unsichere Räume, Zielräume
für `Test`/`Test 2`, KI-aus ohne Modellaufruf,
Matrix-401-Reauthentifizierung samt persistiertem Token, echte Zustandsneustarts,
Crash-Idempotenz, begrenzte Zustellfehler, globale Mailauswahl und fail-closed CSV-Zustand.

## Noch nicht durch Offline-Tests bewiesen

Offline-Tests beweisen keinen erfolgreichen Zugriff auf Exchange, Matrix oder das
lokale GWDG-Gateway. Vor produktiver Aktivierung sind deshalb erforderlich:

1. serverseitiger Konfigurations- und Berechtigungs-Preflight;
2. Dienststart mit KI-Schalter aus;
3. eine frische private Nachricht `Test` und Kontrolle, dass CSV/Statistik
   unverändert bleiben;
4. `KI an`, privater `Test`, anschließend optional `KI aus`;
5. `Test 2` nur als bewusster letzter Produktionstest.
