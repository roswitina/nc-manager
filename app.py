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

VERSION = '0.6.5'
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
})
# Rücksprung von der Job-Seite
ACTION_PAGES = {'php_set': 'php_page', 'fpm_set': 'fpm_page', 'app_update': 'apps_page',
                'app_update_all': 'apps_page', 'backup': 'backups_page', 'backup_delete': 'backups_page',
                'backup_full': 'backups_page', 'backup_verify': 'backups_page', 'restore_backup': 'backups_page',
                'log_archive': 'logs_page', 'log_rotate_set': 'logs_page', 'log_level_set': 'logs_page',
                'nextcloud_update': 'update_wizard', 'update_check': 'update_wizard'}
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
