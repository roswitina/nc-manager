#!/bin/bash
# nc-speicher-zusammenfuehren.sh – Version 1.1
# Führt nach einem Umzug des Nextcloud-Datenverzeichnisses den alten und den neuen Speicher-Eintrag
# (oc_storages) wieder zusammen. Hintergrund: Team-Ordner (groupfolders) merken sich den Speicher, in dem
# sie angelegt wurden. Wurde das Datenverzeichnis später verschoben, ohne den Speicher-Namen in der
# Datenbank anzupassen, legt Nextcloud einen zweiten Speicher an. Folge: Uploads in Team-Ordnern sind für
# andere unsichtbar, umbenannte Ordner verschwinden, files:repair-tree findet immer wieder Fehler.
#
# Was das Script tut (erst nach Bestätigung mit JA):
#   1. Wartungsmodus ein
#   2. Den neuen, doppelten Speicher-Eintrag löschen und den alten (von den Team-Ordnern benutzten)
#      auf den aktuellen Pfad umbenennen – in einer Transaktion
#   3. Redis und PHP-FPM neu starten (zwischengespeicherte Speicher-Nummern verwerfen)
#   4. Wartungsmodus aus (files:cleanup gehört zur Files-App, die im Wartungsmodus nicht geladen wird)
#   5. occ files:cleanup (entfernt die Einträge des gelöschten Doppel-Speichers)
#   6. Team-Ordner und App-Daten neu einlesen, Ergebnis prüfen
# Die Dateien auf der Festplatte werden nicht verändert.
#
# Aufruf als root auf dem Nextcloud-Server:   bash nc-speicher-zusammenfuehren.sh
# Nur anzeigen, nichts ändern:                bash nc-speicher-zusammenfuehren.sh --pruefen
set -Eeuo pipefail   # -E: ERR-Falle gilt auch in Funktionen

NCC=${NCC:-}
if [ -z "$NCC" ]; then
  if command -v ncc >/dev/null; then NCC=ncc
  else NCC="runuser -u www-data -- php /var/www/nextcloud/occ"; fi
fi
MYSQL=${MYSQL:-mysql}
CHECK_ONLY=0
case "${1:-}" in
  "") ;;
  --pruefen|--prüfen|--check) CHECK_ONLY=1 ;;
  *) echo "Unbekannte Option: $1 (erlaubt: --pruefen)" >&2; exit 2 ;;
esac

die() { echo "ABBRUCH: $*" >&2; exit 1; }
occ() { $NCC --no-interaction "$@"; }

[ "$(id -u)" = 0 ] || die "Bitte als root ausführen."

dbtype=$(occ config:system:get dbtype) || die "occ funktioniert nicht."
[ "$dbtype" = mysql ] || die "Dieses Script unterstützt nur MySQL/MariaDB (gefunden: $dbtype)."
dbname=$(occ config:system:get dbname)
prefix=$(occ config:system:get dbtableprefix || echo oc_)
[[ "$dbname" =~ ^[A-Za-z0-9_]+$ ]] || die "Ungewöhnlicher Datenbankname: $dbname"
[[ "$prefix" =~ ^[A-Za-z0-9_]*$ ]] || die "Ungewöhnliches Tabellen-Präfix: $prefix"
datadir=$(occ config:system:get datadirectory)
datadir=${datadir%/}
[ -d "$datadir" ] || die "Datenverzeichnis $datadir existiert nicht."
[[ "$datadir" =~ ^/[A-Za-z0-9_./-]+$ ]] || die "Ungewöhnlicher Pfad des Datenverzeichnisses: $datadir"

sql() { $MYSQL --batch --skip-column-names "$dbname" -e "$1"; }

newid="local::$datadir/"
[ ${#newid} -le 64 ] || newid=$(printf '%s' "$newid" | md5sum | cut -d' ' -f1)   # wie Nextcloud bei langen Namen

# Speicher, den die Team-Ordner benutzen
gf=$(sql "SELECT DISTINCT storage_id FROM ${prefix}group_folders WHERE storage_id IS NOT NULL AND storage_id > 0" 2>/dev/null) \
  || die "Tabelle ${prefix}group_folders nicht lesbar – ist die App Team-Ordner installiert?"
[ -n "$gf" ] || die "Kein Team-Ordner mit eingetragenem Speicher gefunden."
[ "$(printf '%s\n' "$gf" | wc -l)" = 1 ] || die "Team-Ordner verweisen auf mehrere Speicher ($(echo "$gf" | tr '\n' ' ')) – bitte von Hand prüfen."
old_num=$gf
old_id=$(sql "SELECT id FROM ${prefix}storages WHERE numeric_id = $old_num")
[ -n "$old_id" ] || die "Speicher $old_num der Team-Ordner fehlt in ${prefix}storages."
new_num=$(sql "SELECT numeric_id FROM ${prefix}storages WHERE id = '$newid'")

echo "Datenverzeichnis laut config.php: $datadir"
echo "Speicher der Team-Ordner:         $old_num  ($old_id)"
echo "Speicher des aktuellen Pfads:     ${new_num:-–}  ($newid)"
echo

if [ "$old_id" = "$newid" ]; then
  echo "In Ordnung: Die Team-Ordner benutzen bereits den Speicher des aktuellen Datenverzeichnisses. Nichts zu tun."
  exit 0
fi
case "$old_id" in local::/*) ;; *) die "Speicher $old_num ist kein lokaler Pfad-Speicher ($old_id) – bitte von Hand prüfen." ;; esac
old_path=${old_id#local::}
if [ -e "$old_path" ]; then
  [ "$(readlink -f "$old_path")" = "$(readlink -f "$datadir")" ] \
    || die "Der alte Pfad $old_path existiert noch und ist ein anderes Verzeichnis – bitte von Hand prüfen."
fi

echo "Einträge je Speicher (Bereich = oberster Ordner):"
sql "SELECT storage, SUBSTRING_INDEX(path,'/',1), COUNT(*) FROM ${prefix}filecache WHERE storage IN ($old_num${new_num:+,$new_num}) GROUP BY storage, SUBSTRING_INDEX(path,'/',1)" \
  | awk -F'\t' '{printf "  Speicher %-4s %-28s %8s\n", $1, ($2 == "" ? "(Wurzel)" : $2), $3}'
shares=$(sql "SELECT COUNT(*) FROM ${prefix}share s JOIN ${prefix}filecache f ON f.fileid = s.file_source WHERE f.storage = ${new_num:-0}")
echo "Freigaben auf Einträgen des doppelten Speichers ${new_num:-–}: $shares (gehen beim Zusammenführen verloren)"
echo

if [ -n "$new_num" ]; then
  echo "Geplant: Speicher $new_num löschen (doppelt, wird von files:cleanup aufgeräumt),"
  echo "         Speicher $old_num umbenennen auf $newid"
else
  echo "Geplant: Speicher $old_num umbenennen auf $newid"
fi
if [ "$CHECK_ONLY" = 1 ]; then echo; echo "Nur Prüfung (--pruefen) – nichts verändert."; exit 0; fi

echo
echo "Vorher unbedingt: Backup der ganzen VM (PBS/Snapshot)."
read -r -p "Zusammenführen? Zum Bestätigen JA eintippen: " answer
[ "$answer" = JA ] || die "Abgebrochen, nichts verändert."

maint_off() { occ maintenance:mode --off >/dev/null 2>&1 || true; }
echo "=== Wartungsmodus ein ==="
occ maintenance:mode --on
trap 'echo "Fehler – Wartungsmodus wird ausgeschaltet."; maint_off' ERR

echo "=== Speicher zusammenführen ==="
stmt="SET autocommit=0; START TRANSACTION;"
[ -n "$new_num" ] && stmt="$stmt DELETE FROM ${prefix}storages WHERE numeric_id = $new_num AND id = '$newid';"
stmt="$stmt UPDATE ${prefix}storages SET id = '$newid' WHERE numeric_id = $old_num AND id = '$old_id'; COMMIT;"
sql "$stmt"
check=$(sql "SELECT numeric_id FROM ${prefix}storages WHERE id = '$newid'")
[ "$check" = "$old_num" ] || { maint_off; die "Umbenennen nicht wirksam (gefunden: '${check}') – bitte melden."; }
echo "Speicher $old_num heißt jetzt $newid."

echo "=== Zwischenspeicher leeren ==="
systemctl restart redis-server 2>/dev/null && echo "redis-server neu gestartet" || echo "redis-server nicht gefunden (in Ordnung, wenn kein Redis)"
for s in $(systemctl list-units --type=service --no-legend 'php*-fpm.service' 2>/dev/null | awk '{print $1}'); do
  systemctl restart "$s" && echo "$s neu gestartet"
done

echo "=== Wartungsmodus aus ==="
trap - ERR
occ maintenance:mode --off

echo "=== Verwaiste Einträge entfernen (files:cleanup) ==="
occ files:cleanup || echo "WARNUNG: files:cleanup meldet einen Fehler – bitte von Hand wiederholen: ncc files:cleanup"

echo "=== Team-Ordner neu einlesen ==="
occ groupfolders:scan --all || echo "WARNUNG: groupfolders:scan meldet einen Fehler."
echo "=== App-Daten neu einlesen ==="
occ files:scan-app-data || echo "WARNUNG: files:scan-app-data meldet einen Fehler."

echo "=== Ergebnis ==="
opt=--dryRun; occ help files:repair-tree 2>/dev/null | grep -q -- '--dryRun' || opt=--dry-run
occ files:repair-tree "$opt"
echo
echo "Fertig. Jetzt testen: im Team-Ordner hochladen und umbenennen, ein anderer Benutzer drückt F5."
echo "Danach erneut prüfen: ncc files:repair-tree $opt   (soll 0 bleiben)"
