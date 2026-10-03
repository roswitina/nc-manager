# Änderungen

Alle nennenswerten Änderungen am Nextcloud Server Manager, neueste zuerst.

## 0.7.0

- **Neue Seite „Webserver“** für Apache und nginx. Sie erkennt den Webserver, den VirtualHost bzw. server-Block der
  Nextcloud und prüft die Einstellungen, die die Nextcloud-Doku verlangt oder empfiehlt – wie bei PHP mit Ampel,
  Empfehlung, Grundlage („Doku“ bzw. „Doku-Beispiel“) und Link zum passenden Abschnitt der Doku.
  - **Apache:** Module (mod_rewrite Pflicht; headers, env, dir, mime, bei PHP-FPM setenvif empfohlen), AllowOverride
    für den Nextcloud-Ordner, Dav off, HSTS, Weiterleitungen für /.well-known, Pretty URLs, LimitRequestBody,
    mod_reqtimeout. Erkennt auch Einstellungen, die in einem `<IfModule>` eines nicht geladenen Moduls stehen und
    deshalb nicht wirken.
  - **nginx:** client_max_body_size, client_body_timeout, fastcgi_buffers, MIME-Typen für .mjs und .wasm,
    /.well-known, Sperre der internen Ordner (config, data, lib …), Sicherheits-Header, HSTS, front_controller_active,
    X-Powered-By, try_files im PHP-Block, gzip, server_tokens; fastcgi_read_timeout und fastcgi_request_buffering
    werden angezeigt.
  - **Vorschläge zum Kopieren:** Für jede Abweichung zeigt die Seite die passende Zeile und wo sie hingehört.
    Der Manager schreibt die Webserver-Konfiguration bewusst nicht selbst.
  - **Zwei sichere Aktionen:** fehlende Apache-Module aus einer festen Liste einschalten (mit Konfigurationstest und
    automatischer Rücknahme bei Fehlern) und den Webserver neu laden – nur nach erfolgreichem
    `apache2ctl configtest` bzw. `nginx -t`.
- Neue Wrapper-Aktionen `web_info` (nur lesen), `web_enmod` und `web_reload`; neue Helfer-Funktion `web_info`.
- 8 neue Tests, insgesamt 83. Die 4 Tests gegen echte Webserver laufen nur mit `NCM_TEST_WEBSERVER=1`, weil sie
  Testdateien nach /etc/apache2 und /etc/nginx schreiben.

## 0.6.5

- **„Versionen/Papierkorb: abgelaufene löschen“ meldete fälschlich FEHLER.** Steht die Aufbewahrung auf „auto“
  (Nextcloud-Standard), beenden sich `occ versions:expire` und `occ trashbin:expire` mit Exit-Code 1 und dem Hinweis
  „Auto expiration is configured …“. Das ist kein Fehler: Nextcloud räumt dann selbst über die Hintergrundjobs auf.
  Der Wrapper erkennt diese Meldung jetzt und wertet den Lauf als Erfolg mit Hinweis. Echte Fehler bleiben Fehler.
- **Wartungsseite:** Erklärtext zur BigInt-Konvertierung (wofür, wann nötig, einmalig, vorher Backup) und Hinweis zur
  Aufbewahrung „auto“; die Rückfrage vor BigInt empfiehlt Backup und Wartungsmodus.
- **Entwicklung:** neue Datei `requirements-dev.txt` (`pip install -r requirements-dev.txt`), Kommentar zu `NCM_SUDO`
  in `jobs.py` eindeutiger formuliert.
- 3 neue Tests, insgesamt 76.

## 0.6.4

**Sicherheit:** config.php gehört dem Webserver-Benutzer. Wer Nextcloud kompromittiert (z. B. über eine Lücke in einer App),
kann sie ändern. Werte daraus werden jetzt nicht mehr ungeprüft verwendet, wenn der Wrapper als root arbeitet.

- **Datenverzeichnis:** Der Installer liest `datadirectory` aus und hält es in `/etc/nc-manager.env` fest
  (`NCM_DATADIR`, nur für root lesbar). Backup und Wiederherstellung verwenden das Datenverzeichnis nur, wenn
  config.php damit übereinstimmt, der Ordner existiert, kein symbolischer Link und kein Systemverzeichnis ist,
  dem Webserver-Benutzer gehört und die Nextcloud-Markierung `.ocdata` enthält. Sonst bricht die Aktion ab,
  ohne etwas zu verändern (Exit-Code 38 bei Backups, 67 bei der Wiederherstellung). Vorher hätte ein
  manipulierter Pfad (z. B. `/etc`) bei einer Wiederherstellung mit Benutzerdaten von root verschoben und
  per `chown -R` dem Webserver-Benutzer übereignet werden können.
- **Datenbank-Zugangsdaten:** Host, Port, Name, Benutzer und Passwort werden vor der Verwendung geprüft.
  Abgelehnt werden Steuerzeichen (z. B. Zeilenumbrüche, mit denen sich zusätzliche Optionen wie `result-file=`
  in die MySQL-Optionsdatei schreiben ließen), Werte mit führendem `-`, nicht-numerische Ports und
  SQLite-Datenbanknamen mit `/` oder `..`.
- Die Seite „Wiederherstellen“ zeigt den Grund an, wenn das Datenverzeichnis nicht vertrauenswürdig ist.
- Installer: Hat sich das Datenverzeichnis seit der letzten Installation geändert, zeigt er alten und neuen Pfad
  und übernimmt den neuen nur nach Bestätigung.
- 12 neue Tests (Wrapper und Helfer), insgesamt 73.

**Upgrade:** `sudo ./install.sh` ausführen, das Datenverzeichnis wird dabei festgehalten. Bis dahin lehnen Backup
und Wiederherstellung mit dem Hinweis ab, install.sh auszuführen.

## 0.6.3

- **GitHub-tauglich:** Neue README, Dokumentation als Markdown in `docs/DOKUMENTATION.md`, Änderungsliste in `CHANGELOG.md`, Lizenztext als `LICENSE.md` (Inhalt unverändert MIT).
- **Logs: Archivieren und leeren.** Das Nextcloud-Log wird komprimiert ins Backup-Verzeichnis kopiert
  (`logs/nextcloud.log.<Zeit>.gz`, nur für root lesbar, die letzten 10 bleiben, einstellbar mit `NCM_LOG_ARCHIVE_KEEP`)
  und danach geleert. Besitzer und Rechte der Datei bleiben dabei erhalten. Ist das Archiv fehlerhaft, wird nicht geleert.
- **Logs: Größe und Rotation.** Die Seite zeigt Datei, Größe, die ältere Datei `nextcloud.log.1`, die Rotationsgrenze
  (`log_rotate_size`, Nextcloud-Standard 100 MB) und das Log-Level. Rotation (10 MB bis 500 MB oder aus) und Log-Level
  lassen sich direkt einstellen (`occ log:file --rotate-size`, `occ log:manage --level`).
- **Logs: „Nur neue Einträge ab jetzt“.** Blendet ältere Einträge aus, ohne etwas zu löschen. Der Zeitpunkt gilt pro Sitzung.
- **Sicherheit:** Der Pfad des Nextcloud-Logs stammt aus config.php, die der Webserver-Benutzer ändern kann. Lesen,
  Archivieren und Leeren laufen deshalb als Webserver-Benutzer, nicht als root. So lassen sich über einen manipulierten
  Pfad keine Systemdateien lesen oder leeren.

## 0.6.2

- Urheberangabe (roswitina@hotmail.com), MIT-Lizenz (`LICENSE`, ab 0.6.3 `LICENSE.md`) und ausführlicher Gewährleistungs- und
  Haftungsausschluss (`HAFTUNGSAUSSCHLUSS.md`, mit englischer Kurzfassung).
- Urheber- und Lizenzhinweis (SPDX) in allen Quelldateien.
- Der Installer zeigt den Hinweis an und verlangt eine Bestätigung. Bei unbeaufsichtigter Installation lässt sich
  das mit `NCM_ACCEPT_LICENSE=ja` überspringen.
- Die Oberfläche hat eine Fußzeile mit Urheber und Lizenz sowie die öffentliche Seite `/lizenz`.
- Funktional unverändert gegenüber 0.6.1.

## 0.6.1

Basis ist 0.5.2. Die Ideen aus dem Entwicklungsstand 0.6.0 sind übernommen, dessen Fehler behoben.

**Backups**
- Jedes Backup hat SHA-256-Prüfsummen (`checksums.sha256`), die beim Erstellen geschrieben werden.
- Optionales **Vollbackup mit Benutzerdaten**. Nextcloud läuft dabei im Wartungsmodus, damit Datenbank und Dateien
  zusammenpassen. Der Wartungsmodus wird auch bei einem Abbruch wieder ausgeschaltet. Ändert sich während des Sicherns
  eine einzelne Datei (tar-Code 1), gibt es nur einen Hinweis, das Backup bleibt erhalten.
- Die Liste zeigt pro Backup „mit/ohne Benutzerdaten“, den Prüfstatus und den Anlass.
- **Prüfung** als Hintergrund-Job: Prüfsummen-Abgleich, vollständiges Entpacken von Dump und Archiven,
  Abschlusszeile des Dumps (erkennt abgeschnittene Dumps), `integrity_check` bei SQLite, Vollständigkeit des
  Programmcodes. Das Ergebnis steht in `verify.json` und in der Liste.

**Wiederherstellung (neu)**
- Assistent mit Vorprüfung und Bestätigung durch Eintippen von WIEDERHERSTELLEN. Der Ablauf:
  Backup prüfen → Zuordnung und Platz prüfen → Sicherheits-Backup des aktuellen Stands → Code vorbereiten und
  Wartungsmodus an → Benutzerdaten (falls enthalten) → Datenbank → Code austauschen → Wartungsmodus aus.
- MySQL/MariaDB: Vor dem Import werden alle Tabellen gelöscht. Sonst bleiben nach dem Backup entstandene Tabellen
  übrig und das nächste Upgrade scheitert. Scheitert der Import, wird automatisch das Sicherheits-Backup eingespielt.
- PostgreSQL: Der Import läuft in einer Transaktion mit ON_ERROR_STOP und ist damit ganz oder gar nicht.
- SQLite: Besitzer und Rechte der Datei bleiben erhalten, alte -wal/-shm-Dateien werden beiseitegelegt.
- Alte Ordner werden nur umbenannt (`.ncm-before-restore-…`). Bei einem Fehler bleibt der Wartungsmodus an.
- Verweigert wird der Restore bei Backups einer anderen Installation, bei einem Datenverzeichnis im Programmordner,
  bei einem Datenverzeichnis als eigenem Mountpoint (nur bei Vollbackups) und bei zu wenig Platz.

**Weitere Neuerungen**
- **Diagnose-Seite:** Datenbank (Version, Größe, Tabellen, max_connections), Redis (Erreichbarkeit mit den Zugangsdaten
  aus config.php; das Passwort verlässt den Wrapper nie) und Hintergrundjobs (Modus, letzter Lauf, Cron-Eintrag/Timer)
  mit konkreten Hinweisen.
- **PHP: „Wo stehen die Werte?“** Fundstellen jedes Werts in den tatsächlich geladenen INI-Dateien, die wirksame ist markiert.
- **Hooks** in `/etc/nc-manager/hooks/`: `pre-update` (bei Fehler kein Update), `post-update` und `post-backup`
  (z.B. Kopie auf ein NAS, mit `NCM_BACKUP_PATH`). Ausgeführt werden nur root-eigene Dateien, die nicht für
  Gruppe/Andere beschreibbar sind.
- **Update-Vorprüfung** zeigt das letzte Backup (mit/ohne Daten, Prüfstatus) und verfügbare App-Updates.
- Kurzzeit-Cache für Dashboard, PHP-Plattform und FPM-Daten. Er wird nach jeder Änderung geleert.
- **Installer-Self-Test** (Wrapper/occ, Dashboard-Daten, Login-Seite) und zusätzliche systemd-Härtung.
  `ProtectHome` ist entfallen, weil es auch dem root-Wrapper den Zugriff auf /home sperrte (Backups/Daten unter /home).

## 0.5.2

- **PHP-Empfehlungen mit Grundlage und Quelle:** Zu jedem Wert steht, worauf er beruht.
  „Doku“ heißt, die Nextcloud-Dokumentation nennt den Wert ausdrücklich (memory_limit, output_buffering).
  „Doku-Beispiel“ heißt, es ist ein Beispielwert der Doku, den man an die eigenen Dateigrößen anpasst (Upload-Größen, Zeitlimits).
  „Richtwert“ heißt, die Doku nennt keinen festen Wert, empfohlen wird der PHP-Standard bzw. ein Erfahrungswert (OPcache).
  Jede Empfehlung verlinkt die passende Seite der Nextcloud-Dokumentation.
- **Wirksamer Web-Wert:** Die Seite berücksichtigt jetzt Nextclouds eigene .user.ini (gilt für PHP-FPM) und die
  php_value-Zeilen der .htaccess (gilt für Apache mit mod_php). Bei älteren Nextcloud-Versionen setzt die .user.ini z.B.
  upload_max_filesize=511M und überstimmt damit die PHP-Konfiguration. Die Seite zeigt das an und warnt.
  Diese Dateien ändert der Manager bewusst nicht, weil Nextcloud sie bei Updates ersetzt.
- **output_buffering** ist neu als setzbarer Wert (Doku: muss 0 sein).

## 0.5.1 (Hotfix)

- Logs-Seite: HTTP 500 behoben. Der Wrapper hat den Log-Inhalt als Kommandozeilen-Argument weitergegeben,
  was Linux ab 128 KB ablehnt („Argument list too long“) – bei normalen Logs schon ab ca. 500 Zeilen.
  Große Daten (Logs, App-Liste, Core-Konfiguration) laufen jetzt über temporäre Dateien, die automatisch gelöscht werden.
- Die Logs-Seite zeigt Fehler des Wrappers als Meldung an, statt abzustürzen. Einträge mit ungewöhnlichem Level stören nicht mehr.
- Unerwartete Fehler zeigen eine verständliche Seite mit Hinweis auf `journalctl -u nc-manager`.
- Die Anleitung WIEDERHERSTELLEN.txt in Backups spielt erst die Datenbank, dann den Programmcode zurück.
- Neue Tests für den echten Wrapper (laufen als root mit PHP).

## 0.5.0

- **Prüfungen:** Zeigt das Ergebnis von `occ setupchecks` (ab Nextcloud 28), also dieselben Hinweise wie die
  Verwaltungsübersicht, nach Schwere sortiert und mit Doku-Links. Das Ergebnis wird zwischengespeichert, das Dashboard
  zeigt eine Zusammenfassung. Nach jeder Wartungsaktion wird automatisch neu geprüft.
- **Apps:** Verfügbare App-Updates mit installierter und neuer Version. Einzelne oder alle Apps lassen sich aktualisieren,
  dazu gibt es eine Liste der aktivierten und deaktivierten Apps. Beim Nextcloud-Update kann man optional alle App-Updates mit ausführen.
- **Backups:** Datenbank-Dump (MySQL/MariaDB, PostgreSQL, SQLite), Programmcode ohne Datenverzeichnis und config.php,
  jeweils mit Anleitung `WIEDERHERSTELLEN.txt`. Backups lassen sich manuell oder automatisch vor dem Update erstellen
  (Standard). Ziel und Anzahl der aufbewahrten Backups fragt der Installer ab (`NCM_BACKUP_DIR`, `NCM_BACKUP_KEEP`).
  DB-Zugangsdaten werden nie als Argument übergeben, sondern nur über stdin und eine temporäre 600-Datei.
- **Logs:** nextcloud.log mit Filter nach Level und Suchtext, Ausnahmen werden kompakt angezeigt.
  Dazu die letzten Zeilen aus dem PHP-FPM- und Webserver-Fehlerlog.
- **PHP-FPM:** Pool-Einstellungen, laufende Worker und deren Speicher (PSS), sowie die Zahl der Treffer
  „max_children erreicht“ im Log. Ein Vorschlag wird aus RAM und gemessenem Speicher je Worker berechnet und lässt sich
  mit einem Klick übernehmen. Eigene Werte sind ebenfalls möglich. Geschrieben wird nur in
  `pool.d/zzzz-nextcloud-manager.conf`, vorher läuft `php-fpm -t` mit Rollback.
- **Aufräumen** auf der Wartungsseite: `files:scan --all`, `files:cleanup`, `trashbin:expire`, `versions:expire`.
  Dazu, rot markiert und mit Rückfrage, Papierkorb bzw. alle Versionen leeren.
- Neue Tab-Navigation, die auf dem Handy horizontal scrollt.

## 0.4.5

- PHP-Seite: Für jede änderbare Einstellung werden der aktuelle Web/FPM-Wert, der CLI-Wert und die Empfehlung aus dem Nextcloud-Admin-Handbuch angezeigt.
  Zu niedrige Werte sind markiert und lassen sich mit einem Klick auf die Empfehlung setzen.
  Höhere Werte bleiben unangetastet, es gibt nie einen Vorschlag zum Herabsetzen.
- Das Formular „Eigenen Wert setzen“ ist mit dem aktuellen Wert vorausgefüllt und aktualisiert sich bei Auswahl einer anderen Einstellung.
- Warnung, wenn `post_max_size` kleiner als `upload_max_filesize` ist.

## 0.4.4

**Fehlerbehebungen**
- Lange Aktionen (Update, Repair, BigInt, Indizes …) laufen als Hintergrund-Job mit Live-Ausgabe.
  Bisher brach der Gunicorn-Worker-Timeout (30 s) sie im Browser ab und es entstand kein Protokolleintrag.
- Das Update lässt sich nicht mehr per `POST /run/nextcloud_update` ohne Backup-Bestätigung starten.
- Ändernde Aktionen sind gesperrt (`flock`), solange eine andere läuft. Kein doppeltes Update durch Doppelklick.
- PHP: Es wird jede laufende FPM-Version neu geladen, in die geschrieben wurde, nicht nur die CLI-Version.
  Vorher läuft `php-fpm -t`, bei Fehlern wird zurückgerollt. Werte werden pro Einstellung validiert (z.B. `512M`, `-1`, Sekunden).
- occ läuft mit `--no-interaction`. `db:convert-filecache-bigint` wartete sonst auf eine Bestätigung.
- Die Update-Vorprüfung zählt ein Datenverzeichnis unter dem Programmordner nicht mehr mit.
- Hintergrundjobs: Modus und letzter Lauf kommen aus `occ config:list core`.
- Die Liste der geladenen CLI-INI-Dateien ist vollständig (zuvor nur die ersten beiden).

**Sicherheit**
- CSRF-Token auf allen Formularen, Cookies `SameSite=Strict` + `HttpOnly` (+ `Secure` hinter Proxy), 2 h Leerlauf-Timeout.
- Login-Sperre nach 5 Fehlversuchen für 15 Minuten pro IP. Fehlversuche landen im Journal.
- Security-Header (CSP ohne Inline-Skripte, `X-Frame-Options`, `nosniff`, `no-referrer`).
- Automatisches HTML-Escaping durch Jinja2-Templates.
- Backups in `/var/backups/nc-manager` nur für root lesbar (`700`/`600`).
- Der Installer schlägt localhost als Standard vor, fragt das Passwort doppelt ab (min. 12 Zeichen)
  und übergibt es per stdin statt als Prozess-Argument.

**Betrieb**
- Das Protokoll enthält nur noch ausgeführte Aktionen und behält die letzten 500 Einträge.
- Das Dashboard braucht 2 statt 8 occ-Aufrufe (`status --output=json`) und zeigt zusätzlich memcache.local/locking.
- `systemctl restart nc-manager` bricht ein laufendes Update nicht mehr ab (`KillMode=process`).
- `uninstall.sh` entfernt jetzt auch `/opt/nc-manager`. Mit `--purge` werden zusätzlich Konfiguration, Protokoll und Benutzer entfernt.
