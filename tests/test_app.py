# Nextcloud Server Manager
# Copyright (c) 2026 roswitina@hotmail.com
# SPDX-License-Identifier: MIT
# Lizenz: siehe LICENSE · Gewährleistungs- und Haftungsausschluss: siehe HAFTUNGSAUSSCHLUSS.md
"""Tests der Web-App mit simuliertem Wrapper:  python3 -m pytest tests/"""
import importlib
import os
import re
import sys
import time

import pytest
from werkzeug.security import generate_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
PASSWORD = 'richtig-langes-passwort'


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('NCM_SECRET', 'x' * 64)
    monkeypatch.setenv('NCM_PASSWORD_HASH', generate_password_hash(PASSWORD))
    monkeypatch.setenv('NCM_USER', 'admin')
    monkeypatch.setenv('NCM_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('NCM_WRAPPER', os.path.join(HERE, 'fake-wrapper'))
    monkeypatch.setenv('NCM_SUDO', '')
    import jobs
    importlib.reload(jobs)
    import app as appmod
    importlib.reload(appmod)
    appmod.app.config['TESTING'] = True
    with appmod.app.test_client() as c:
        yield c


def csrf(c, path='/login'):
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def login(c, pw=PASSWORD):
    return c.post('/login', data={'_csrf': csrf(c), 'user': 'admin', 'password': pw})


def wait_job(c, url):
    jid = int(url.rstrip('/').split('/')[-1])
    for _ in range(100):
        j = c.get(f'/jobs/{jid}/status').get_json()
        if j['status'] != 'running':
            return j
        time.sleep(0.1)
    raise AssertionError('Job wurde nicht fertig')


def test_login_page_renders(client):          # Regression v0.4.2: HTTP 500
    r = client.get('/login')
    assert r.status_code == 200 and 'Anmelden' in r.get_data(as_text=True)


def test_requires_login(client):
    assert client.get('/').status_code == 302


def test_post_without_csrf_rejected(client):
    login(client)
    assert client.post('/run/repair').status_code == 400


def test_wrong_password_and_lockout(client):
    for _ in range(5):
        assert 'fehlgeschlagen' in login(client, 'falsch').get_data(as_text=True)
    r = login(client)              # richtiges Passwort, aber gesperrt
    assert 'Zu viele Fehlversuche' in r.get_data(as_text=True)


def test_dashboard_escapes_and_shows_data(client):
    login(client)
    html = client.get('/').get_data(as_text=True)
    assert '30.0.1' in html and '/srv/data' in html and re.search(r'vor 6[0-9] s', html)
    assert '<script>alert' not in html and '&lt;script&gt;' in html


def test_security_headers(client):
    r = client.get('/login')
    assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
    assert 'SameSite=Strict' in r.headers.get('Set-Cookie', '')


def test_update_not_reachable_via_run(client):
    login(client)
    t = csrf(client, '/maintenance')
    assert client.post('/run/nextcloud_update', data={'_csrf': t}).status_code == 404
    assert client.post('/run/php_set', data={'_csrf': t}).status_code == 404


def test_update_requires_backup_confirmation(client):
    login(client)
    t = csrf(client, '/update')
    assert client.post('/update/start', data={'_csrf': t}).status_code == 400


def test_job_runs_in_background_and_is_logged(client):
    login(client)
    r = client.post('/run/repair', data={'_csrf': csrf(client, '/maintenance')})
    assert r.status_code == 302
    j = wait_job(client, r.headers['Location'])
    assert j['rc'] == 0 and 'repair ok' in j['output']
    hist = client.get('/history').get_data(as_text=True)
    assert 'Maintenance Repair' in hist
    assert 'dashboard' not in hist          # Lese-Aufrufe landen nicht im Protokoll


def test_failed_job_reports_exit_code(client):
    login(client)
    r = client.post('/run/maint_on', data={'_csrf': csrf(client, '/maintenance')})
    j = wait_job(client, r.headers['Location'])
    assert j['rc'] == 3 and 'fehler' in j['output']


def test_only_one_job_at_a_time(client):
    login(client)
    t = csrf(client, '/maintenance')
    r1 = client.post('/run/bigint', data={'_csrf': t})
    assert r1.status_code == 302
    r2 = client.post('/run/repair', data={'_csrf': t})
    assert r2.status_code == 409
    time.sleep(0.5)
    live = client.get(r1.headers['Location'] + '/status').get_json()
    assert live['status'] == 'running' and 'start' in live['output']
    assert wait_job(client, r1.headers['Location'])['rc'] == 0


def test_php_set_validation(client):
    login(client)
    t = csrf(client, '/php')
    assert client.post('/php/set', data={'_csrf': t, 'key': 'memory_limit', 'value': '512X'}).status_code == 400
    assert client.post('/php/set', data={'_csrf': t, 'key': 'display_errors', 'value': '1'}).status_code == 400
    r = client.post('/php/set', data={'_csrf': t, 'key': 'memory_limit', 'value': '1G'})
    assert wait_job(client, r.headers['Location'])['output'] == 'set memory_limit=1G'


def test_php_page_marks_differences(client):
    login(client)
    html = client.get('/php').get_data(as_text=True)
    assert '512M' in html and '≠' in html


def test_php_form_prefilled_with_current_value(client):
    login(client)
    html = client.get('/php').get_data(as_text=True)
    # erstes Feld = memory_limit, aktueller FPM-Wert vorausgefüllt
    assert 'id="value" name="value" value="512M"' in html
    assert 'data-current="511M" data-rec="16G"' in html          # upload_max_filesize: Wert aus .user.ini


def test_php_recommendations(client):
    login(client)
    html = client.get('/php').get_data(as_text=True)
    assert '5 Wert(e) weichen von der Empfehlung ab' in html      # upload(.user.ini), post, exec, input, interned
    assert html.count('Empfehlung setzen</button>') == 4           # upload nicht: steht in .user.ini
    assert 'in .user.ini festgelegt' in html
    assert 'upload_max_filesize: Der Wert aus /var/www/nextcloud/.user.ini überstimmt' in html
    assert 'value="opcache.interned_strings_buffer"><input type="hidden" name="value" value="16"' in html


def test_php_sources_and_basis(client):
    login(client)
    html = client.get('/php').get_data(as_text=True)
    assert html.count('class="basis basis-richtwert"') >= 3 + 1    # 3 OPcache-Zeilen + Legende
    assert 'big_file_upload_configuration.html' in html and 'php_configuration.html' in html
    # output_buffering: FPM sagt 4096, aber .user.ini setzt 0 -> wirksam 0, in Ordnung
    row = html[html.index('<td>output_buffering<div'):]
    row = row[:row.index('</tr>')]
    assert 'aus .user.ini' in row and '(Konfiguration: 4096)' in row and '✓' in row


def test_php_output_buffering_settable(client):
    login(client)
    r = post(client, '/php', '/php/set', key='output_buffering', value='0')
    assert wait_job(client, r.headers['Location'])['output'] == 'set output_buffering=0'
    assert post(client, '/php', '/php/set', key='output_buffering', value='Off').status_code == 400


def test_php_web_value_logic():
    import app as appmod
    f = appmod.php_web_value
    ov = {'user_ini': {'upload_max_filesize': '511M', 'opcache.memory_consumption': '64'},
          'htaccess': {'upload_max_filesize': '100M'}}
    assert f('upload_max_filesize', '2M', '16G', True, 'fpm', ov) == ('511M', '.user.ini')
    assert f('opcache.memory_consumption', '128', '128', True, 'fpm', ov) == ('128', 'PHP-FPM')   # nicht PERDIR
    assert f('upload_max_filesize', '2M', '', False, 'apache2-mod_php', ov) == ('100M', '.htaccess')
    assert f('memory_limit', '-1', '512M', True, 'fpm', {}) == ('512M', 'PHP-FPM')
    assert appmod.php_assessment('output_buffering', '4096') == 'bad'
    assert appmod.php_assessment('output_buffering', 'Off') == 'ok'


def test_php_number():
    import app as appmod
    n = appmod.php_number
    assert n('512M', 'size') == 512 * 1024 ** 2 and n('1g', 'size') == 1024 ** 3
    assert n('-1', 'size') == float('inf') and n('0', 'time') == float('inf')
    assert n('abc', 'size') is None
    assert appmod.php_assessment('memory_limit', '256M') == 'low'
    assert appmod.php_assessment('memory_limit', '-1') == 'ok'


def test_migration_from_v043(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / 'history.db'
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE log(id INTEGER PRIMARY KEY,ts INTEGER,action TEXT,rc INTEGER,duration REAL,output TEXT)')
        c.executemany('INSERT INTO log(ts,action,rc,duration,output) VALUES(?,?,?,?,?)',
                      [(1, 'dashboard', 0, 1, '{}'), (2, 'repair', 0, 5, 'ok'), (3, 'php_platform', 0, 1, '{}')])
    monkeypatch.setenv('NCM_STATE_DIR', str(tmp_path))
    import jobs
    importlib.reload(jobs)
    jobs.init_db()
    rows = jobs.list_jobs()
    assert [r['action'] for r in rows] == ['repair']


# ------------------------------------------------------------------ v0.5.0

def post(c, page, url, **data):
    return c.post(url, data={'_csrf': csrf(c, page), **data})


def test_checks_page_sorted_escaped_and_cached(client):
    login(client)
    html = client.get('/checks').get_data(as_text=True)
    assert html.index('Missing indices') < html.index('Maintenance window')     # Fehler vor Warnung
    assert '&lt;i&gt;gesetzt' in html and 'javascript:alert' not in html          # escaped, unsichere Links verworfen
    assert 'https://docs.nextcloud.com/x' in html
    dash = client.get('/').get_data(as_text=True)
    assert '1 Fehler' in dash and '1 Warnungen' in dash


def test_maintenance_job_invalidates_checks_cache(client):
    import jobs
    login(client)
    client.get('/checks')
    assert jobs.cache_get('setupchecks')[0] is not None
    r = post(client, '/maintenance', '/run/files_scan')
    wait_job(client, r.headers['Location'])
    assert jobs.cache_get('setupchecks')[0] is None


def test_apps_page_and_update(client):
    login(client)
    html = client.get('/apps').get_data(as_text=True)
    assert 'calendar' in html and '4.7.15' in html and '4.7.16' in html
    assert '1 verfügbar' in client.get('/').get_data(as_text=True)
    r = post(client, '/apps', '/apps/update', app='calendar')
    assert wait_job(client, r.headers['Location'])['output'] == 'app_update calendar'
    assert post(client, '/apps', '/apps/update', app='../evil').status_code == 400
    r = post(client, '/apps', '/apps/update', app='*')
    assert wait_job(client, r.headers['Location'])['output'] == 'app_update_all'


def test_backups_page_create_delete(client):
    login(client)
    html = client.get('/backups').get_data(as_text=True)
    assert '01.10.2026 12:00' in html and 'Nextcloud 30.0.1' in html
    r = post(client, '/backups', '/backups/create')
    assert wait_job(client, r.headers['Location'])['output'] == 'backup'
    r = post(client, '/backups', '/backups/delete', name='20261001-120000')
    assert wait_job(client, r.headers['Location'])['output'] == 'backup_delete 20261001-120000'
    assert post(client, '/backups', '/backups/delete', name='../../etc').status_code == 400


def test_update_passes_flags(client):
    login(client)
    r = post(client, '/update', '/update/start', backup='yes', with_backup='yes', with_apps='yes')
    import jobs
    jid = int(r.headers['Location'].rsplit('/', 1)[-1])
    assert jobs.get_job(jid)['args'] == ['backup', 'apps']


def test_logs_filter(client):
    login(client)
    html = client.get('/logs?level=2').get_data(as_text=True)
    assert 'Datei &lt;b&gt;fehlt' in html and 'nur info' not in html and 'kaputte Zeile' in html
    assert 'nur info' in client.get('/logs?level=0').get_data(as_text=True)
    html = client.get('/logs?level=0&q=core').get_data(as_text=True)
    assert 'nur info' in html and 'Datei &lt;b&gt;fehlt' not in html


def test_fpm_recommendation_and_set(client):
    import app as appmod
    login(client)
    html = client.get('/fpm').get_data(as_text=True)
    assert '7× im Log' in html
    info = {'mem_total_kb': 4000000, 'mem_available_kb': 2000000, 'versions': {'8.2': {'pools': {'www': {
        'workers': 3, 'worker_kb_total': 240000, 'worker_kb_avg': 80000}}}}}
    rec, basis = appmod.fpm_recommendation(info, '8.2', 'www')
    mc, st, lo, hi = (int(rec[k]) for k in ('pm.max_children', 'pm.start_servers', 'pm.min_spare_servers', 'pm.max_spare_servers'))
    assert 1 <= lo <= st <= hi <= mc and 15 <= mc <= 25          # (4 GB*0.85 - 1.76 GB) / 78 MB ≈ 20
    r = post(client, '/fpm', '/fpm/set', version='8.2', pool='www', pm='dynamic', **{'pm.max_children': '20'})
    assert wait_job(client, r.headers['Location'])['output'] == 'fpm_set 8.2 www pm=dynamic pm.max_children=20'
    assert post(client, '/fpm', '/fpm/set', version='8.2', pool='www', **{'pm.max_children': '1;rm'}).status_code == 400
    assert post(client, '/fpm', '/fpm/set', version='8.2; x', pool='www', pm='static').status_code == 400


def test_small_ram_recommendation_stays_valid():
    import app as appmod
    info = {'mem_total_kb': 1000000, 'mem_available_kb': 150000, 'versions': {'8.2': {'pools': {'www': {
        'workers': 2, 'worker_kb_total': 100000, 'worker_kb_avg': 50000}}}}}
    rec, _ = appmod.fpm_recommendation(info, '8.2', 'www')
    mc, st, lo, hi = (int(rec[k]) for k in ('pm.max_children', 'pm.start_servers', 'pm.min_spare_servers', 'pm.max_spare_servers'))
    assert mc >= 2 and 1 <= lo <= st <= hi <= mc


def test_all_pages_render(client):
    login(client)
    for page in ('/', '/checks', '/php', '/fpm', '/apps', '/update', '/backups', '/maintenance', '/logs', '/history'):
        html = client.get(page).get_data(as_text=True)
        assert html.count('<div') == html.count('</div>'), f'{page}: <div> nicht ausgeglichen'


# ------------------------------------------------------------------ Fehlerfälle (v0.5.0 Hotfix)

def test_logs_page_survives_wrapper_failure(client):        # war: HTTP 500
    login(client)
    r = client.get('/logs?lines=2000')
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'Argument list too long' in html


def test_logs_odd_levels(client):
    login(client)
    html = client.get('/logs?level=3').get_data(as_text=True)
    assert 'Level als Text' in html and 'Level fehlt' in html


def test_500_page_is_friendly(client, monkeypatch):
    import app as appmod
    login(client)
    appmod.app.config['PROPAGATE_EXCEPTIONS'] = False
    appmod.app.testing = False
    monkeypatch.setattr(appmod, 'call_json', lambda *a: 1 / 0)
    r = client.get('/backups')
    assert r.status_code == 500 and 'journalctl -u nc-manager' in r.get_data(as_text=True)


# ------------------------------------------------------------------ v0.6.1: Backups, Restore, Diagnose

def test_backups_page_shows_data_and_verify_status(client):
    login(client)
    html = client.get('/backups').get_data(as_text=True)
    assert 'mit Benutzerdaten' in html and 'ohne Benutzerdaten' in html
    assert 'geprüft vor 5 min' in html and 'Prüfung fehlgeschlagen' in html
    assert 'config.php wurde verändert' in html and 'Anlass: manuell' in html


def test_full_backup_and_verify_are_jobs(client):
    login(client)
    r = post(client, '/backups', '/backups/create', with_data='yes')
    assert wait_job(client, r.headers['Location'])['output'] == 'backup_full'
    r = post(client, '/backups', '/backups/verify', name='20261002-120000')
    assert wait_job(client, r.headers['Location'])['output'] == 'backup_verify 20261002-120000'
    assert post(client, '/backups', '/backups/verify', name='../x').status_code == 400


def test_restore_plan_ok_and_blocked(client):
    login(client)
    html = client.get('/backups/20261002-120000/restore').get_data(as_text=True)
    assert 'werden ebenfalls zurückgespielt' in html and 'class="bad">✗' not in html
    assert 'Wiederherstellung starten</button>' in html and 'disabled>Wiederherstellung' not in html
    # älteres Backup: ohne Prüfsummen, Prüfung fehlgeschlagen, Mountpoint ist hier egal (keine Benutzerdaten)
    html = client.get('/backups/20261001-120000/restore').get_data(as_text=True)
    assert 'letzte Prüfung dieses Backups ist fehlgeschlagen' in html
    assert 'keine Prüfsummen' in html and 'Alle Dateien neu einlesen' in html
    assert 'Mountpoint' not in html
    assert ' disabled>Wiederherstellung starten' in html
    assert client.get('/backups/20991231-000000/restore').status_code == 404


def test_restore_plan_blocked_by_untrusted_datadir(client, monkeypatch):   # 0.6.4
    import app as appmod
    orig = appmod.call_json_args

    def fake(action, *args):
        data, err = orig(action, *args)
        if action == 'restore_plan':
            data = dict(data, datadir='/etc', datadir_problem='Datenverzeichnis in config.php (/etc) weicht vom '
                                                             'bei der Installation festgehaltenen (/srv/data) ab.')
        return data, err
    monkeypatch.setattr(appmod, 'call_json_args', fake)
    login(client)
    html = client.get('/backups/20261002-120000/restore').get_data(as_text=True)
    assert 'weicht vom bei der Installation festgehaltenen' in html
    assert ' disabled>Wiederherstellung starten' in html


def test_maintenance_page_explains_bigint_and_auto_expiry(client):   # 0.6.5
    login(client)
    html = client.get('/maintenance').get_data(as_text=True)
    assert 'BigInt-Konvertierung:' in html and '2,1 Milliarden' in html
    assert 'auf „auto“' in html


def test_restore_needs_typed_confirmation(client):
    login(client)
    t = csrf(client, '/backups/20261002-120000/restore')
    r = client.post('/backups/restore', data={'_csrf': t, 'name': '20261002-120000', 'confirm': 'ja'})
    assert r.status_code == 400 and 'nichts verändert' in r.get_data(as_text=True)
    r = client.post('/backups/restore', data={'_csrf': t, 'name': '20261002-120000', 'confirm': 'WIEDERHERSTELLEN'})
    assert wait_job(client, r.headers['Location'])['output'] == 'restore_backup 20261002-120000'


def test_diagnose_page(client):
    login(client)
    html = client.get('/diagnose').get_data(as_text=True)
    assert '10.11.6-MariaDB' in html and '50.0 MB' in html and 'PONG' in html
    assert 'cron.php' in html and 'läuft' in html and 'Hinweise' not in html


def test_php_ini_sources(client):
    login(client)
    html = client.get('/php').get_data(as_text=True)
    sect = html[html.index('Wo stehen die Werte?'):html.index('Geladene PHP-Konfigurationen')]
    assert '99-nextcloud-manager.ini' in sect and sect.count('wirksam') == 2 and 'überschrieben' in sect
    assert 'post_max_size' not in sect          # keine Fundstellen -> nicht aufgeführt


def test_call_cache_is_cleared_by_jobs(client):
    import jobs
    login(client)
    client.get('/')
    assert jobs.cache_get('call-dashboard')[0] is not None
    r = post(client, '/maintenance', '/run/repair')
    wait_job(client, r.headers['Location'])
    assert jobs.cache_get('call-dashboard')[0] is None


# ------------------------------------------------------------------ v0.6.2: Lizenz

def test_license_page_public_and_footer(client):
    r = client.get('/lizenz')                       # ohne Anmeldung erreichbar
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert 'roswitina@hotmail.com' in html and 'Permission is hereby granted' in html
    assert '<h3>3. Besondere Risiken dieser Software</h3>' in html and '<b>„wie besehen“ (as is)</b>' in html
    assert 'nicht mit ihr verbunden' in html or 'in keiner Verbindung' in html
    login_html = client.get('/login').get_data(as_text=True)
    assert 'MIT-Lizenz' in login_html and '/lizenz' in login_html


def test_source_files_carry_license_header():
    root = os.path.dirname(HERE)
    for f in ('app.py', 'jobs.py', 'ncm_helper.py', 'nc-manager-cmd', 'install.sh', 'uninstall.sh',
              'static/app.js', 'static/style.css', 'templates/base.html'):
        head = open(os.path.join(root, f), encoding='utf-8').read(600)
        assert 'roswitina@hotmail.com' in head and 'SPDX-License-Identifier: MIT' in head, f
    assert 'MIT License' in open(os.path.join(root, 'LICENSE')).read()


# ------------------------------------------------------------------ v0.6.3: Log archivieren, Rotation, „nur neue“

def test_logs_file_card(client):
    login(client)
    html = client.get('/logs').get_data(as_text=True)
    assert '/srv/data/nextcloud.log' in html and '5.0 MB' in html
    assert 'ab 100.0 MB' in html                      # leer in config.php -> Nextcloud-Standard 100 MB
    assert '3 im Backup-Verzeichnis' in html and 'Archivieren und leeren' in html


def test_logs_since_filter(client):
    login(client)
    html = client.get('/logs?level=3').get_data(as_text=True)
    assert 'uralter Fehler' in html and 'ganz neuer Fehler' in html
    t = csrf(client, '/logs')
    client.post('/logs/since', data={'_csrf': t, 'level': '3', 'lines': '500'})
    html = client.get('/logs?level=3').get_data(as_text=True)
    assert 'uralter Fehler' not in html and 'ganz neuer Fehler' in html
    assert 'ältere ausgeblendet' in html and 'Wieder alle zeigen' in html
    client.post('/logs/since', data={'_csrf': t, 'mode': 'clear'})
    assert 'uralter Fehler' in client.get('/logs?level=3').get_data(as_text=True)


def test_logs_actions_are_jobs_and_validated(client):
    login(client)
    r = post(client, '/logs', '/logs/archive')
    assert wait_job(client, r.headers['Location'])['output'] == 'log_archive'
    r = post(client, '/logs', '/logs/rotate', size='52428800')
    assert wait_job(client, r.headers['Location'])['output'] == 'log_rotate_set 52428800'
    assert post(client, '/logs', '/logs/rotate', size='123').status_code == 400
    r = post(client, '/logs', '/logs/level', level='3')
    assert wait_job(client, r.headers['Location'])['output'] == 'log_level_set 3'
    assert post(client, '/logs', '/logs/level', level='9').status_code == 400
