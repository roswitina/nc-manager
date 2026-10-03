#!/usr/bin/env python3
# Nextcloud Server Manager
# Copyright (c) 2026 roswitina@hotmail.com
# SPDX-License-Identifier: MIT
# Lizenz: siehe LICENSE · Gewährleistungs- und Haftungsausschluss: siehe HAFTUNGSAUSSCHLUSS.md
"""Nextcloud Server Manager – Web-Oberfläche."""
import json
import os
import re
import secrets
import subprocess
import time
from contextlib import closing
from datetime import timedelta
from functools import wraps

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

import jobs

VERSION = '0.8.0'
AUTHOR = 'roswitina@hotmail.com'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

USER = os.environ.get('NCM_USER', 'admin')
PWHASH = os.environ.get('NCM_PASSWORD_HASH', '')
SECRET = os.environ.get('NCM_SECRET', '')
BEHIND_PROXY = os.environ.get('NCM_BEHIND_PROXY', '0') == '1'
if not PWHASH or not SECRET:
    # Ohne festes Secret hätte jeder Gunicorn-Worker ein eigenes -> zufällige Logouts.
    raise RuntimeError('NCM_PASSWORD_HASH und NCM_SECRET müssen gesetzt sein (/etc/nc-manager.env).')

LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 15 * 60

# Aktionen der Wartungsseite: Schlüssel -> (Beschriftung, Button-Klasse, Rückfrage, Gruppe)
MAINT_GROUPS = ('Datenbank', 'Dateien & Aufräumen', 'Wartungsmodus')
MAINT_ACTIONS = {
    'repair': ('Maintenance Repair', '', None, 'Datenbank'),
    'indices': ('Fehlende DB-Indizes', '', None, 'Datenbank'),
    'columns': ('Fehlende DB-Spalten', '', None, 'Datenbank'),
    'keys': ('Fehlende Primary Keys', '', None, 'Datenbank'),
    'bigint': ('BigInt-Konvertierung', 'warn',
               'Die BigInt-Konvertierung schreibt große Tabellen neu und kann bei großen Instanzen lange dauern. '
               'Am besten vorher ein Backup erstellen und den Wartungsmodus einschalten. Fortfahren?', 'Datenbank'),
    'files_scan': ('Alle Dateien neu einlesen', 'warn',
                   'files:scan --all liest die Dateien aller Benutzer neu ein und kann lange dauern. Fortfahren?',
                   'Dateien & Aufräumen'),
    'files_cleanup': ('Verwaiste Datei-Einträge entfernen', '', None, 'Dateien & Aufräumen'),
    'trashbin_expire': ('Papierkorb: abgelaufene löschen', '', None, 'Dateien & Aufräumen'),
    'versions_expire': ('Versionen: abgelaufene löschen', '', None, 'Dateien & Aufräumen'),
    'trashbin_cleanup': ('Papierkorb ALLER Benutzer leeren', 'danger',
                         'Löscht den kompletten Papierkorb aller Benutzer endgültig. Wirklich fortfahren?',
                         'Dateien & Aufräumen'),
    'versions_cleanup': ('ALLE Dateiversionen löschen', 'danger',
                         'Löscht alle älteren Dateiversionen aller Benutzer endgültig. Wirklich fortfahren?',
                         'Dateien & Aufräumen'),
    'maint_on': ('Wartungsmodus EIN', 'warn',
                 'Wartungsmodus einschalten? Nextcloud ist dann für alle Benutzer gesperrt.', 'Wartungsmodus'),
    'maint_off': ('Wartungsmodus AUS', '', None, 'Wartungsmodus'),
}
ACTION_LABELS = {k: v[0] for k, v in MAINT_ACTIONS.items()}
ACTION_LABELS.update({
    'update_check': 'Nextcloud Update prüfen',
    'nextcloud_update': 'Nextcloud aktualisieren',
    'php_set': 'PHP ändern',
    'fpm_set': 'PHP-FPM-Pool ändern',
    'app_update': 'App aktualisieren',
    'app_update_all': 'Alle Apps aktualisieren',
    'backup': 'Backup erstellen',
    'backup_full': 'Vollbackup erstellen (mit Benutzerdaten)',
    'backup_verify': 'Backup prüfen',
    'restore_backup': 'Backup wiederherstellen',
    'backup_delete': 'Backup löschen',
    'log_archive': 'Nextcloud-Log archivieren und leeren',
    'log_rotate_set': 'Log-Rotation einstellen',
    'log_level_set': 'Log-Level einstellen',
    'php_info': 'PHP-Konfiguration',
    'status': 'Status',
    'preflight': 'Update-Vorprüfung',
    'web_enmod': 'Apache-Modul einschalten',
    'web_reload': 'Webserver neu laden',
    'web_info': 'Webserver-Konfiguration',
    'config_set': 'config.php: Wert setzen',
    'config_reset': 'config.php: Standard wiederherstellen',
})
# Rücksprung von der Job-Seite
ACTION_PAGES = {'php_set': 'php_page', 'fpm_set': 'fpm_page', 'app_update': 'apps_page',
                'app_update_all': 'apps_page', 'backup': 'backups_page', 'backup_delete': 'backups_page',
                'backup_full': 'backups_page', 'backup_verify': 'backups_page', 'restore_backup': 'backups_page',
                'log_archive': 'logs_page', 'log_rotate_set': 'logs_page', 'log_level_set': 'logs_page',
                'nextcloud_update': 'update_wizard', 'update_check': 'update_wizard',
                'web_enmod': 'webserver_page', 'web_reload': 'webserver_page',
                'config_set': 'config_page', 'config_reset': 'config_page'}
# Diese Jobs ändern nichts am Zustand, den Setup-Checks/App-Liste beschreiben.
CACHE_NEUTRAL = {'backup', 'backup_full', 'backup_verify', 'backup_delete', 'update_check', 'log_archive'}
# Kurzzeit-Cache für häufige, langsame Abfragen (Datei-Cache, gilt für alle Gunicorn-Worker).
CALL_CACHE_TTL = {'dashboard': 15, 'php_platform': 60, 'fpm_info': 15}
APP_RE = r'[a-z0-9_]{1,64}'
BACKUP_NAME_RE = r'[0-9]{8}-[0-9]{6}'

# Änderbare PHP-Werte mit Empfehlung, Grundlage und Quelle.
# Der Wrapper prüft das Format identisch nach (php_value_ok).
SIZE = r'[0-9]{1,6}[KMGkmg]?'
DOC = 'https://docs.nextcloud.com/server/latest/admin_manual'
PHP_SOURCES = {
    'php': ('Nextcloud-Doku: PHP-Konfiguration', f'{DOC}/installation/php_configuration.html'),
    'upload': ('Nextcloud-Doku: Große Dateien hochladen', f'{DOC}/configuration_files/big_file_upload_configuration.html'),
    'tuning': ('Nextcloud-Doku: Server-Tuning', f'{DOC}/installation/server_tuning.html'),
}
# Grundlage einer Empfehlung: wie verbindlich sie ist.
PHP_BASIS = {
    'doku': ('Doku', 'Die Nextcloud-Dokumentation nennt diesen Wert ausdrücklich.'),
    'beispiel': ('Doku-Beispiel', 'Beispielwert der Nextcloud-Dokumentation – an die eigenen Dateigrößen anpassen.'),
    'richtwert': ('Richtwert', 'Die Dokumentation nennt keinen festen Wert. Empfohlen wird der PHP-Standard bzw. ein '
                               'verbreiteter Erfahrungswert; Nextcloud meldet einen zu kleinen OPcache selbst unter Prüfungen.'),
}
UPLOAD_NOTE = ('Web-Oberfläche und Desktop-Client laden große Dateien in Teilstücken hoch; das Limit betrifft vor allem '
               'Programme, die ohne Teilstücke hochladen (z.B. manche WebDAV-Clients).')
PHP_SETTINGS = {
    'memory_limit': dict(rx=rf'-1|{SIZE}', hint='z.B. 512M, -1 = unbegrenzt', rec='512M', kind='size', basis='doku',
                         source='php', note='Doku: „mindestens 512MB“; Nextcloud braucht mindestens 128 MB pro Prozess.'),
    'upload_max_filesize': dict(rx=SIZE, hint='z.B. 16G', rec='16G', kind='size', basis='beispiel', source='upload',
                                note=UPLOAD_NOTE),
    'post_max_size': dict(rx=SIZE, hint='mind. so groß wie upload_max_filesize', rec='16G', kind='size',
                          basis='beispiel', source='upload', note='Muss mindestens so groß sein wie upload_max_filesize.'),
    'max_execution_time': dict(rx=r'-1|[0-9]{1,6}', hint='Sekunden, 0 = unbegrenzt', rec='3600', kind='time',
                               basis='beispiel', source='upload', note='Für lange Uploads und Vorgänge.'),
    'max_input_time': dict(rx=r'-1|[0-9]{1,6}', hint='Sekunden, -1 = wie max_execution_time', rec='3600', kind='time',
                           basis='beispiel', source='upload', note='Zeit zum Empfangen der Upload-Daten.'),
    'output_buffering': dict(rx=r'[0-9]{1,7}', hint='0 = aus', rec='0', kind='zero', basis='doku', source='upload',
                             note='Doku: muss aus sein, sonst drohen Speicherfehler. Nextcloud setzt es selbst in '
                                  '.user.ini bzw. .htaccess.'),
    'opcache.memory_consumption': dict(rx=r'[0-9]{1,7}', hint='MB', rec='128', kind='int', basis='richtwert',
                                       source='tuning', note='PHP-Standardwert.'),
    'opcache.interned_strings_buffer': dict(rx=r'[0-9]{1,7}', hint='MB', rec='16', kind='int', basis='richtwert',
                                            source='tuning', note='PHP-Standard ist 8; 16 ist der übliche Wert gegen '
                                                                  'die Warnung „interned strings buffer nearly full“.'),
    'opcache.max_accelerated_files': dict(rx=r'[0-9]{1,7}', hint='Anzahl Dateien', rec='10000', kind='int',
                                          basis='richtwert', source='tuning', note='PHP-Standardwert.'),
}
# Diese Werte darf eine .user.ini (PHP-FPM) bzw. .htaccess (mod_php) im Nextcloud-Ordner überschreiben;
# OPcache-Werte nicht (PHP_INI_SYSTEM).
PHP_PERDIR = {'memory_limit', 'upload_max_filesize', 'post_max_size', 'max_execution_time', 'max_input_time',
              'output_buffering'}


def php_number(value, kind):
    """Vergleichbare Zahl; float('inf') für 'unbegrenzt', None wenn unbekannt."""
    v = str(value or '').strip()
    if kind == 'size':
        m = re.fullmatch(r'(-1|[0-9]+)([KMGkmg]?)', v)
        if not m:
            return None
        if m.group(1) == '-1':
            return float('inf')
        return int(m.group(1)) * {'': 1, 'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3}[m.group(2).upper()]
    if not re.fullmatch(r'-?[0-9]+', v):
        return None
    n = int(v)
    if kind == 'time' and n <= 0:      # 0 bzw. -1 = keine Begrenzung
        return float('inf')
    return n


def php_assessment(key, value):
    """'ok', 'low' (zu niedrig), 'bad' (nicht empfohlen) oder None (nicht bewertbar)."""
    kind, rec = PHP_SETTINGS[key]['kind'], PHP_SETTINGS[key]['rec']
    if kind == 'zero':
        return 'ok' if str(value).strip().lower() in ('0', 'off', 'no', 'false') else 'bad'
    cur, want = php_number(value, kind), php_number(rec, kind)
    if cur is None or want is None:
        return None
    return 'ok' if cur >= want else 'low'


def php_web_value(key, cli, fpm, has_fpm, web_sapi, overrides):
    """Wert, den Webanfragen tatsächlich sehen, und woher er kommt."""
    ov_user = (overrides.get('user_ini') or {}).get(key)
    ov_ht = (overrides.get('htaccess') or {}).get(key)
    if has_fpm:
        if key in PHP_PERDIR and ov_user not in (None, ''):
            return ov_user, '.user.ini'
        return fpm, 'PHP-FPM'
    if web_sapi == 'apache2-mod_php':
        if key in PHP_PERDIR and ov_ht not in (None, ''):
            return ov_ht, '.htaccess'
        return cli, 'CLI (mod_php vermutlich gleich)'
    return cli, "CLI"


PHP_LABELS = {
    'opcache.enable': 'OPcache aktiv', 'opcache.memory_consumption': 'OPcache Speicher',
    'opcache.interned_strings_buffer': 'OPcache interned strings',
    'opcache.max_accelerated_files': 'OPcache Dateien', 'opcache.save_comments': 'OPcache Kommentare',
    'opcache.revalidate_freq': 'OPcache revalidate', 'apc.enabled': 'APCu aktiv',
    'apc.shm_size': 'APCu Speicher', 'session.save_handler': 'Session Handler',
    'session.save_path': 'Session Pfad',
}

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Strict',
    SESSION_COOKIE_SECURE=BEHIND_PROXY,      # nur sinnvoll, wenn TLS davor sitzt
    SESSION_COOKIE_NAME='ncm_session',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),   # Leerlauf-Timeout
    MAX_CONTENT_LENGTH=64 * 1024,
)
if BEHIND_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# --------------------------------------------------------------------------- Hilfen

def call(action, *args, timeout=120):
    """Kurze, nur lesende Wrapper-Abfrage. Wird nicht protokolliert."""
    try:
        p = subprocess.run([*jobs.SUDO, jobs.WRAPPER, action, *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, '', f'Zeitüberschreitung nach {timeout} s'
    except OSError as e:
        return 255, '', f'FEHLER: {e}'


def call_json_args(action, *args):
    rc, out, err = call(action, *args)
    if rc == 0:
        try:
            return json.loads(out), ''
        except ValueError:
            pass
    return {}, (out + '\n' + err).strip() or f'Exit-Code {rc}'


def call_json(action):
    rc, out, err = call(action)
    if rc == 0:
        try:
            return json.loads(out), ''
        except ValueError:
            pass
    return {}, (out + '\n' + err).strip() or f'Exit-Code {rc}'


def call_cached_json(action):
    data, ts = jobs.cache_get(f'call-{action}')
    if data is not None and time.time() - ts < CALL_CACHE_TTL[action]:
        return data, ''
    data, err = call_json(action)
    if data:
        jobs.cache_set(f'call-{action}', data)
    return data, err


def as_bool(v):
    return v is True or str(v).lower() in ('true', '1', 'yes')


def human_kb(kb):
    try:
        n = float(kb) * 1024
    except (TypeError, ValueError):
        return '–'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f} {unit}'
        n /= 1024


def df_text(s):
    try:
        total, used, free, mount = (s or '').split('|', 3)
    except ValueError:
        return 'nicht ermittelt'
    return f'{human_kb(total)} gesamt · {human_kb(used)} belegt · {human_kb(free)} frei · {mount}'


def ago(ts):
    try:
        sec = int(time.time() - int(ts))
    except (TypeError, ValueError):
        return '–'
    if sec < 120:
        return f'vor {sec} s'
    if sec < 7200:
        return f'vor {sec // 60} min'
    if sec < 172800:
        return f'vor {sec // 3600} h'
    return f'vor {sec // 86400} Tagen'


app.jinja_env.filters.update(
    kb=human_kb, df=df_text, ago=ago, bytes=lambda b: human_kb((b or 0) / 1024),
    dt=lambda ts: time.strftime('%d.%m.%Y %H:%M:%S', time.localtime(ts)),
    label=lambda a: ACTION_LABELS.get(a, a),
)


def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_urlsafe(32)
    return session['_csrf']


app.jinja_env.globals.update(csrf_token=csrf_token, VERSION=VERSION, AUTHOR=AUTHOR)


@app.before_request
def check_csrf():
    if request.method == 'POST':
        expected = session.get('_csrf')
        sent = request.form.get('_csrf', '')
        if not expected or not secrets.compare_digest(sent, expected):
            return render_template('message.html', title='Sitzung abgelaufen',
                                   text='Das Formular ist ungültig oder abgelaufen. Bitte die Seite neu laden.',
                                   bad=True), 400


@app.after_request
def security_headers(resp):
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    if resp.mimetype in ('text/html', 'application/json'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if session.get('auth') is not True:
            if request.path.endswith('/status'):
                return jsonify(error='login'), 401
            return redirect(url_for('login'))
        return view(*a, **kw)
    return wrapper


def start(action, args=()):
    """Job starten; Caches verwerfen, die danach veraltet sein könnten."""
    jid, busy = jobs.start_job(action, args)
    if jid and action not in CACHE_NEUTRAL:
        jobs.cache_clear('setupchecks')
        jobs.cache_clear('apps')
        for a in CALL_CACHE_TTL:
            jobs.cache_clear(f'call-{a}')
    return job_started(jid, busy)


def job_started(jid, busy):
    if jid:
        return redirect(url_for('job_view', jid=jid))
    return render_template('message.html', title='Bitte warten',
                           text='Es läuft bereits eine Aktion. Neue Aktionen sind erst nach deren Ende möglich.',
                           job_id=busy, bad=True), 409


# --------------------------------------------------------------------------- Login

def _login_failures(ip):
    with closing(jobs.db()) as c:
        c.execute('DELETE FROM login_fail WHERE ts < ?', (int(time.time()) - LOGIN_WINDOW,))
        return c.execute('SELECT COUNT(*) FROM login_fail WHERE ip=?', (ip,)).fetchone()[0]


@app.route('/login', methods=['GET', 'POST'])
def login():
    err = ''
    if request.method == 'POST':
        ip = request.remote_addr or '?'
        if _login_failures(ip) >= LOGIN_MAX_FAILS:
            err = f'Zu viele Fehlversuche. Bitte {LOGIN_WINDOW // 60} Minuten warten.'
        else:
            # Hash immer prüfen, damit die Antwortzeit nichts über den Benutzernamen verrät.
            pw_ok = check_password_hash(PWHASH, request.form.get('password', ''))
            user_ok = secrets.compare_digest(request.form.get('user', ''), USER)
            if pw_ok and user_ok:
                with closing(jobs.db()) as c:
                    c.execute('DELETE FROM login_fail WHERE ip=?', (ip,))
                session.clear()
                session.permanent = True
                session['auth'] = True
                return redirect(url_for('home'))
            with closing(jobs.db()) as c:
                c.execute('INSERT INTO login_fail(ip, ts) VALUES(?, ?)', (ip, int(time.time())))
            app.logger.warning('Fehlgeschlagene Anmeldung von %s', ip)
            err = 'Anmeldung fehlgeschlagen.'
    return render_template('login.html', err=err)


@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# --------------------------------------------------------------------------- Seiten

@app.get('/')
@login_required
def home():
    x, err = call_cached_json('dashboard')
    if not x:
        return render_template('message.html', title='Dashboard-Fehler', text='Der Wrapper lieferte keine Daten.',
                               output=err, bad=True), 502
    px, _ = call_cached_json('php_platform')
    st = x.get('status') or {}
    cfg = x.get('config') or {}
    core = x.get('core') or {}
    core = (core.get('apps') or {}).get('core', core) if isinstance(core, dict) else {}
    maint = as_bool(st.get('maintenance'))
    dbup = as_bool(st.get('needsDbUpgrade'))
    cron_mode = core.get('backgroundjobs_mode') or '–'
    lastcron = core.get('lastcron')
    cron_stale = cron_mode == 'cron' and lastcron and time.time() - int(lastcron) > 3600
    health = [
        ('Nextcloud erreichbar', bool(st.get('versionstring'))),
        ('Wartungsmodus aus', not maint),
        ('Kein DB-Upgrade erforderlich', not dbup),
        ('PHP-FPM aktiv', x.get('php_fpm') == 'active'),
        ('Redis aktiv', x.get('redis') == 'active'),
        ('Hintergrundjobs laufen', bool(lastcron) and not cron_stale),
    ]
    mem_total = int(x.get('mem_total_kb') or 0)
    mem_avail = int(x.get('mem_available_kb') or 0)

    # Betrieb: zwischengespeicherte Prüfungen/App-Updates, Backups, FPM-Auslastung
    checks, checks_ts = jobs.cache_get('setupchecks')
    apps, apps_ts = jobs.cache_get('apps')
    bl, _ = call_json('backup_list')
    last_backup = next((b for b in bl.get('backups', []) if b.get('complete')), None)
    last_backup_ts = time.mktime(time.strptime(last_backup['name'], '%Y%m%d-%H%M%S')) if last_backup else None
    fi, _ = call_cached_json('fpm_info')
    fpm_hits = sum(v.get('max_children_hits', 0) for v in (fi.get('versions') or {}).values())
    if fi.get('versions'):
        health.append(('PHP-FPM-Limit nicht erreicht', fpm_hits == 0))
    summary = checks_summary(checks)
    if summary is not None:
        health.append(('Nextcloud-Prüfungen ohne Fehler', not summary.get('error')))
    return render_template(
        'dashboard.html', x=x, px=px, st=st, cfg=cfg, maint=maint, dbup=dbup,
        cron_mode=cron_mode, lastcron=lastcron, health=health,
        dietpi=as_bool(x.get('dietpi')), mem_used=mem_total - mem_avail, mem_total=mem_total,
        checks_summary=summary, checks_error=(checks or {}).get('error'), checks_ts=checks_ts,
        apps=apps, apps_ts=apps_ts, last_backup=last_backup, last_backup_ts=last_backup_ts,
        backup_count=len(bl.get('backups', [])), fpm_hits=fpm_hits, has_fpm=bool(fi.get('versions')),
        running=jobs.running_job_id())


@app.get('/maintenance')
@login_required
def maintenance():
    return render_template('maintenance.html', actions=MAINT_ACTIONS, groups=MAINT_GROUPS,
                           running=jobs.running_job_id())


@app.post('/run/<action>')
@login_required
def run_action(action):
    # Bewusst nur die Wartungsaktionen. Update und PHP haben eigene, geprüfte Routen.
    if action not in MAINT_ACTIONS:
        abort(404)
    return start(action)


@app.get('/update')
@login_required
def update_wizard():
    rc, out, err = call('preflight', timeout=300)
    return render_template('update.html', ok=rc == 0, output=(out + err).strip(), running=jobs.running_job_id())


@app.post('/update/check')
@login_required
def update_check():
    return start('update_check')


@app.post('/update/start')
@login_required
def update_start():
    if request.form.get('backup') != 'yes':
        return render_template('message.html', title='Bestätigung fehlt',
                               text='Bitte bestätigen, dass ein aktuelles Backup/Snapshot existiert.', bad=True), 400
    flags = [f for f in ('backup', 'apps') if request.form.get(f'with_{f}') == 'yes']
    return start('nextcloud_update', flags)


@app.get('/php')
@login_required
def php_page():
    p, _ = call_json('php_platform')
    v, _ = call_json('php_values')
    cli_vals, fpm_vals = v.get('cli') or {}, v.get('fpm') or {}
    has_fpm = bool(v.get('fpm_service'))
    rows = []
    for k in v.get('keys', []):
        cv, fv = cli_vals.get(k), fpm_vals.get(k)
        cv = '–' if cv in (None, '') else str(cv)
        fv = '–' if fv in (None, '') else str(fv)
        rows.append((PHP_LABELS.get(k, k), cv, fv, cv != fv))
    # Pro änderbarer Einstellung: wirksamer Web-Wert, Empfehlung, Grundlage, Bewertung.
    overrides = v.get('overrides') or {}
    web_sapi = p.get('web_sapi', '')
    current = {}
    for k, cfg in PHP_SETTINGS.items():
        cv, fv = cli_vals.get(k) or '', fpm_vals.get(k) or ''
        web, origin = php_web_value(k, cv, fv, has_fpm, web_sapi, overrides)
        if k == 'max_execution_time' and origin.startswith('CLI'):
            web = ''          # CLI meldet immer 0 – keine Aussage über den Webserver möglich
        current[k] = {'cli': cv, 'fpm': fv, 'web': web, 'origin': origin, 'base': fv if has_fpm else cv,
                      'rec': cfg['rec'], 'hint': cfg['hint'], 'basis': cfg['basis'], 'note': cfg['note'],
                      'source': PHP_SOURCES[cfg['source']], 'overridden': origin in ('.user.ini', '.htaccess'),
                      'state': php_assessment(k, web) if web != '' else None}
    warnings = []
    up = php_number(current['upload_max_filesize']['web'], 'size')
    post = php_number(current['post_max_size']['web'], 'size')
    if up and post and post < up:
        warnings.append('post_max_size ist kleiner als upload_max_filesize – große Uploads scheitern dann am kleineren Wert.')
    # Nur warnen, wenn die Überschreibung einen nicht empfohlenen Wert erzwingt (output_buffering=0 ist gewollt).
    over = [k for k, c in current.items() if c['overridden'] and c['web'] != c['base'] and c['state'] in ('low', 'bad')]
    if over:
        src = overrides.get('user_ini_path') if has_fpm else overrides.get('htaccess_path')
        warnings.append(f'{", ".join(over)}: Der Wert aus {src} überstimmt die PHP-Konfiguration für Webanfragen. '
                        'Eine Änderung hier wirkt dort erst, wenn die Zeile in dieser Datei angepasst oder entfernt wird. '
                        'Nextcloud ersetzt die Datei bei Updates – der Manager ändert sie deshalb bewusst nicht.')
    low = [k for k, c in current.items() if c['state'] in ('low', 'bad')]
    return render_template('php.html', p=p, v=v, rows=rows, current=current, basis=PHP_BASIS,
                           overrides=overrides, has_fpm=has_fpm, low=low, warnings=warnings,
                           running=jobs.running_job_id())


@app.post('/php/set')
@login_required
def php_set():
    key = request.form.get('key', '')
    value = request.form.get('value', '').strip()
    if key not in PHP_SETTINGS or not re.fullmatch(PHP_SETTINGS[key]['rx'], value):
        hint = PHP_SETTINGS[key]['hint'] if key in PHP_SETTINGS else 'unbekannte Einstellung'
        return render_template('message.html', title='Ungültiger Wert',
                               text=f'„{value}“ ist für {key} nicht zulässig ({hint}).', bad=True), 400
    return start('php_set', [key, value])


# --------------------------------------------------------------------------- Webserver (prüfen und erklären)

WEB_SOURCES = {
    'apache': ('Nextcloud-Doku: Apache-Konfiguration',
               f'{DOC}/installation/source_installation.html#apache-web-server-configuration'),
    'apache_more': ('Nextcloud-Doku: Weitere Apache-Einstellungen',
                    f'{DOC}/installation/source_installation.html#additional-apache-configurations'),
    'pretty': ('Nextcloud-Doku: Pretty URLs', f'{DOC}/installation/source_installation.html#pretty-urls'),
    'nginx': ('Nextcloud-Doku: nginx-Konfiguration', f'{DOC}/installation/nginx.html'),
    'upload': PHP_SOURCES['upload'],
    'harden': ('Nextcloud-Doku: Server absichern (HSTS)', f'{DOC}/installation/harden_server.html'),
    'wellknown': ('Nextcloud-Doku: Service Discovery', f'{DOC}/issues/general_troubleshooting.html#service-discovery'),
}
WEB_MODULES = ('rewrite', 'headers', 'env', 'dir', 'mime', 'setenvif')     # identisch im Wrapper
HSTS_MIN = 15552000                    # 180 Tage – Mindestwert der Nextcloud-Prüfung
NGINX_HEADERS = {'referrer-policy': 'no-referrer', 'x-content-type-options': 'nosniff',
                 'x-frame-options': 'SAMEORIGIN', 'x-permitted-cross-domain-policies': 'none',
                 'x-robots-tag': 'noindex, nofollow'}


def web_size(v):
    """nginx-Größe (512M, 1g, 0) bzw. Bytes in Bytes; None bei unlesbarem Wert."""
    m = re.fullmatch(r'\s*(\d+)\s*([kKmMgG]?)\s*', str(v or ''))
    if not m:
        return None
    return int(m.group(1)) * {'': 1, 'k': 1024, 'm': 1024 ** 2, 'g': 1024 ** 3}[m.group(2).lower()]


def web_seconds(v):
    m = re.fullmatch(r'\s*(\d+)\s*(ms|s|m|h|d)?\s*', str(v or ''))
    if not m:
        return None
    return int(m.group(1)) * {None: 1, 'ms': 0.001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[m.group(2)]


def _hsts_age(v):
    m = re.search(r'max-age\s*=\s*(\d+)', str(v or ''), re.I)
    return int(m.group(1)) if m else None


def _row(title, current, rec, state, basis, source, note='', fix=None, where='', action=None):
    return dict(title=title, current=current, rec=rec, state=state, basis=basis, source=WEB_SOURCES[source],
                note=note, fix=fix, where=where, action=action)


def _hsts_row(value, src, ssl, inactive_src=''):
    if not ssl:
        return _row('HSTS (Strict-Transport-Security)', 'kein HTTPS-Bereich erkannt', f'max-age ≥ {HSTS_MIN}', 'info',
                    'doku', 'harden', 'HSTS gehört in den HTTPS-Bereich – oder in den Reverse-Proxy, falls '
                    'HTTPS dort endet. Nextcloud prüft den Header unter „Prüfungen“.')
    age = _hsts_age(value)
    if age is None:
        note = 'Ohne HSTS meldet Nextcloud eine Warnung unter „Prüfungen“.'
        if inactive_src:
            note = (f'Der Header steht in {inactive_src}, wirkt aber nicht: Er liegt in einem <IfModule>-Abschnitt, '
                    'dessen Modul nicht geladen ist (meist mod_headers). ' + note)
        return _row('HSTS (Strict-Transport-Security)', 'nicht gesetzt', f'max-age ≥ {HSTS_MIN}', 'warn', 'doku',
                    'harden', note)
    return _row('HSTS (Strict-Transport-Security)', f'max-age={age}', f'max-age ≥ {HSTS_MIN}',
                'ok' if age >= HSTS_MIN else 'warn', 'doku', 'harden',
                f'Gefunden in {src}.' + ('' if age >= HSTS_MIN else ' Nextcloud verlangt mindestens 180 Tage.'))


def web_apache_checks(a, nc_cfg, nc_path):
    rows = []
    mods = set(a.get('modules') or [])
    vfile = ((a.get('primary') or {}).get('src') or '').rsplit(':', 1)[0]
    where = f'in den VirtualHost der Nextcloud ({vfile})' if vfile else 'in den VirtualHost der Nextcloud'
    fpm = 'fpm' in (a.get('php_handlers') or [])
    for mod, need, note in [
            ('rewrite', 'bad', 'Pflicht: ohne mod_rewrite funktionieren die Regeln aus Nextclouds .htaccess nicht '
                               '(u. a. .well-known, Pretty URLs).'),
            ('headers', 'warn', 'Empfohlen: setzt Sicherheits-Header aus der .htaccess und HSTS.'),
            ('env', 'warn', 'Empfohlen: nötig für Pretty URLs.'),
            ('dir', 'warn', 'Empfohlen.'),
            ('mime', 'warn', 'Empfohlen: richtige MIME-Typen, u. a. für .mjs-Dateien.'),
            ('setenvif', 'warn' if fpm else 'info', 'Empfohlen bei PHP-FPM (mod_proxy_fcgi).')]:
        have = f'{mod}_module' in mods
        rows.append(_row(f'Modul mod_{mod}', 'geladen' if have else 'fehlt', 'geladen', 'ok' if have else need,
                         'doku', 'apache_more' if mod != 'rewrite' else 'apache', note,
                         action=None if have else ('enmod', mod)))
    if not a.get('primary') and not a.get('mode'):
        return rows
    ao = a.get('allow_override') or {}
    ok = 'all' in ao.get('value', '').lower().split()
    rows.append(_row('AllowOverride für den Nextcloud-Ordner', ao.get('value', '–'), 'All', 'ok' if ok else 'bad', 'doku',
                     'apache', f'Quelle: {ao.get("src", "–")}. Ohne „All“ ignoriert Apache Nextclouds .htaccess '
                     '(Sicherheitsregeln, Weiterleitungen, Header).',
                     fix=f'<Directory {nc_path}/>\n  Require all granted\n  AllowOverride All\n  Options FollowSymLinks MultiViews\n'
                         '  <IfModule mod_dav.c>\n    Dav off\n  </IfModule>\n</Directory>', where=where))
    if a.get('dav_loaded'):
        rows.append(_row('WebDAV von Apache (Dav off)', 'aus' if a.get('dav_off') else 'nicht abgeschaltet', 'Dav off',
                         'ok' if a.get('dav_off') else 'warn', 'doku', 'apache',
                         'mod_dav ist geladen. Für den Nextcloud-Ordner muss es aus sein, Nextcloud bringt eigenes WebDAV mit.',
                         fix='<IfModule mod_dav.c>\n  Dav off\n</IfModule>', where=f'in den <Directory {nc_path}/>-Abschnitt'))
    p = a.get('primary') or {}
    hs = a.get('hsts') or {}
    rows.append(_hsts_row(hs.get('value'), hs.get('src', ''), p.get('ssl'), a.get('hsts_inactive', '')))
    if rows[-1]['state'] == 'warn':
        rows[-1].update(fix='<IfModule mod_headers.c>\n  Header always set Strict-Transport-Security '
                            f'"max-age={HSTS_MIN}; includeSubDomains"\n</IfModule>',
                        where='in den HTTPS-VirtualHost (Port 443) der Nextcloud')
    wk = a.get('wellknown') or {}
    if a.get('mode') == 'webroot':
        ok = ok and 'rewrite_module' in mods
        rows.append(_row('Weiterleitungen /.well-known (CalDAV/CardDAV)', 'über Nextclouds .htaccess' if ok
                         else '.htaccess wirkt nicht', 'aktiv', 'ok' if ok else 'warn', 'doku', 'wellknown',
                         'Nextcloud liegt im Wurzelverzeichnis: die .htaccess übernimmt die Weiterleitungen, '
                         'sofern AllowOverride All und mod_rewrite aktiv sind.'))
    else:
        base = (a.get('url_path') or '/nextcloud').rstrip('/')
        ok = bool(wk.get('carddav') and wk.get('caldav'))
        rows.append(_row('Weiterleitungen /.well-known (CalDAV/CardDAV)', 'vorhanden' if ok else 'fehlen', 'vorhanden',
                         'ok' if ok else 'warn', 'doku', 'wellknown',
                         f'Nextcloud liegt unter {base}/. Die Weiterleitungen müssen dann im Wurzelverzeichnis des '
                         'Webservers stehen, sonst finden Kalender- und Kontakt-Apps den Server nicht.',
                         fix='<IfModule mod_rewrite.c>\n  RewriteEngine on\n'
                             f'  RewriteRule ^/\\.well-known/carddav {base}/remote.php/dav [R=301,L]\n'
                             f'  RewriteRule ^/\\.well-known/caldav {base}/remote.php/dav [R=301,L]\n</IfModule>',
                         where='in den VirtualHost, der das Wurzelverzeichnis ausliefert'))
    rb = (nc_cfg or {}).get('htaccess.RewriteBase')
    rows.append(_row('Pretty URLs (ohne index.php)', f'htaccess.RewriteBase = {rb}' if rb else 'nicht eingerichtet',
                     'eingerichtet', 'ok' if rb else 'info', 'doku', 'pretty',
                     'Optional, aber empfohlen: Adressen ohne „/index.php/“. Braucht mod_env und mod_rewrite.',
                     fix=None if rb else f"'htaccess.RewriteBase' => '{(a.get('url_path') or '/').rstrip('/') or '/'}',\n"
                                         "// danach: occ maintenance:update:htaccess ausführen",
                     where='in config.php (in das $CONFIG-Array)' if not rb else ''))
    lrb = a.get('limit_request_body')
    if lrb:
        cur = 'unbegrenzt' if lrb['value'] == '0' else f'{int(lrb["value"]) // 1048576} MiB'
        note = f'Gesetzt in {lrb["src"]}.'
    else:
        cur, note = 'Apache-Standard (1 GiB ab 2.4.54)', 'Nicht ausdrücklich gesetzt.'
    rows.append(_row('LimitRequestBody (max. Anfragegröße)', cur, 'groß genug für Uploads ohne Teilstücke', 'info',
                     'doku', 'upload', note + ' Betrifft nur Programme, die große Dateien in einem Stück hochladen.'))
    if a.get('reqtimeout'):
        rows.append(_row('mod_reqtimeout', 'geladen', 'bei Upload-Abbrüchen anpassen', 'info', 'doku', 'upload',
                         'Kann sehr große Uploads abbrechen. Nur bei Problemen RequestReadTimeout erhöhen oder das Modul abschalten.'))
    return rows


def web_nginx_checks(n, nc_cfg, nc_path):
    rows = []
    pfile = (n.get('primary') or {}).get('file', '')
    where = f'in den server-Block der Nextcloud ({pfile})' if pfile else 'in den server-Block der Nextcloud'
    php_where = 'in den location-Block für PHP (location ~ \\.php…)'

    def val(k):
        return (n.get(k) or {}).get('value')

    def src(k):
        return (n.get(k) or {}).get('src', '')

    cmb = val('client_max_body_size')
    size = web_size(cmb) if cmb else 1024 ** 2
    st = 'ok' if size == 0 or (size or 0) >= 512 * 1024 ** 2 else ('bad' if (size or 0) < 100 * 1024 ** 2 else 'warn')
    rows.append(_row('client_max_body_size', cmb or 'nicht gesetzt (Standard 1m)', '512M', st, 'beispiel', 'nginx',
                     'Maximale Größe einer Anfrage. Der nginx-Standard von 1 MB lässt größere Uploads scheitern.'
                     + (f' Quelle: {src("client_max_body_size")}.' if cmb else ''),
                     fix=None if st == 'ok' else 'client_max_body_size 512M;', where=where))
    cbt = val('client_body_timeout')
    secs = web_seconds(cbt) if cbt else 60
    st = 'ok' if (secs or 0) >= 300 else 'warn'
    rows.append(_row('client_body_timeout', cbt or 'nicht gesetzt (Standard 60s)', '300s', st, 'beispiel', 'nginx',
                     'Wartezeit beim Empfang großer Uploads.', fix=None if st == 'ok' else 'client_body_timeout 300s;',
                     where=where))
    fb = val('fastcgi_buffers')
    rows.append(_row('fastcgi_buffers', fb or 'nicht gesetzt', '64 4K', 'ok' if fb else 'warn', 'beispiel', 'nginx',
                     'Puffer für Antworten von PHP-FPM.', fix=None if fb else 'fastcgi_buffers 64 4K;', where=where))
    mjs = (n.get('types') or {}).get('mjs', '')
    ok = mjs in ('text/javascript', 'application/javascript')
    rows.append(_row('MIME-Typ für .mjs', mjs or 'nicht gesetzt', 'text/javascript', 'ok' if ok else 'bad', 'doku', 'nginx',
                     'Ohne passenden Typ lädt der Browser JavaScript-Module nicht; Nextcloud meldet das unter „Prüfungen“.',
                     fix=None if ok else 'include mime.types;\ntypes {\n    text/javascript mjs;\n    application/wasm wasm;\n}',
                     where=where))
    wasm = (n.get('types') or {}).get('wasm', '')
    rows.append(_row('MIME-Typ für .wasm', wasm or 'nicht gesetzt', 'application/wasm', 'ok' if wasm else 'warn', 'doku',
                     'nginx', 'Für WebAssembly-Dateien einiger Apps.'))
    wk = n.get('wellknown') or {}
    ok = bool(wk.get('carddav') and wk.get('caldav'))
    rows.append(_row('Weiterleitungen /.well-known (CalDAV/CardDAV)', 'vorhanden' if ok else 'fehlen', 'vorhanden',
                     'ok' if ok else 'warn', 'doku', 'nginx',
                     'Damit Kalender- und Kontakt-Apps den Server finden. nginx liest keine .htaccess – die Regeln '
                     'müssen in der nginx-Konfiguration stehen.',
                     fix=None if ok else 'location ^~ /.well-known {\n    location = /.well-known/carddav { return 301 /remote.php/dav/; }\n'
                                         '    location = /.well-known/caldav  { return 301 /remote.php/dav/; }\n'
                                         '    location /.well-known/acme-challenge    { try_files $uri $uri/ =404; }\n'
                                         '    location /.well-known/pki-validation    { try_files $uri $uri/ =404; }\n'
                                         '    return 301 /index.php$request_uri;\n}', where=where))
    hp = n.get('hidden_paths')
    rows.append(_row('Interne Ordner gesperrt (config, data, lib …)', 'gesperrt' if hp else 'keine Sperrregel gefunden',
                     'gesperrt', 'ok' if hp else 'bad', 'doku', 'nginx',
                     'Sicherheit: Ordner wie config/ und data/ dürfen nie direkt abrufbar sein.' + (f' Quelle: {hp}.' if hp else ''),
                     fix=None if hp else 'location ~ ^/(?:build|tests|config|lib|3rdparty|templates|data)(?:$|/)  { return 404; }\n'
                                         'location ~ ^/(?:\\.|autotest|occ|issue|indie|db_|console)                { return 404; }',
                     where=where))
    hdr = n.get('headers') or {}
    missing = [k for k in NGINX_HEADERS if k not in hdr]
    rows.append(_row('Sicherheits-Header', 'vollständig' if not missing else f'fehlen: {", ".join(missing)}',
                     'alle 5 gesetzt', 'ok' if not missing else 'warn', 'doku', 'nginx',
                     'Diese Header setzt bei Apache die .htaccess; bei nginx muss die Konfiguration sie liefern.'
                     + (' Achtung: Der PHP-location-Block setzt eigene add_header-Zeilen – dort gelten die des server-Blocks dann nicht.'
                        if n.get('php_location_own_headers') else ''),
                     fix=None if not missing else '\n'.join(
                         f'add_header {k.title()} "{v}" always;' for k, v in NGINX_HEADERS.items() if k in missing),
                     where=where))
    p = n.get('primary') or {}
    rows.append(_hsts_row(hdr.get('strict-transport-security'), n.get('headers_src', ''), p.get('ssl')))
    if rows[-1]['state'] == 'warn':
        rows[-1].update(fix=f'add_header Strict-Transport-Security "max-age={HSTS_MIN}; includeSubDomains" always;',
                        where=where + ', HTTPS-Teil')
    fc = (n.get('front_controller') or {}).get('value')
    rows.append(_row('Pretty URLs (front_controller_active)', fc or 'nicht gesetzt', 'true', 'ok' if fc == 'true' else 'info',
                     'doku', 'nginx', 'Optional: Adressen ohne „/index.php/“.',
                     fix=None if fc == 'true' else 'fastcgi_param front_controller_active true;', where=php_where))
    rows.append(_row('X-Powered-By ausblenden', 'ja' if n.get('hide_powered_by') else 'nein', 'ja',
                     'ok' if n.get('hide_powered_by') else 'warn', 'doku', 'nginx', 'Verrät sonst die PHP-Version.',
                     fix=None if n.get('hide_powered_by') else 'fastcgi_hide_header X-Powered-By;', where=where))
    if n.get('php_location'):
        ok = n.get('try_files_php')
        rows.append(_row('Nicht vorhandene PHP-Dateien abweisen', 'ja' if ok else 'nein', 'ja', 'ok' if ok else 'warn',
                         'doku', 'nginx', 'Verhindert, dass beliebige Pfade an PHP-FPM gehen (bekanntes Sicherheitsrisiko).',
                         fix=None if ok else 'try_files $fastcgi_script_name =404;', where=php_where))
    gz = val('gzip')
    rows.append(_row('gzip', gz or 'aus (Standard)', 'on', 'ok' if gz == 'on' else 'warn', 'doku', 'nginx',
                     'Komprimiert Textdateien und beschleunigt die Oberfläche.', fix=None if gz == 'on' else 'gzip on;\ngzip_vary on;',
                     where=where))
    stk = val('server_tokens')
    rows.append(_row('server_tokens', stk or 'on (Standard)', 'off', 'ok' if stk == 'off' else 'warn', 'doku', 'nginx',
                     'Blendet die nginx-Version in Fehlerseiten und Headern aus.',
                     fix=None if stk == 'off' else 'server_tokens off;', where=where))
    frt = val('fastcgi_read_timeout')
    rows.append(_row('fastcgi_read_timeout', frt or 'nicht gesetzt (Standard 60s)', 'nur bei 504-Fehlern erhöhen', 'info',
                     'doku', 'upload', 'Häufige Lösung für „504 Gateway Timeout“ bei langen Vorgängen.'))
    frb = val('fastcgi_request_buffering')
    rows.append(_row('fastcgi_request_buffering', frb or 'nicht gesetzt (Standard on)', 'on', 'info', 'doku', 'nginx',
                     'Die aktuelle Beispielkonfiguration setzt „on“, weil PHP-FPM keine Chunked-Übertragung unterstützt.'))
    return rows


def web_summary(rows):
    return {s: sum(1 for r in rows if r['state'] == s) for s in ('ok', 'warn', 'bad', 'info')}


@app.get('/webserver')
@login_required
def webserver_page():
    data, err = call_json('web_info')
    web = data.get('web') or {}
    nc_cfg = data.get('nc') or {}
    nc_path = data.get('nc_path') or '/var/www/nextcloud'
    cards = []
    for key, label, fn in (('apache', 'Apache', web_apache_checks), ('nginx', 'nginx', web_nginx_checks)):
        f = web.get(key)
        if not f:
            continue
        rows = [] if f.get('error') else fn(f, nc_cfg, nc_path)
        cards.append({'key': key, 'service': 'apache2' if key == 'apache' else 'nginx', 'label': label, 'f': f,
                      'rows': rows, 'sum': web_summary(rows), 'found': bool(f.get('primary') or f.get('mode'))})
    return render_template('webserver.html', cards=cards, err=err, servers=web.get('servers') or [], basis=PHP_BASIS,
                           nc_path=nc_path,
                           running=jobs.running_job_id())


@app.post('/webserver/enmod')
@login_required
def webserver_enmod():
    mod = request.form.get('module', '')
    if mod not in WEB_MODULES:
        abort(400)
    return start('web_enmod', [mod])


@app.post('/webserver/reload')
@login_required
def webserver_reload():
    srv = request.form.get('server', '')
    if srv not in ('apache2', 'nginx'):
        abort(400)
    return start('web_reload', [srv])


# --------------------------------------------------------------------------- config.php (anzeigen, erklären, drucken)

CONFIG_DOC = ('Nextcloud-Doku: Konfigurationsparameter',
              f'{DOC}/configuration_server/config_sample_php_parameters.html')
MASKED = '••• ausgeblendet'
# Harmlose Schlüssel, die die Oberfläche setzen darf (identisch im Wrapper, dort erneut geprüft).
CONFIG_SETTABLE = {
    'default_phone_region': dict(rx=r'[A-Z]{2}', hint='Ländercode nach ISO 3166-1, z. B. AT, DE, CH', ex='AT'),
    'default_language': dict(rx=r'[a-z]{2,3}(_[A-Z][a-z]{3})?(_[A-Z]{2})?', hint='z. B. de, de_DE', ex='de'),
    'default_locale': dict(rx=r'[a-z]{2,3}(_[A-Z][a-z]{3})?(_[A-Z]{2})?', hint='z. B. de_AT, de_DE', ex='de_AT'),
    'logtimezone': dict(rx=r'[A-Za-z]+(/[A-Za-z0-9_+-]+){0,2}', hint='z. B. Europe/Vienna, UTC', ex='Europe/Vienna'),
    'loglevel': dict(rx=r'[0-4]', hint='0 Debug, 1 Info, 2 Warnung, 3 Fehler, 4 Fatal', ex='2'),
    'maintenance_window_start': dict(rx=r'[0-9]|1[0-9]|2[0-3]|100', hint='Stunde in UTC (0–23), 100 = aus', ex='1'),
    'trashbin_retention_obligation': dict(rx=r'auto|disabled|auto, ?[0-9]{1,4}|[0-9]{1,4}, ?auto|[0-9]{1,4}, ?[0-9]{1,4}',
                                          hint='auto, „auto, 30“, „7, 30“ oder disabled', ex='auto, 30'),
    'versions_retention_obligation': dict(rx=r'auto|disabled|auto, ?[0-9]{1,4}|[0-9]{1,4}, ?auto|[0-9]{1,4}, ?[0-9]{1,4}',
                                          hint='auto, „auto, 365“, „30, 365“ oder disabled', ex='auto, 365'),
    'preview_max_x': dict(rx=r'[0-9]{2,5}', hint='Pixel', ex='2048'),
    'preview_max_y': dict(rx=r'[0-9]{2,5}', hint='Pixel', ex='2048'),
}
# Kritisch: eine falsche Änderung kann Nextcloud unerreichbar machen oder Daten unauffindbar. Nur anzeigen.
CONFIG_CRITICAL = {
    'instanceid': 'Eindeutige Kennung der Installation. Nie ändern – sonst gehen Caches, Sitzungen und Teile der Verschlüsselung verloren.',
    'passwordsalt': 'Salz für ältere Passwort-Hashes. Geht es verloren, funktionieren alte Passwörter nicht mehr.',
    'secret': 'Geheimer Schlüssel für Verschlüsselung und Tokens. Geht er verloren, sind verschlüsselte Daten unlesbar.',
    'trusted_domains': 'Adressen, unter denen Nextcloud erreichbar sein darf. Fehlt die richtige, erscheint „Zugriff über nicht vertrauenswürdige Domain“.',
    'datadirectory': 'Ordner mit allen Benutzerdateien. Ein falscher Wert lässt Nextcloud ohne Dateien starten.',
    'dbtype': 'Art der Datenbank (mysql, pgsql, sqlite3).',
    'dbhost': 'Rechner (und ggf. Port oder Socket) der Datenbank.',
    'dbname': 'Name der Nextcloud-Datenbank.',
    'dbuser': 'Benutzer, mit dem Nextcloud sich an der Datenbank anmeldet.',
    'dbpassword': 'Passwort des Datenbank-Benutzers (hier ausgeblendet; steht nur in config.php).',
    'dbport': 'Port der Datenbank, falls nicht Standard.',
    'dbtableprefix': 'Präfix aller Nextcloud-Tabellen (meist oc_). Nie nachträglich ändern.',
    'mysql.utf8mb4': 'Datenbank nutzt 4-Byte-UTF-8 (nötig für Emojis).',
    'version': 'Installierte Nextcloud-Version – wird vom Updater gepflegt, nie von Hand ändern.',
    'installed': 'Kennzeichen „Installation abgeschlossen“. Bei false startet der Installationsassistent.',
    'overwrite.cli.url': 'Basis-Adresse für Links, die Hintergrundjobs und occ erzeugen (z. B. in Mails).',
    'overwritehost': 'Erzwungener Hostname – meist nur hinter einem Reverse-Proxy nötig.',
    'overwriteprotocol': 'Erzwungenes Protokoll (https) – meist hinter einem Reverse-Proxy.',
    'overwritewebroot': 'Erzwungener Unterpfad, z. B. /nextcloud.',
    'overwritecondaddr': 'Bedingung, wann die overwrite-Einstellungen gelten.',
    'htaccess.RewriteBase': 'Basis für Pretty URLs (Adressen ohne /index.php/).',
    'trusted_proxies': 'Reverse-Proxys, deren Weiterleitungs-Header Nextcloud vertraut.',
    'forwarded_for_headers': 'Header, aus denen hinter einem Proxy die Besucher-IP gelesen wird.',
    'memcache.local': 'Lokaler Cache (meist APCu). Ein Wert für ein nicht installiertes Modul legt Nextcloud lahm.',
    'memcache.distributed': 'Verteilter Cache (meist Redis).',
    'memcache.locking': 'Cache für Dateisperren (meist Redis).',
    'redis': 'Verbindung zu Redis (Host, Port, Passwort ausgeblendet).',
    'redis.cluster': 'Verbindung zu einem Redis-Cluster.',
    'objectstore': 'Objektspeicher (S3 o. Ä.) als Hauptspeicher. Zugangsdaten ausgeblendet.',
    'objectstore.multibucket': 'Objektspeicher mit mehreren Buckets.',
    'apps_paths': 'Ordner, in denen Apps liegen.',
    'maintenance': 'Wartungsmodus. Bei true ist Nextcloud für alle gesperrt.',
}
CONFIG_SECTION_DE = {
    'Default Parameters': 'Grundeinstellungen', 'User Experience': 'Benutzeroberfläche', 'User session': 'Sitzungen',
    'Mail Parameters': 'E-Mail', 'Proxy Configurations': 'Proxy und Adressen', 'Deleted Items (trash bin)': 'Papierkorb',
    'File versions': 'Dateiversionen', 'Nextcloud Verifications': 'Prüfungen', 'Logging': 'Protokollierung',
    'Alternate Code Locations': 'Weitere Code-Orte', 'Apps': 'Apps', 'Previews': 'Vorschaubilder', 'LDAP': 'LDAP',
    'Comments': 'Kommentare', 'Maintenance': 'Wartung', 'SSL': 'SSL', 'Memory caching backend configuration': 'Caching',
    'Using Object Store with Nextcloud': 'Objektspeicher', 'Sharing': 'Freigaben', 'Federated Cloud Sharing': 'Föderation',
    'Hashing': 'Hashing', 'All other configuration options': 'Weitere Einstellungen',
}


def config_fmt(v):
    """Wert lesbar: Zeichenketten ohne Anführungszeichen, Arrays als eingerücktes JSON."""
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (dict, list)):
        return json.dumps(v, indent=2, ensure_ascii=False).replace('\\\\', '\\')   # \\OC\\… wie in config.php: \OC\…
    return '' if v is None else str(v)


def config_short(desc):
    """Erster Satz der englischen Beschreibung (RST-Markierungen entfernt)."""
    d = re.sub(r'``([^`]+)``', r'\1', ' '.join((desc or '').split()))
    m = re.match(r'(.+?[.!?])(\s|$)', d)
    s = m.group(1) if m else d
    return s if len(s) <= 180 else s[:177].rsplit(' ', 1)[0] + ' …'


def config_recommendations(cfg, opts):
    rows = []

    def row(key, state, current, rec, note, fix=None, set_value=None):
        rows.append(dict(key=key, state=state, current=current, rec=rec, note=note, fix=fix,
                         set_value=set_value if key in CONFIG_SETTABLE else None))
    if not cfg.get('default_phone_region'):
        row('default_phone_region', 'warn', 'nicht gesetzt', 'Ländercode, z. B. AT',
            'Ohne Standard-Region akzeptiert Nextcloud Telefonnummern nur mit Ländervorwahl; die Prüfungen melden das.',
            set_value='AT')
    mws = cfg.get('maintenance_window_start')
    if mws in (None, '', 100, '100'):
        row('maintenance_window_start', 'warn', 'nicht gesetzt' if mws in (None, '') else '100 (aus)', 'z. B. 1 (= 01:00 UTC)',
            'Ohne Wartungsfenster laufen aufwendige tägliche Hintergrundjobs zu beliebiger Zeit; die Prüfungen melden das.',
            set_value='1')
    ml = cfg.get('memcache.local')
    if not ml:
        row('memcache.local', 'warn', 'nicht gesetzt', '\\OC\\Memcache\\APCu',
            'Ohne lokalen Cache ist Nextcloud spürbar langsamer; die Prüfungen melden das. Vorher muss das PHP-Modul APCu '
            'installiert sein (z. B. Paket php-apcu) – sonst startet Nextcloud nicht mehr.',
            fix="'memcache.local' => '\\\\OC\\\\Memcache\\\\APCu',")
    if not cfg.get('memcache.locking'):
        row('memcache.locking', 'info', 'nicht gesetzt', '\\OC\\Memcache\\Redis',
            'Dateisperren laufen dann über die Datenbank. Mit Redis ist das schneller; nötig ist ein laufender Redis-Server '
            'und der Eintrag „redis“.' + (' Redis ist bereits eingetragen.' if cfg.get('redis') else ''),
            fix="'memcache.locking' => '\\\\OC\\\\Memcache\\\\Redis',")
    if cfg.get('debug') in (True, 'true', 1):
        row('debug', 'bad', 'true', 'false (bzw. entfernen)',
            'Laut Doku nur für die Entwicklung: verlangsamt Nextcloud und kann interne Informationen preisgeben.',
            fix="sudo -u www-data php occ config:system:delete debug")
    if cfg.get('maintenance') in (True, 'true', 1):
        row('maintenance', 'warn', 'true', 'false',
            'Der Wartungsmodus ist aktiv – Nextcloud ist für alle Benutzer gesperrt. Ausschalten unter „Wartung“.')
    if not cfg.get('overwrite.cli.url'):
        row('overwrite.cli.url', 'warn', 'nicht gesetzt', 'https://cloud.example.org',
            'Hintergrundjobs und occ kennen dann die eigene Adresse nicht; Links in Mails können falsch sein.',
            fix="'overwrite.cli.url' => 'https://cloud.example.org',   // eigene Adresse eintragen")
    ll = cfg.get('loglevel')
    if ll in (0, 1, '0', '1'):
        row('loglevel', 'warn', f'{ll} ({"Debug" if str(ll) == "0" else "Info"})', '2 (Warnung)',
            'Sehr ausführliches Logging füllt die Platte und kostet Leistung. Nur zur Fehlersuche verwenden.', set_value='2')
    if not cfg.get('logtimezone'):
        row('logtimezone', 'info', 'nicht gesetzt (UTC)', 'z. B. Europe/Vienna',
            'Zeitangaben im Nextcloud-Log sind sonst in UTC.', set_value='Europe/Vienna')
    # Kritische Schlüssel nie zum Entfernen empfehlen (z. B. passwordsalt: nötig für alte Passwort-Hashes).
    dep = [k for k in cfg if (opts.get(k) or {}).get('deprecated') and k not in CONFIG_CRITICAL]
    if dep:
        row(', '.join(dep), 'warn', 'gesetzt', 'entfernen, wenn nicht mehr benötigt',
            'Laut config.sample.php veraltet – die Einstellung wird künftig nicht mehr unterstützt.')
    unknown = [k for k in cfg if k not in opts]
    if unknown and opts:
        row(', '.join(sorted(unknown)), 'info', f'{len(unknown)} Schlüssel', 'prüfen',
            'Nicht in der config.sample.php dieser Version beschrieben. Meist Einstellungen von Apps (unbedenklich) – '
            'oder ein Tippfehler im Schlüssel, dann wirkt die Einstellung nicht.')
    return rows


def config_data():
    data, err = call_json('config_info')
    cfg = (data.get('config') or {}).get('system') or {}
    sample = data.get('sample') or {}
    opts = sample.get('options') or {}
    status = data.get('status') or {}
    sources = data.get('sources') or {}

    def where(k):
        x = sources.get(k) or {}
        return f"{x['file']}:{x['line']}" if x.get('file') and x.get('line') else x.get('file', '')
    groups = {}
    for k in sorted(cfg, key=lambda k: ((opts.get(k) or {}).get('order', 10 ** 6), k)):
        o = opts.get(k) or {}
        sec = CONFIG_SECTION_DE.get(o.get('section'), o.get('section')) if o else 'Nicht in der Doku beschrieben'
        groups.setdefault(sec, []).append(dict(
            key=k, value=config_fmt(cfg[k]), complex=isinstance(cfg[k], (dict, list)), masked=MASKED in json.dumps(cfg[k], ensure_ascii=False),
            default=o.get('default', ''), desc=o.get('desc', ''), short=config_short(o.get('desc', '')),
            de=CONFIG_CRITICAL.get(k, ''), critical=k in CONFIG_CRITICAL, settable=k in CONFIG_SETTABLE,
            documented=bool(o), deprecated=o.get('deprecated', False), src=where(k),
            file=(sources.get(k) or {}).get('file', '')))
    catalog = {}
    for k, o in sorted(opts.items(), key=lambda x: x[1].get('order', 0)):
        sec = CONFIG_SECTION_DE.get(o.get('section'), o.get('section'))
        catalog.setdefault(sec, []).append(dict(key=k, set=k in cfg, default=o.get('default', ''),
                                                short=config_short(o.get('desc', '')), desc=o.get('desc', ''),
                                                example=o.get('example', '')))
    host = cfg.get('overwrite.cli.url') or ((cfg.get('trusted_domains') or [''])[0] if isinstance(cfg.get('trusted_domains'), list) else '')
    info = dict(version=status.get('versionstring', ''), nc_path=data.get('nc_path', ''), host=host or data.get('hostname', ''),
                hostname=data.get('hostname', ''), datadir=cfg.get('datadirectory', ''), dbtype=cfg.get('dbtype', ''),
                count=len(cfg), critical=sum(1 for k in cfg if k in CONFIG_CRITICAL), sample_error=sample.get('error', ''),
                documented=len(opts))
    files = sorted({(x or {}).get('file') for x in sources.values() if (x or {}).get('file')},
                   key=lambda f: (not f.endswith('/config.php'), f))
    return dict(cfg=cfg, opts=opts, groups=groups, catalog=catalog, info=info, err=err, files=files,
                recs=config_recommendations(cfg, opts))


@app.get('/config')
@login_required
def config_page():
    d = config_data()
    settable = [dict(key=k, current=config_fmt(d['cfg'].get(k)) if k in d['cfg'] else '', **v,
                     default=(d['opts'].get(k) or {}).get('default', '')) for k, v in CONFIG_SETTABLE.items()]
    return render_template('config.html', **d, settable=settable, doc=CONFIG_DOC, running=jobs.running_job_id())


@app.get('/config/print')
@login_required
def config_print():
    d = config_data()
    crit = [r for g in d['groups'].values() for r in g if r['critical']]
    crit.sort(key=lambda r: list(CONFIG_CRITICAL).index(r['key']))
    other = {g: [r for r in rows if not r['critical']] for g, rows in d['groups'].items()}
    other = {g: rows for g, rows in other.items() if rows}
    return render_template('config_print.html', **d, crit=crit, other=other, doc=CONFIG_DOC, cfg_masked=MASKED,
                           now=time.strftime('%d.%m.%Y %H:%M'))


def report_php():
    """PHP-Werte für CLI und Web mit der Datei, aus der der wirksame Wert stammt."""
    p, _ = call_json('php_platform')
    v, _ = call_json('php_values')
    cli, fpm = v.get('cli') or {}, v.get('fpm') or {}
    has_fpm, web_sapi = bool(v.get('fpm_service')), p.get('web_sapi', '')
    ov, srcs = v.get('overrides') or {}, v.get('sources') or {}

    def last(k, sapi):
        items = [x for x in srcs.get(k) or [] if x.get('sapi') == sapi]
        return items[-1]['file'] if items else ''
    rows = []
    for k in v.get('keys', []):
        web, origin = php_web_value(k, cli.get(k) or '', fpm.get(k) or '', has_fpm, web_sapi, ov)
        if origin == '.user.ini':
            wfile = ov.get('user_ini_path', '.user.ini')
        elif origin == '.htaccess':
            wfile = ov.get('htaccess_path', '.htaccess')
        else:
            wfile = last(k, 'fpm' if has_fpm else 'cli')
        rows.append(dict(key=k, label=PHP_LABELS.get(k, ''), cli=cli.get(k) or '–', cli_file=last(k, 'cli') or 'PHP-Standard',
                         web=web or '–', web_file=wfile or 'PHP-Standard', origin=origin))
    return dict(platform=p, rows=rows, cli_files=v.get('cli_files') or [], fpm_files=v.get('fpm_files') or [])


def report_web():
    data, _ = call_json('web_info')
    web, out = data.get('web') or {}, []
    for key, label in (('apache', 'Apache'), ('nginx', 'nginx')):
        f = web.get(key)
        if not f:
            continue
        rows = []
        p = f.get('primary') or {}
        if p:
            rows.append(('Nextcloud-Bereich', p.get('name') or '(ohne Namen)', p.get('src', '')))
        if key == 'apache':
            for name, fk in (('AllowOverride (Nextcloud-Ordner)', 'allow_override'), ('HSTS', 'hsts'),
                             ('LimitRequestBody', 'limit_request_body'), ('PHP-FPM-Anbindung', 'fpm_handler')):
                x = f.get(fk)
                if x:
                    rows.append((name, x.get('value', ''), x.get('src', '')))
            rows.append(('MPM · PHP', f"{f.get('mpm', '–')} · {' + '.join(f.get('php_handlers') or ['–'])}", 'apache2ctl -V / -M'))
            rows.append(('Geladene Module', ', '.join(m.replace('_module', '') for m in f.get('modules') or []),
                         '/etc/apache2/mods-enabled/'))
        else:
            for d in ('client_max_body_size', 'client_body_timeout', 'fastcgi_buffers', 'fastcgi_read_timeout',
                      'fastcgi_request_buffering', 'fastcgi_pass', 'gzip', 'server_tokens', 'front_controller'):
                x = f.get(d)
                if x:
                    rows.append((d, x.get('value', ''), x.get('src', '')))
            for name, fk in (('/.well-known/carddav', 'carddav'), ('/.well-known/caldav', 'caldav')):
                if (f.get('wellknown') or {}).get(fk):
                    rows.append((name, 'Weiterleitung', f['wellknown'][fk]))
            if f.get('hidden_paths'):
                rows.append(('Sperre interner Ordner', 'vorhanden', f['hidden_paths']))
            if f.get('headers'):
                rows.append(('Sicherheits-Header', ', '.join(f['headers']), f.get('headers_src', '')))
        out.append(dict(label=f"{label} {f.get('version', '')}", rows=rows, error=f.get('error', '')))
    return out


@app.get('/report')
@login_required
def report_page():
    d = config_data()
    by_file = {}
    for rows in d['groups'].values():
        for r in rows:
            by_file.setdefault(r['file'] or '(Fundort unbekannt)', []).append(r)
    for f in by_file:
        by_file[f].sort(key=lambda r: int(r['src'].rsplit(':', 1)[1]) if r['src'][-1:].isdigit() else 0)
    order = sorted(by_file, key=lambda f: (not f.endswith('/config.php'), f))
    fpm, _ = call_json('fpm_info')
    diag, _ = call_json('diagnose')
    core = (((diag.get('core') or {}).get('apps') or {}).get('core') or {})
    return render_template('report.html', **d, by_file=[(f, by_file[f]) for f in order], php=report_php(),
                           fpm=fpm, web=report_web(), diag=diag, bgmode=core.get('backgroundjobs_mode', ''),
                           crit_keys=CONFIG_CRITICAL, now=time.strftime('%d.%m.%Y %H:%M'), cfg_masked=MASKED, doc=CONFIG_DOC)


@app.post('/config/set')
@login_required
def config_set():
    key, value = request.form.get('key', ''), request.form.get('value', '').strip()
    if key not in CONFIG_SETTABLE or not re.fullmatch(CONFIG_SETTABLE[key]['rx'], value):
        hint = CONFIG_SETTABLE[key]['hint'] if key in CONFIG_SETTABLE else 'nicht erlaubter Schlüssel'
        return render_template('message.html', title='Ungültiger Wert',
                               text=f'„{value}“ ist für {key} nicht zulässig ({hint}).', bad=True), 400
    return start('config_set', [key, value])


@app.post('/config/reset')
@login_required
def config_reset():
    key = request.form.get('key', '')
    if key not in CONFIG_SETTABLE:
        abort(400)
    return start('config_reset', [key])


# --------------------------------------------------------------------------- Nextcloud-Prüfungen

SEVERITY_ORDER = {'error': 0, 'warning': 1, 'info': 2, 'success': 3}


def run_setupchecks():
    rc, out, err = call('setupchecks', timeout=170)
    try:
        data = json.loads(out)
    except ValueError:
        msg = (out + err).strip()
        if 'not defined' in msg or 'nicht definiert' in msg:
            msg = 'occ setupchecks gibt es erst ab Nextcloud 28.'
        return {'error': msg or f'Exit-Code {rc}'}
    checks = []
    for category, items in (data or {}).items():
        for cls, c in (items or {}).items():
            if not isinstance(c, dict):
                continue
            link = c.get('linkToDoc') or ''
            checks.append({'category': category, 'name': c.get('name') or cls.rsplit('\\', 1)[-1],
                           'severity': c.get('severity') or 'info', 'description': c.get('description') or '',
                           'link': link if link.startswith('https://') else ''})
    checks.sort(key=lambda c: (SEVERITY_ORDER.get(c['severity'], 9), c['category'], c['name']))
    result = {'checks': checks}
    jobs.cache_set('setupchecks', result)
    return result


def checks_summary(data):
    if not data or 'checks' not in data:
        return None
    count = {}
    for c in data['checks']:
        count[c['severity']] = count.get(c['severity'], 0) + 1
    return count


@app.get('/checks')
@login_required
def checks_page():
    data, ts = jobs.cache_get('setupchecks')
    if data is None:
        data, ts = run_setupchecks(), int(time.time())
    return render_template('checks.html', data=data, ts=ts, summary=checks_summary(data),
                           running=jobs.running_job_id())


@app.post('/checks/refresh')
@login_required
def checks_refresh():
    run_setupchecks()
    return redirect(url_for('checks_page'))


# --------------------------------------------------------------------------- Apps

def fetch_app_updates():
    data, err = call_json('app_updates')
    if not data:
        return {'error': err}
    updates = []
    for line in (data.get('updates') or '').splitlines():
        m = re.match(r'^(\S+) new version available: (\S+)$', line.strip())
        if m:
            updates.append({'id': m.group(1), 'new': m.group(2)})
    apps = data.get('apps') or {}
    enabled = apps.get('enabled') or {}
    for u in updates:
        u['current'] = enabled.get(u['id']) or (apps.get('disabled') or {}).get(u['id']) or '–'
    result = {'updates': updates, 'enabled': enabled, 'disabled': apps.get('disabled') or {}}
    jobs.cache_set('apps', result)
    return result


@app.get('/apps')
@login_required
def apps_page():
    data, ts = jobs.cache_get('apps')
    if data is None:
        data, ts = fetch_app_updates(), int(time.time())
    return render_template('apps.html', data=data, ts=ts, running=jobs.running_job_id())


@app.post('/apps/refresh')
@login_required
def apps_refresh():
    fetch_app_updates()
    return redirect(url_for('apps_page'))


@app.post('/apps/update')
@login_required
def apps_update():
    app_id = request.form.get('app', '')
    if app_id == '*':
        return start('app_update_all')
    if not re.fullmatch(APP_RE, app_id):
        abort(400)
    return start('app_update', [app_id])


# --------------------------------------------------------------------------- Backups

@app.get('/backups')
@login_required
def backups_page():
    data, err = call_json('backup_list')
    return render_template('backups.html', data=data, err=err, keep=os.environ.get('NCM_BACKUP_KEEP', '3'),
                           running=jobs.running_job_id())


@app.post('/backups/create')
@login_required
def backup_create():
    return start('backup_full' if request.form.get('with_data') == 'yes' else 'backup')


@app.post('/backups/verify')
@login_required
def backup_verify():
    name = request.form.get('name', '')
    if not re.fullmatch(BACKUP_NAME_RE, name):
        abort(400)
    # Als Job: bei großen Vollbackups dauert das vollständige Lesen deutlich länger als ein Seitenaufruf.
    return start('backup_verify', [name])


def _find_backup(name):
    data, err = call_json('backup_list')
    return next((b for b in data.get('backups', []) if b['name'] == name), None), data, err


@app.get('/backups/<name>/restore')
@login_required
def restore_plan(name):
    if not re.fullmatch(BACKUP_NAME_RE, name):
        abort(404)
    backup, data, err = _find_backup(name)
    if not backup:
        abort(404)
    plan, perr = call_json_args('restore_plan', name)
    info = backup.get('info') or {}
    problems, notes = [], []
    if plan:
        if (info.get('nc_path') or '').rstrip('/') not in ('', plan.get('nc_path')):
            problems.append(f"Das Backup gehört zu {info.get('nc_path')}, nicht zu {plan.get('nc_path')}.")
        if info.get('datadir') and info['datadir'].rstrip('/') != plan.get('datadir'):
            problems.append(f"Datenverzeichnis im Backup ({info['datadir']}) weicht vom aktuellen ({plan.get('datadir')}) ab.")
        if plan.get('datadir_problem'):
            problems.append(plan['datadir_problem'])
        if as_bool(plan.get('datadir_inside')):
            problems.append('Das Datenverzeichnis liegt im Programmordner – automatischer Restore nicht möglich '
                            '(Anleitung: WIEDERHERSTELLEN.txt im Backup).')
        if backup.get('user_data') and as_bool(plan.get('datadir_mountpoint')):
            problems.append('Das Datenverzeichnis ist ein eigener Mountpoint und kann nicht umbenannt werden – '
                            'Benutzerdaten bitte von Hand zurückspielen.')
    else:
        problems.append(f'Vorprüfung nicht möglich: {perr}')
    ver = backup.get('verify') or {}
    if ver.get('ok') is False:
        problems.append('Die letzte Prüfung dieses Backups ist fehlgeschlagen.')
    if not backup.get('user_data'):
        notes.append('Benutzerdaten sind nicht enthalten und bleiben unverändert. Dateien, die seit dem Backup '
                     'hochgeladen oder gelöscht wurden, passen danach nicht mehr zur Datenbank – anschließend unter '
                     'Wartung „Alle Dateien neu einlesen“ ausführen.')
    if not backup.get('checksums'):
        notes.append('Dieses Backup hat keine Prüfsummen (erstellt vor Version 0.6.1) – geprüft wird nur die Struktur.')
    return render_template('restore.html', b=backup, info=info, plan=plan, problems=problems, notes=notes,
                           ver=ver, running=jobs.running_job_id())


@app.post('/backups/restore')
@login_required
def backup_restore():
    name = request.form.get('name', '')
    if not re.fullmatch(BACKUP_NAME_RE, name):
        abort(400)
    if request.form.get('confirm', '').strip() != 'WIEDERHERSTELLEN':
        return render_template('message.html', title='Nicht bestätigt',
                               text='Zum Bestätigen muss WIEDERHERSTELLEN eingegeben werden. Es wurde nichts verändert.',
                               bad=True), 400
    return start('restore_backup', [name])


@app.post('/backups/delete')
@login_required
def backup_delete():
    name = request.form.get('name', '')
    if not re.fullmatch(BACKUP_NAME_RE, name):
        abort(400)
    return start('backup_delete', [name])


# --------------------------------------------------------------------------- Diagnose

@app.get('/diagnose')
@login_required
def diagnose_page():
    d, err = call_json('diagnose')
    core = d.get('core') or {}
    core = (core.get('apps') or {}).get('core', core) if isinstance(core, dict) else {}
    lastcron = core.get('lastcron')
    try:
        age = int(time.time()) - int(lastcron)
    except (TypeError, ValueError):
        age = None
    mode = core.get('backgroundjobs_mode') or 'ajax'
    cron = {'mode': mode, 'lastcron': lastcron, 'age': age, 'entry': d.get('cron_entry'), 'timer': d.get('cron_timer'),
            'ok': mode == 'cron' and age is not None and age < 900 and bool(d.get('cron_entry') or d.get('cron_timer'))}
    hints = []
    if mode != 'cron':
        hints.append(f'Hintergrundjobs laufen im Modus „{mode}“. Empfohlen ist „cron“: Eintrag '
                     '„*/5 * * * * php -f <Nextcloud>/cron.php“ in der Crontab des Webserver-Benutzers.')
    elif not (d.get('cron_entry') or d.get('cron_timer')):
        hints.append('Modus ist „cron“, aber weder ein Cron-Eintrag mit cron.php (Crontab des Webserver-Benutzers, /etc/crontab, /etc/cron.d) noch ein systemd-Timer wurde gefunden.')
    elif age is not None and age >= 900:
        hints.append(f'Der letzte Cron-Lauf ist {age // 60} Minuten her – Nextcloud erwartet alle 5 Minuten einen Lauf.')
    db = d.get('db') or {}
    redis = d.get('redis') or {}
    if redis.get('configured') and redis.get('ping') not in ('PONG', None):
        hints.append(f"Redis ist in config.php eingetragen, antwortet aber nicht: {redis.get('ping')}")
    if not redis.get('configured') and not d.get('memcache_local'):
        hints.append('Es ist kein Cache konfiguriert (memcache.local). Empfohlen: APCu lokal, Redis für Locking.')
    return render_template('diagnose.html', d=d, err=err, db=db, redis=redis, cron=cron, hints=hints)


# --------------------------------------------------------------------------- Logs

LOG_LEVELS = {0: 'Debug', 1: 'Info', 2: 'Warnung', 3: 'Fehler', 4: 'Fatal'}


LOG_ROTATE_CHOICES = [('10485760', '10 MB'), ('52428800', '50 MB'), ('104857600', '100 MB (Standard)'),
                      ('262144000', '250 MB'), ('524288000', '500 MB'), ('0', 'aus (keine Rotation)')]


def _log_time(value):
    """Zeitstempel eines Log-Eintrags als Unix-Zeit (None, wenn nicht lesbar)."""
    from datetime import datetime
    try:
        t = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return t.timestamp()


@app.get('/logs')
@login_required
def logs_page():
    level = request.args.get('level', '2')
    level = int(level) if level in ('0', '1', '2', '3', '4') else 2
    lines = request.args.get('lines', '500')
    lines = lines if lines in ('200', '500', '1000', '2000') else '500'
    q = request.args.get('q', '').strip()[:100]
    since = session.get('log_since')
    rc, out, err = call('logs', lines)
    try:
        data = json.loads(out) if rc == 0 else {}
    except ValueError:
        data = {}
    err = '' if data else (out + '\n' + err).strip()
    nc = data.get('nextcloud') or {}
    entries, hidden_old = [], 0
    for e in nc.get('entries', []):
        try:
            lv = int(e.get('level', -1))
        except (TypeError, ValueError):
            lv = -1
        e['level'] = lv
        if lv != -1 and lv < level:
            continue
        if since:
            ts = _log_time(e.get('time'))
            if ts is not None and ts < since:
                hidden_old += 1
                continue
        if q and q.lower() not in json.dumps(e, ensure_ascii=False).lower():
            continue
        entries.append(e)
    try:
        rotate = int(data.get('log_rotate_size')) if data.get('log_rotate_size') not in (None, '') else 104857600
    except ValueError:
        rotate = 104857600
    try:
        loglevel = int(data.get('loglevel')) if data.get('loglevel') not in (None, '') else 2
    except ValueError:
        loglevel = 2
    return render_template('logs.html', data=data, err=err, nc=nc, entries=entries[:300], total=len(entries),
                           level=level, lines=lines, q=q, levels=LOG_LEVELS, since=since, hidden_old=hidden_old,
                           rotate=rotate, rotate_choices=LOG_ROTATE_CHOICES, loglevel=loglevel,
                           running=jobs.running_job_id())


@app.post('/logs/since')
@login_required
def logs_since():
    """„Nur neue Einträge“: Zeitpunkt merken (pro Sitzung) bzw. wieder alles zeigen."""
    if request.form.get('mode') == 'clear':
        session.pop('log_since', None)
    else:
        session['log_since'] = time.time()
    return redirect(url_for('logs_page', level=request.form.get('level', '2'), lines=request.form.get('lines', '500')))


@app.post('/logs/archive')
@login_required
def logs_archive():
    session.pop('log_since', None)          # nach dem Leeren ist ohnehin alles neu
    return start('log_archive')


@app.post('/logs/rotate')
@login_required
def logs_rotate():
    v = request.form.get('size', '')
    if v not in dict(LOG_ROTATE_CHOICES):
        abort(400)
    return start('log_rotate_set', [v])


@app.post('/logs/level')
@login_required
def logs_level():
    v = request.form.get('level', '')
    if v not in ('0', '1', '2', '3', '4'):
        abort(400)
    return start('log_level_set', [v])


# --------------------------------------------------------------------------- PHP-FPM

FPM_FIELDS = ('pm', 'pm.max_children', 'pm.start_servers', 'pm.min_spare_servers', 'pm.max_spare_servers',
              'pm.max_requests')
MB = 1024


def fpm_recommendation(info, ver, pool):
    """Vorschlag für den Pool anhand von RAM und gemessenem Speicher pro Worker."""
    p = info['versions'][ver]['pools'][pool]
    total, avail = info.get('mem_total_kb', 0), info.get('mem_available_kb', 0)
    fpm_total = sum(pp['worker_kb_total'] for v in info['versions'].values() for pp in v['pools'].values())
    measured = p['workers'] > 0
    per_proc = max(p['worker_kb_avg'], 64 * MB) if measured else 96 * MB
    other = max(0, total - avail - fpm_total)           # RAM für DB, Redis, System …
    budget = max(0, total * 0.85 - other)               # 15 % Reserve für Cache und Spitzen
    # Obergrenze 120: jeder Worker kann eine DB-Verbindung öffnen (MariaDB-Standard max_connections = 151).
    mc = max(2, min(120, int(budget // per_proc)))
    lo = max(1, round(mc * 0.15))
    st = max(lo, round(mc * 0.25))
    hi = min(mc, max(st, round(mc * 0.35)))
    values = {'pm': 'dynamic', 'pm.max_children': str(mc), 'pm.start_servers': str(st),
              'pm.min_spare_servers': str(lo), 'pm.max_spare_servers': str(hi), 'pm.max_requests': '500'}
    basis = {'per_proc_mb': round(per_proc / MB), 'measured': measured, 'other_mb': round(other / MB),
             'budget_mb': round(budget / MB), 'total_mb': round(total / MB)}
    return values, basis


@app.get('/fpm')
@login_required
def fpm_page():
    info, err = call_json('fpm_info')
    recs = {}
    for ver, v in (info.get('versions') or {}).items():
        for pool in v['pools']:
            recs[(ver, pool)] = fpm_recommendation(info, ver, pool)
    return render_template('fpm.html', info=info, err=err, recs=recs, fields=FPM_FIELDS,
                           running=jobs.running_job_id())


@app.post('/fpm/set')
@login_required
def fpm_set():
    ver, pool = request.form.get('version', ''), request.form.get('pool', '')
    if not re.fullmatch(r'[0-9]\.[0-9]{1,2}', ver) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', pool):
        abort(400)
    pairs = []
    for k in FPM_FIELDS:
        v = request.form.get(k, '').strip()
        if not v:
            continue
        if (k == 'pm' and v not in ('dynamic', 'ondemand', 'static')) or (k != 'pm' and not re.fullmatch(r'[0-9]{1,6}', v)):
            return render_template('message.html', title='Ungültiger Wert', text=f'{k} = {v} ist nicht zulässig.',
                                   bad=True), 400
        pairs.append(f'{k}={v}')
    if not pairs:
        abort(400)
    return start('fpm_set', [ver, pool, *pairs])


@app.get('/jobs/<int:jid>')
@login_required
def job_view(jid):
    job = jobs.get_job(jid)
    if not job:
        abort(404)
    back = ACTION_PAGES.get(job['action'])
    return render_template('job.html', job=job, back=url_for(back) if back else None)


@app.get('/jobs/<int:jid>/status')
@login_required
def job_status(jid):
    job = jobs.get_job(jid)
    if not job:
        return jsonify(error='not found'), 404
    return jsonify(status=job['status'], rc=job['rc'], duration=round(job['duration'] or 0, 1),
                   output=job['output'] or '')


@app.get('/history')
@login_required
def history():
    return render_template('history.html', rows=jobs.list_jobs(100))


def _simple_markdown(text):
    """Minimaler Markdown-Umsetzer für den Haftungsausschluss (Überschriften, Listen, Absätze, **fett**)."""
    from markupsafe import Markup, escape
    html, para, items = [], [], []

    def inline(t):
        t = str(escape(t))
        return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', re.sub(r'`([^`]+)`', r'<code>\1</code>', t))

    def flush():
        if para:
            html.append('<p>' + inline(' '.join(para)) + '</p>')
            para.clear()
        if items:
            html.append('<ul>' + ''.join(f'<li>{inline(i)}</li>' for i in items) + '</ul>')
            items.clear()
    for line in text.splitlines():
        st = line.strip()
        if not st or st == '---':
            flush()
        elif st.startswith('#'):
            flush()
            level = min(len(st) - len(st.lstrip('#')) + 1, 4)
            html.append(f'<h{level}>{inline(st.lstrip("#").strip())}</h{level}>')
        elif st.startswith('- '):
            if para:
                flush()
            items.append(st[2:])
        elif items and line.startswith('  '):
            items[-1] += ' ' + st
        else:
            para.append(st)
    flush()
    return Markup('\n'.join(html))


@app.get('/lizenz')
def license_page():
    """Öffentlich: Lizenz und Haftungsausschluss (auch ohne Anmeldung lesbar)."""
    def read(name):
        try:
            with open(os.path.join(BASE_DIR, name), encoding='utf-8') as f:
                return f.read()
        except OSError:
            return f'{name} nicht gefunden.'
    return render_template('license.html', license_text=read('LICENSE'),
                           disclaimer=_simple_markdown(read('HAFTUNGSAUSSCHLUSS.md')))


@app.get('/favicon.ico')
def favicon():
    return '', 204


@app.errorhandler(500)
def server_error(e):
    # Traceback steht im Journal (journalctl -u nc-manager); hier nur eine verständliche Meldung.
    app.logger.error('Fehler bei %s: %s', request.path, getattr(e, 'original_exception', e))
    try:
        return render_template('message.html', title='Interner Fehler',
                               text=f'Beim Laden von {request.path} ist ein Fehler aufgetreten. Details stehen im '
                                    'Journal: sudo journalctl -u nc-manager -n 50', bad=True), 500
    except Exception:
        return 'Interner Fehler – Details: sudo journalctl -u nc-manager -n 50', 500


@app.errorhandler(404)
def not_found(_e):
    return render_template('message.html', title='Nicht gefunden', text='Diese Seite gibt es nicht.', bad=True), 404


jobs.init_db()
