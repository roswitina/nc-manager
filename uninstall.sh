#!/bin/bash
# Nextcloud Server Manager
# Copyright (c) 2026 roswitina@hotmail.com
# SPDX-License-Identifier: MIT
# Lizenz: siehe LICENSE · Gewährleistungs- und Haftungsausschluss: siehe HAFTUNGSAUSSCHLUSS.md
# Entfernt den Nextcloud Server Manager.
#   ./uninstall.sh          Programm, Dienst, sudo-Regel und Wrapper entfernen
#   ./uninstall.sh --purge  zusätzlich Konfiguration, Protokoll und Systembenutzer entfernen
# Die Backups in /var/backups/nc-manager (u.a. config.php) bleiben immer erhalten.
set -eu
[ "$EUID" -eq 0 ] || { echo 'Bitte als root/sudo starten.'; exit 1; }
PURGE=0; [ "${1:-}" = --purge ] && PURGE=1
BK=$(sed -n 's/^NCM_BACKUP_DIR=//p' /etc/nc-manager.env 2>/dev/null | head -n1)

systemctl disable --now nc-manager 2>/dev/null || true
pkill -u ncmanager -f 'jobs.py run' 2>/dev/null && echo 'Hinweis: laufender Manager-Job wurde beendet.' || true
rm -f /etc/systemd/system/nc-manager.service /etc/sudoers.d/ncmanager /usr/local/sbin/nc-manager-cmd /run/nc-manager.lock
rm -rf /opt/nc-manager /usr/local/lib/nc-manager
systemctl daemon-reload

if [ "$PURGE" -eq 1 ]; then
  rm -rf /var/lib/nc-manager /etc/nc-manager.env /etc/nc-manager
  id ncmanager >/dev/null 2>&1 && userdel ncmanager
  echo 'Programm, Konfiguration, Protokoll und Benutzer entfernt.'
else
  echo 'Programm entfernt. /etc/nc-manager.env und /var/lib/nc-manager bleiben erhalten (vollständig: --purge).'
fi
echo 'Vom Manager gesetzte PHP-Werte bleiben aktiv: /etc/php/*/*/conf.d/99-nextcloud-manager.ini und /etc/php/*/fpm/pool.d/zzzz-nextcloud-manager.conf'
echo "Backups bleiben erhalten: ${BK:-/var/backups/nc-manager} (enthalten Datenbank und config.php mit Zugangsdaten – bei Bedarf manuell löschen)."
