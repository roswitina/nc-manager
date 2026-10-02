# Nextcloud Server Manager

**Weboberfläche, die Prüfung, Backup, Update, PHP-Konfiguration, Wartung und Logs einer selbst gehosteten Nextcloud an einer Stelle bündelt – mit Protokoll jeder Aktion.**

![Version](https://img.shields.io/badge/version-0.6.3-blue)
![Lizenz](https://img.shields.io/badge/lizenz-MIT-green)
![Plattform](https://img.shields.io/badge/Debian%20%7C%20Ubuntu%20%7C%20DietPi-lightgrey)
![Status](https://img.shields.io/badge/status-Testversion-orange)

> [!WARNING]
> **Testversion.** Der Manager führt Aktionen mit Root-Rechten aus (Updates, Wiederherstellung der Datenbank,
> Löschen von Backups, Änderungen an PHP). Bitte zuerst auf einer Testinstanz ausprobieren und vorher ein
> unabhängiges Backup bzw. einen Snapshot anlegen. Nutzung auf eigenes Risiko – siehe [Haftungsausschluss](HAFTUNGSAUSSCHLUSS.md).

## Funktionen

| Seite | Was sie kann |
|---|---|
| **Dashboard** | Zustand von Nextcloud, System, PHP, Datenbank, Webserver, Speicher und Hintergrundjobs auf einen Blick |
| **Prüfungen** | Ergebnis von `occ setupchecks` – dieselben Hinweise wie die Nextcloud-Verwaltungsübersicht |
| **PHP** | Wirksame Werte für CLI und FPM (inkl. `.user.ini`/`.htaccess`), Empfehlungen mit Quelle, Fundstellen in den INI-Dateien; Werte setzen mit Konfigurationstest und Rückrollen |
| **PHP-FPM** | Prozess-Pool, Speicher je Worker, Treffer „max_children erreicht“, berechneter Vorschlag zum Übernehmen |
| **Apps** | Verfügbare App-Updates, einzeln oder alle installieren |
| **Update** | Vorprüfung, Nextcloud-Update mit Backup davor und optional allen App-Updates |
| **Backups** | Datenbank, Code und config.php, optional mit Benutzerdaten; SHA-256-Prüfsummen, echte Prüfung, geführte Wiederherstellung mit Sicherheits-Backup |
| **Diagnose** | Datenbank, Redis und Cron mit konkreten Hinweisen |
| **Wartung** | Repair, fehlende Indizes/Spalten/Schlüssel, BigInt, Dateien einlesen, Aufräumen, Wartungsmodus |
| **Logs** | nextcloud.log mit Filter, archivieren und leeren, Rotation und Log-Level einstellen; Fehlerlogs von PHP-FPM und Webserver |
| **Protokoll** | Die letzten 500 Aktionen mit Ausgabe und Exit-Code |

Lange Aktionen laufen als Hintergrund-Job mit Live-Ausgabe, es läuft immer nur eine ändernde Aktion gleichzeitig.
Über [Hooks](docs/DOKUMENTATION.md#konfiguration) lassen sich eigene Skripte vor/nach Updates und nach Backups einhängen
(z. B. Backup aufs NAS kopieren).

## Voraussetzungen

- Debian 11+, Ubuntu 22.04+ oder DietPi (auch Raspberry Pi), mit systemd und apt
- Eine laufende Nextcloud (klassische Installation, z. B. unter `/var/www/nextcloud`) mit Apache oder nginx und PHP-FPM bzw. mod_php
- MariaDB/MySQL, PostgreSQL oder SQLite
- root-Zugang für die Installation

Nicht unterstützt: Docker, Snap und Nextcloud AIO.

## Installation

Aktuelles Archiv von der [Releases-Seite](../../releases) laden, dann auf dem Server:

```bash
tar xzf nc-manager-v0.6.3.tar.gz
cd nc-manager-v0.6.3
sudo ./install.sh
```

Oder direkt aus dem Repository:

```bash
git clone https://github.com/roswitina/nc-manager.git
cd nc-manager
sudo ./install.sh
```

Der Installer erkennt Nextcloud-Pfad, Webserver-Benutzer und PHP-Version selbst und fragt nur nach Zugangsdaten,
Zugriffsart und Backup-Ziel. Ein Upgrade übernimmt die bestehende Konfiguration – einfach die neue Version
genauso installieren. Unbeaufsichtigt: `NCM_ACCEPT_LICENSE=ja` setzen.

### Zugriff

Standard und empfohlen ist **„nur localhost“** (Port 8787):

```bash
# vom eigenen Rechner aus
ssh -L 8787:127.0.0.1:8787 user@server
# dann im Browser: http://localhost:8787
```

Alternativ hinter einem **HTTPS-Reverse-Proxy** (nginx/Apache) – Beispiele in der
[Dokumentation](docs/DOKUMENTATION.md#zugriff). Die Option „LAN“ gibt es, sie überträgt Passwort und Sitzung aber unverschlüsselt.

### Deinstallation

```bash
sudo ./uninstall.sh           # Programm entfernen, Konfiguration und Protokoll bleiben
sudo ./uninstall.sh --purge   # zusätzlich Konfiguration, Protokoll und Systembenutzer
```

Backups, Nextcloud selbst und die vom Manager gesetzten PHP-Werte bleiben in beiden Fällen erhalten.

## Sicherheit

- Die Web-App läuft als eigener Systembenutzer `ncmanager` **ohne** Root-Rechte.
- Per sudo darf sie genau einen Befehl aufrufen: den Wrapper `nc-manager-cmd` mit fester Liste erlaubter Aktionen und geprüften Argumenten.
- `occ`, der Updater und das Nextcloud-Log werden nur als Webserver-Benutzer angesprochen, nie als root.
- CSRF-Schutz, `SameSite=Strict`-Cookies, Login-Sperre nach 5 Fehlversuchen, Content-Security-Policy ohne Inline-Skripte.
- Zugangsdaten (Datenbank, Redis) erscheinen nie als Prozess-Argument; Backups sind nur für root lesbar.

Details: [Sicherheitskonzept](docs/DOKUMENTATION.md#sicherheitskonzept). Sicherheitslücken bitte nicht öffentlich
als Issue melden, sondern per Mail an roswitina@hotmail.com.

## Dokumentation

- 📖 [Vollständige Programmdokumentation](docs/DOKUMENTATION.md) – Installation, Bedienung, Konfiguration, Fehlerbehebung, Exit-Codes
- 📝 [Änderungen](CHANGELOG.md)
- ⚖️ [Lizenz](LICENSE.md) · [Gewährleistungs- und Haftungsausschluss](HAFTUNGSAUSSCHLUSS.md)

## Aufbau

| Datei | Aufgabe |
|---|---|
| `app.py` | Flask-Web-App (läuft als `ncmanager` unter Gunicorn) |
| `jobs.py` | Hintergrund-Jobs, Sperre und Protokoll (SQLite) |
| `templates/`, `static/` | HTML-Vorlagen, CSS, JavaScript |
| `nc-manager-cmd` | Root-Wrapper – der einzige per sudo erlaubte Befehl |
| `ncm_helper.py` | Helfer des Wrappers (Logs, FPM, DB-Dump/-Restore, Backup-Prüfung, Diagnose) |
| `install.sh`, `uninstall.sh` | Installation, Upgrade, Entfernen |
| `tests/` | Tests für Web-App und Wrapper |

## Entwicklung und Tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python3 -m pytest tests/        # Wrapper-Tests nur als root mit PHP; MariaDB-Tests, wenn ein Server läuft
shellcheck nc-manager-cmd install.sh uninstall.sh
```

Rückmeldungen, Fehlerberichte und Ideen gerne als [Issue](../../issues) – am besten mit Distribution,
Nextcloud-Version, Webserver, Datenbank und der Ausgabe aus dem Protokoll.

## Entstehung

Das Projekt wurde mit Unterstützung von KI-Werkzeugen entwickelt. Der vollständige Quellcode liegt in diesem
Repository und kann vor der Installation geprüft werden. Der Code ist mit automatisierten Tests abgedeckt
(Web-App, Wrapper, MariaDB und SQLite); Erfahrungen auf anderen Systemen fehlen noch – deshalb Testversion.

## Lizenz

Copyright (c) 2026 roswitina@hotmail.com – veröffentlicht unter der [MIT-Lizenz](LICENSE.md).
Ergänzend gilt der [Gewährleistungs- und Haftungsausschluss](HAFTUNGSAUSSCHLUSS.md); rechtlich maßgeblich ist der englische Lizenztext.

Verwendete Python-Pakete (werden bei der Installation aus PyPI geladen): Flask, Werkzeug, Jinja2, MarkupSafe,
itsdangerous, click (BSD-3-Clause), gunicorn, blinker (MIT).

*Nextcloud ist eine Marke der Nextcloud GmbH. Dieses Projekt ist kein offizielles Nextcloud-Produkt und steht in keiner Verbindung zur Nextcloud GmbH.*
