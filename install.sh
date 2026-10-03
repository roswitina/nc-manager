#!/bin/bash
# Nextcloud Server Manager
# Copyright (c) 2026 roswitina@hotmail.com
# SPDX-License-Identifier: MIT
# Lizenz: siehe LICENSE · Gewährleistungs- und Haftungsausschluss: siehe HAFTUNGSAUSSCHLUSS.md
# Installer / Upgrade für den Nextcloud Server Manager.
set -euo pipefail
VERSION=0.7.0
SRC="$(cd "$(dirname "$0")" && pwd)"
[ "$EUID" -eq 0 ] || { echo 'Bitte als root/sudo starten.'; exit 1; }
echo "=== Nextcloud Server Manager $VERSION Installer ==="
cat <<'TXT'

Urheber: roswitina@hotmail.com · Lizenz: MIT (siehe LICENSE)

WICHTIG – GEWÄHRLEISTUNGS- UND HAFTUNGSAUSSCHLUSS (Details: HAFTUNGSAUSSCHLUSS.md)
Diese Software wird unentgeltlich und "wie besehen" ohne jede Gewährleistung bereitgestellt.
Sie führt Aktionen mit Root-Rechten aus (Updates, Wiederherstellung der Datenbank, Löschen von
Backups, Papierkorb und Versionen, Änderungen an PHP), die Daten unwiderruflich verändern oder
löschen können. Die Nutzung erfolgt auf eigenes Risiko. Soweit gesetzlich zulässig, wird keine
Haftung für Schäden jeglicher Art übernommen, insbesondere nicht für Datenverlust oder Ausfälle.
Vorher ein aktuelles, unabhängiges Backup bzw. einen Snapshot anlegen und zuerst testen.
Nextcloud ist eine Marke der Nextcloud GmbH; dieses Projekt ist nicht mit ihr verbunden.

TXT
if [ "${NCM_ACCEPT_LICENSE:-}" != ja ]; then
  read -rp 'Lizenz und Haftungsausschluss akzeptieren und fortfahren? [j/N]: ' ACCEPT
  [[ "${ACCEPT:-}" =~ ^[JjYy]$ ]] || { echo 'Abgebrochen – es wurde nichts verändert.'; exit 1; }
fi
. /etc/os-release 2>/dev/null || true; echo "System: ${PRETTY_NAME:-Linux}"
command -v apt-get >/dev/null || { echo 'Benötigt Debian/DietPi/Ubuntu mit apt.'; exit 1; }
apt-get update; apt-get install -y python3 python3-venv sudo util-linux

PHP="$(command -v php || true)"
[ -n "$PHP" ] || { echo 'FEHLER: php (CLI) nicht gefunden.'; exit 1; }
NCC="$(command -v ncc || true)"; BACKEND=occ
if [ -f /boot/dietpi/.version ] || grep -qi dietpi /etc/os-release 2>/dev/null; then
  # DietPi stellt ncc oft nur als Shell-Alias bereit; Skripte erben keine Aliase.
  echo "✓ DietPi erkannt: ncc ist ggf. Shell-Alias auf occ; Manager verwendet occ direkt."
fi
if [ -n "$NCC" ]; then BACKEND=ncc; echo "✓ ausführbares ncc gefunden: $NCC"; fi

NC_PATH=''
for p in /var/www/nextcloud /var/www/html/nextcloud /opt/nextcloud; do [ -f "$p/occ" ] && { NC_PATH="$p"; break; }; done
[ -n "$NC_PATH" ] || read -rp 'Nextcloud-Pfad: ' NC_PATH
[ -f "$NC_PATH/occ" ] || { echo "Kein occ unter $NC_PATH"; exit 1; }
WEBUSER=www-data; id "$WEBUSER" >/dev/null 2>&1 || read -rp 'Webserver-Benutzer: ' WEBUSER
id "$WEBUSER" >/dev/null 2>&1 || { echo "Benutzer $WEBUSER existiert nicht."; exit 1; }
echo "✓ Nextcloud: $NC_PATH"; echo "✓ PHP: $PHP"; echo "✓ Backend: $BACKEND"

# Bestehende Konfiguration übernehmen (nie per source einlesen: Hashes enthalten '$').
ENVF=/etc/nc-manager.env; SERVICE=/etc/systemd/system/nc-manager.service
getenv() { [ -r "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF" | head -n1 || true; }
OLD_USER=$(getenv NCM_USER); OLD_USER=${OLD_USER:-admin}
OLD_HASH=$(getenv NCM_PASSWORD_HASH); OLD_SECRET=$(getenv NCM_SECRET); OLD_PROXY=$(getenv NCM_BEHIND_PROXY)
OLD_BKDIR=$(getenv NCM_BACKUP_DIR); OLD_KEEP=$(getenv NCM_BACKUP_KEEP)
OLD_DATADIR=$(getenv NCM_DATADIR)
OLD_BIND=''; OLD_PORT=''
if [ -r "$SERVICE" ]; then
  b=$(sed -n 's/.*--bind \([^ ]*\).*/\1/p' "$SERVICE" | head -n1)
  OLD_BIND=${b%:*}; OLD_PORT=${b##*:}
  echo '✓ Bestehende Installation gefunden – Einstellungen werden als Vorgabe übernommen.'
fi

echo
echo 'Zugriff:'
echo '  1) nur localhost  (empfohlen; Zugriff per SSH-Tunnel oder HTTPS-Reverse-Proxy)'
echo '  2) LAN            (ACHTUNG: unverschlüsseltes HTTP, Passwort im Klartext im Netz)'
DEF_MODE=1; [ "$OLD_BIND" = 0.0.0.0 ] && DEF_MODE=2
read -rp "Auswahl [$DEF_MODE]: " MODE; MODE=${MODE:-$DEF_MODE}
BIND=127.0.0.1; [ "$MODE" = 2 ] && BIND=0.0.0.0
read -rp "Port [${OLD_PORT:-8787}]: " PORT; PORT=${PORT:-${OLD_PORT:-8787}}
[[ "$PORT" =~ ^[0-9]{2,5}$ ]] || { echo 'Ungültiger Port.'; exit 1; }
DEF_PROXY=N; [ "$OLD_PROXY" = 1 ] && DEF_PROXY=J
read -rp "Läuft ein HTTPS-Reverse-Proxy (nginx/Apache) davor? [j/n, Vorgabe $DEF_PROXY]: " PROXY; PROXY=${PROXY:-$DEF_PROXY}
BEHIND_PROXY=0; [[ "$PROXY" =~ ^[JjYy]$ ]] && BEHIND_PROXY=1
read -rp "Admin-Benutzer [$OLD_USER]: " ADMIN; ADMIN=${ADMIN:-$OLD_USER}
[[ "$ADMIN" =~ ^[A-Za-z0-9._@-]{1,64}$ ]] || { echo 'Ungültiger Benutzername.'; exit 1; }

echo
echo 'Backups (Datenbank + Programmcode). Auf dem Raspberry Pi am besten ein USB-Laufwerk statt der SD-Karte wählen.'
read -rp "Backup-Verzeichnis [${OLD_BKDIR:-/var/backups/nc-manager}]: " BKDIR; BKDIR=${BKDIR:-${OLD_BKDIR:-/var/backups/nc-manager}}
[[ "$BKDIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || { echo 'Ungültiger Pfad (nur Buchstaben, Ziffern, . _ - /).'; exit 1; }
read -rp "Anzahl aufzubewahrender Backups [${OLD_KEEP:-3}]: " KEEP_N; KEEP_N=${KEEP_N:-${OLD_KEEP:-3}}
[[ "$KEEP_N" =~ ^[0-9]{1,3}$ ]] && [ "$KEEP_N" -ge 1 ] || { echo 'Ungültige Anzahl.'; exit 1; }

# Datenverzeichnis festhalten: Der root-Wrapper vertraut später nur diesem Wert, nicht config.php
# (die kann der Webserver-Benutzer ändern). Ausgelesen als Webserver-Benutzer, wie Nextcloud selbst.
CUR_DATADIR=$(runuser -u "$WEBUSER" -- "$PHP" -r '
$d = $argv[1]; $c = [];
foreach (array_merge([$d . "/config.php"], glob($d . "/*.config.php") ?: []) as $f) {
  $CONFIG = []; if (is_readable($f)) { include $f; } $c = array_merge($c, $CONFIG);
}
echo $c["datadirectory"] ?? "";' -- "$NC_PATH/config" 2>/dev/null || true)
CUR_DATADIR=${CUR_DATADIR%/}
DATADIR=$CUR_DATADIR
if [ -n "$OLD_DATADIR" ] && [ "$OLD_DATADIR" != "$CUR_DATADIR" ]; then
  echo
  echo 'ACHTUNG: Das Datenverzeichnis in config.php hat sich seit der letzten Installation geändert:'
  echo "  bisher festgehalten: $OLD_DATADIR"
  echo "  jetzt in config.php: ${CUR_DATADIR:-(nicht gesetzt)}"
  echo 'Nur übernehmen, wenn du es selbst verschoben hast – sonst wurde config.php möglicherweise manipuliert.'
  read -rp 'Neuen Pfad aus config.php übernehmen? [j/N]: ' TAKE
  [[ "${TAKE:-}" =~ ^[JjYy]$ ]] || DATADIR=$OLD_DATADIR
fi
[ -n "$DATADIR" ] || read -rp 'Nextcloud-Datenverzeichnis (datadirectory): ' DATADIR
DATADIR=${DATADIR%/}
[[ "$DATADIR" =~ ^/[A-Za-z0-9._/@+-]+$ ]] && [[ ! "$DATADIR/" =~ /\.\.?/ ]] || { echo "Ungültiges Datenverzeichnis: $DATADIR"; exit 1; }
[ -d "$DATADIR" ] && [ -e "$DATADIR/.ocdata" ] || echo "WARNUNG: $DATADIR fehlt oder enthält keine .ocdata – Backups und Wiederherstellung werden abgelehnt, bis das stimmt."
echo "✓ Datenverzeichnis: $DATADIR"

HASH=''; PASS=''
if [ -n "$OLD_HASH" ]; then read -rp 'Bestehendes Passwort beibehalten? [J/n]: ' KEEP; KEEP=${KEEP:-J}; else KEEP=n; fi
if [[ "$KEEP" =~ ^[JjYy]$ ]]; then
  HASH="$OLD_HASH"
else
  while true; do
    read -rsp 'Admin-Passwort (min. 12 Zeichen): ' PASS; echo
    [ ${#PASS} -ge 12 ] || { echo 'Zu kurz.'; continue; }
    read -rsp 'Passwort wiederholen: ' PASS2; echo
    [ "$PASS" = "$PASS2" ] && break
    echo 'Passwörter stimmen nicht überein.'
  done
fi

if systemctl cat nc-manager.service >/dev/null 2>&1; then
  echo 'Stoppe vorhandenen nc-manager Dienst für Upgrade...'
  systemctl stop nc-manager 2>/dev/null || true
fi

ID=ncmanager
id "$ID" >/dev/null 2>&1 || useradd --system --home /opt/nc-manager --shell /usr/sbin/nologin "$ID"
mkdir -p /opt/nc-manager /var/lib/nc-manager /usr/local/lib/nc-manager "$BKDIR"
# Hooks (pre-update, post-update, post-backup): nur root darf hier schreiben.
install -d -m 0755 -o root -g root /etc/nc-manager /etc/nc-manager/hooks
chmod 700 "$BKDIR"
find "$BKDIR" -type f -exec chmod 600 {} + 2>/dev/null || true

# Programmdateien ersetzen (venv bleibt erhalten).
rm -rf /opt/nc-manager/templates /opt/nc-manager/static /opt/nc-manager/__pycache__
cp "$SRC/app.py" "$SRC/jobs.py" "$SRC/requirements.txt" "$SRC/LICENSE" "$SRC/HAFTUNGSAUSSCHLUSS.md" /opt/nc-manager/
cp -r "$SRC/templates" "$SRC/static" /opt/nc-manager/
install -m 0755 -o root -g root "$SRC/nc-manager-cmd" /usr/local/sbin/nc-manager-cmd
# Der Helfer läuft als root – er muss root gehören und darf für ncmanager nicht beschreibbar sein.
install -m 0644 -o root -g root "$SRC/ncm_helper.py" /usr/local/lib/nc-manager/ncm_helper.py
install -m 0644 -o root -g root "$SRC/LICENSE" /usr/local/lib/nc-manager/LICENSE
chmod 755 /usr/local/lib/nc-manager

# Werkzeuge für den Datenbank-Dump
if systemctl is-active --quiet mariadb 2>/dev/null || systemctl is-active --quiet mysql 2>/dev/null; then
  command -v mariadb-dump >/dev/null || command -v mysqldump >/dev/null || apt-get install -y mariadb-client
fi
if systemctl is-active --quiet postgresql 2>/dev/null; then
  command -v pg_dump >/dev/null || apt-get install -y postgresql-client
fi

[ -x /opt/nc-manager/venv/bin/python ] || python3 -m venv /opt/nc-manager/venv
/opt/nc-manager/venv/bin/pip install -q --upgrade pip
/opt/nc-manager/venv/bin/pip install -q -r /opt/nc-manager/requirements.txt

if [ -z "$HASH" ]; then
  # Passwort über stdin – taucht so nicht in der Prozessliste auf.
  HASH=$(printf '%s' "$PASS" | /opt/nc-manager/venv/bin/python -c \
    'import sys; from werkzeug.security import generate_password_hash; print(generate_password_hash(sys.stdin.read()))')
  unset PASS PASS2
fi
SECRET=${OLD_SECRET:-}; [ -n "$SECRET" ] || SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')

umask 077
cat >"$ENVF" <<EOF
NCM_USER=$ADMIN
NCM_PASSWORD_HASH=$HASH
NCM_SECRET=$SECRET
NCM_BEHIND_PROXY=$BEHIND_PROXY
NCM_BACKEND=$BACKEND
NCM_NCC=${NCC:-/usr/local/bin/ncc}
NCM_PHP=$PHP
NCM_NC_PATH=$NC_PATH
NCM_WEBUSER=$WEBUSER
NCM_BACKUP_DIR=$BKDIR
NCM_BACKUP_KEEP=$KEEP_N
NCM_DATADIR=$DATADIR
EOF
chmod 600 "$ENVF"
umask 022

cat >/etc/sudoers.d/ncmanager <<EOF
ncmanager ALL=(root) NOPASSWD: /usr/local/sbin/nc-manager-cmd
EOF
chmod 440 /etc/sudoers.d/ncmanager
visudo -cf /etc/sudoers.d/ncmanager >/dev/null || { rm -f /etc/sudoers.d/ncmanager; echo 'FEHLER: sudoers ungültig.'; exit 1; }

cat >"$SERVICE" <<EOF
[Unit]
Description=Nextcloud Server Manager
After=network.target

[Service]
User=ncmanager
Group=ncmanager
EnvironmentFile=$ENVF
WorkingDirectory=/opt/nc-manager
# Timeout gilt nur noch für Seitenaufrufe; lange Aktionen laufen als Hintergrund-Job.
ExecStart=/opt/nc-manager/venv/bin/gunicorn --workers 2 --timeout 180 --bind $BIND:$PORT --access-logfile - app:app
Restart=on-failure
# Beim Stoppen/Neustarten nur Gunicorn beenden – ein laufendes Update läuft weiter.
KillMode=process
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
LockPersonality=true
RestrictRealtime=true
# Kein ProtectHome: Backup-Ziel oder Datenverzeichnis liegen manchmal unter /home, und die
# Einschränkung gilt auch für den root-Wrapper.
# sudo benötigt neue Privilegien. Weitere Sandbox-Optionen (ProtectSystem …) würden
# auch den root-Wrapper einschränken und PHP-/Nextcloud-Änderungen verhindern.
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOF

chown -R ncmanager:ncmanager /opt/nc-manager /var/lib/nc-manager
chmod 750 /var/lib/nc-manager
systemctl daemon-reload
systemctl reset-failed nc-manager 2>/dev/null || true
systemctl enable nc-manager >/dev/null
systemctl restart nc-manager
sleep 2
if ! systemctl is-active --quiet nc-manager; then
  echo 'FEHLER: nc-manager konnte nicht gestartet werden.'
  systemctl status nc-manager --no-pager || true
  exit 70
fi

# Self-Test: Wrapper/occ, Dashboard-Daten und Login-Seite
echo 'Self-Test ...'
SELF_OK=1; T_OUT=$(mktemp); trap 'rm -f "$T_OUT"' EXIT
# shellcheck disable=SC2024  # Absicht: root schreibt in die eigene mktemp-Datei
if sudo -u ncmanager sudo -n /usr/local/sbin/nc-manager-cmd status >"$T_OUT" 2>&1; then echo '  ✓ Wrapper und occ'
else echo '  ✗ Wrapper/occ:'; sed 's/^/    /' "$T_OUT"; SELF_OK=0; fi
# shellcheck disable=SC2024
if sudo -u ncmanager sudo -n /usr/local/sbin/nc-manager-cmd dashboard >"$T_OUT" 2>&1 && python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$T_OUT"; then
  echo '  ✓ Dashboard-Daten'
else echo '  ✗ Dashboard liefert keine gültigen Daten:'; head -5 "$T_OUT" | sed 's/^/    /'; SELF_OK=0; fi
if python3 - "$PORT" <<'PY'
import sys, time, urllib.request
for _ in range(10):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{sys.argv[1]}/login', timeout=5) as r:
            sys.exit(0 if r.status == 200 and b'Nextcloud' in r.read() else 1)
    except OSError:
        time.sleep(1)
sys.exit(1)
PY
then echo '  ✓ Login-Seite erreichbar'; else echo "  ✗ Login-Seite auf Port $PORT nicht erreichbar"; SELF_OK=0; fi
if [ "$SELF_OK" != 1 ]; then
  echo 'WARNUNG: Der Dienst läuft, aber der Self-Test meldet Probleme (siehe oben, außerdem: journalctl -u nc-manager).'
fi

IP=$(hostname -I | awk '{print $1}')
echo
echo "✓ Version $VERSION installiert."
echo 'Status: systemctl status nc-manager    Log: journalctl -u nc-manager -f'
if [ "$BIND" = 0.0.0.0 ]; then
  echo "GUI: http://${IP:-SERVER-IP}:$PORT   (unverschlüsselt – nur in vertrauenswürdigen Netzen nutzen)"
else
  echo "GUI: http://127.0.0.1:$PORT"
  echo "Vom eigenen Rechner aus:  ssh -L $PORT:127.0.0.1:$PORT <user>@${IP:-SERVER-IP}   →  http://localhost:$PORT"
fi
