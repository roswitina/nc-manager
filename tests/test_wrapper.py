# Nextcloud Server Manager
# Copyright (c) 2026 roswitina@hotmail.com
# SPDX-License-Identifier: MIT
# Lizenz: siehe LICENSE · Gewährleistungs- und Haftungsausschluss: siehe HAFTUNGSAUSSCHLUSS.md
"""Tests des echten Wrappers gegen eine simulierte Nextcloud.
Laufen nur als root mit PHP-CLI, runuser und Benutzer www-data (sonst übersprungen).
Die MariaDB-Tests laufen zusätzlich nur, wenn ein lokaler MariaDB-Server per Socket erreichbar ist."""
import gzip
import json
import os
import pwd
import shutil
import sqlite3
import subprocess
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

OCC = r'''<?php
$a = array_values(array_diff(array_slice($argv, 1), ["--no-interaction"]));
$cfgf = __DIR__ . "/config/config.php"; $CONFIG = []; include $cfgf; $m = !empty($CONFIG["maintenance"]);
$set = function ($v) use ($cfgf) { $CONFIG = []; include $cfgf; $CONFIG["maintenance"] = $v;
  file_put_contents($cfgf, "<?php\n\$CONFIG = " . var_export($CONFIG, true) . ";\n"); };
switch ($a[0] ?? "") {
  case "status": echo in_array("--output=json", $a) ? json_encode(["versionstring" => "30.0.1", "maintenance" => $m, "needsDbUpgrade" => false]) : "  - maintenance: " . ($m ? "true" : "false") . "\n"; break;
  case "maintenance:mode": $set(in_array("--on", $a)); echo "ok\n"; break;
  case "config:list": echo json_encode(["apps" => ["core" => ["backgroundjobs_mode" => "cron", "lastcron" => (string) (time() - 60), "x" => str_repeat("y", 200000)]]]); break;
  case "app:list": echo json_encode(["enabled" => array_fill_keys(array_map(fn($i) => "app$i", range(1, 3000)), "1.0.0")]); break;
  default: echo "ok\n";
}
'''


def _can_run():
    try:
        pwd.getpwnam('www-data')
    except KeyError:
        return False
    return os.geteuid() == 0 and shutil.which('php') and shutil.which('runuser')


def _mariadb_available():
    return shutil.which('mariadb') and subprocess.run(['mariadb', '-e', 'SELECT 1'], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _can_run(), reason='braucht root, php, runuser und www-data')


def make_sim(dbtype='sqlite3'):
    d = tempfile.mkdtemp(prefix='ncsim-', dir='/tmp')
    os.chmod(d, 0o755)
    nc, data, bk = os.path.join(d, 'www', 'nextcloud'), os.path.join(d, 'data'), os.path.join(d, 'bk')
    for p in (os.path.join(nc, 'config'), os.path.join(nc, 'updater'), os.path.join(data, 'admin', 'files'), bk):
        os.makedirs(p)
    os.chmod(os.path.join(d, 'www'), 0o755)
    open(os.path.join(nc, 'occ'), 'w').write(OCC)
    open(os.path.join(nc, 'version.php'), 'w').write('<?php // original\n')
    open(os.path.join(nc, 'updater', 'updater.phar'), 'w').write('<?php echo "updater\\n";')
    cfg = {'datadirectory': data, 'dbtype': dbtype, 'maintenance': False,
           'redis': {'host': 'localhost', 'port': 6379, 'password': 'REDIS-GEHEIM'}}
    if dbtype == 'mysql':
        cfg.update(dbhost='localhost', dbname='ncm_test', dbuser='ncm_test', dbpassword='pw"x')
    else:
        cfg['dbname'] = 'owncloud'
        con = sqlite3.connect(os.path.join(data, 'owncloud.db'))
        con.execute('create table oc_t(a)')
        con.execute('insert into oc_t values(1)')
        con.commit()
        con.close()
    php_cfg = 'array(' + ', '.join(f"'{k}' => " + (
        'false' if v is False else ("array('host'=>'localhost','port'=>6379,'password'=>'REDIS-GEHEIM')" if k == 'redis'
                                    else "'" + str(v).replace("'", "\\'") + "'")) for k, v in cfg.items()) + ')'
    open(os.path.join(nc, 'config', 'config.php'), 'w').write(f'<?php\n$CONFIG = {php_cfg};\n')
    open(os.path.join(data, '.ocdata'), 'w').close()
    for i in range(20):
        open(os.path.join(data, 'admin', 'files', f'f{i}'), 'wb').write(os.urandom(2000))
    with open(os.path.join(nc, '.user.ini'), 'w') as f:
        f.write("upload_max_filesize=511M\noutput_buffering=0\ndefault_charset='UTF-8'\n")
    with open(os.path.join(data, 'nextcloud.log'), 'w') as f:
        for i in range(2500):
            f.write(json.dumps({'level': i % 5, 'time': 't', 'app': 'core', 'message': f'Meldung {i} ' + 'x' * 300}) + '\n')
    subprocess.run(['chown', '-R', 'www-data:www-data', nc, data], check=True)
    envf = os.path.join(d, 'env')
    with open(envf, 'w') as f:
        f.write(f"NCM_BACKEND=occ\nNCM_PHP={shutil.which('php')}\nNCM_NC_PATH={nc}\nNCM_WEBUSER=www-data\n"
                f"NCM_BACKUP_DIR={bk}\nNCM_BACKUP_KEEP=10\nNCM_DATADIR={data}\n")
    env = dict(os.environ, NCM_ENV_FILE=envf, NCM_LOCK=os.path.join(d, 'lock'),
               NCM_HELPER=os.path.join(ROOT, 'ncm_helper.py'), NCM_HOOKS_DIR=os.path.join(d, 'hooks'))
    env.pop('SUDO_USER', None)
    return {'dir': d, 'nc': nc, 'data': data, 'bk': bk, 'env': env}


@pytest.fixture()
def sim():
    s = make_sim()
    yield s
    shutil.rmtree(s['dir'])


def run(s, *args):
    return subprocess.run([os.path.join(ROOT, 'nc-manager-cmd'), *args], env=s['env'], capture_output=True, text=True)


def maintenance(s):
    return "'maintenance' => true" in open(os.path.join(s['nc'], 'config', 'config.php')).read()


def latest_backup(s):
    return sorted(n for n in os.listdir(s['bk']) if n[:2] == '20')[-1]


# ------------------------------------------------------------------ bisherige Fälle

def test_logs_large_output(sim):          # 0.5.1: "Argument list too long" ab 500 Zeilen
    r = run(sim, 'logs', '2000')
    assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)['nextcloud']['entries']) == 2000


def test_dashboard_large_json_and_no_secrets(sim):
    r = run(sim, 'dashboard')
    assert r.returncode == 0, r.stderr
    assert len(json.loads(r.stdout)['core']['apps']['core']['x']) == 200000
    assert 'REDIS-GEHEIM' not in r.stdout
    r = run(sim, 'diagnose')
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert 'REDIS-GEHEIM' not in r.stdout and d['redis']['password_set'] is True
    assert d['db']['ok'] and d['db']['tables'] == 1


def test_php_values_reports_user_ini_and_sources(sim):
    r = run(sim, 'php_values')
    assert r.returncode == 0, r.stderr
    j = json.loads(r.stdout)
    assert j['overrides']['user_ini']['upload_max_filesize'] == '511M'
    assert 'memory_limit' in j['sources']


def test_rejects_unknown_action_and_bad_args(sim):
    assert run(sim, 'rm', '-rf', '/').returncode == 64
    assert run(sim, 'php_set', 'output_buffering', 'Off').returncode == 65
    assert run(sim, 'restore_backup', '../../etc').returncode == 65
    assert run(sim, 'backup_verify', '20990101-000000').returncode == 65


def test_no_temp_files_left(sim):
    before = set(os.listdir('/tmp'))
    run(sim, 'logs', '500')
    run(sim, 'dashboard')
    run(sim, 'diagnose')
    assert not [f for f in set(os.listdir('/tmp')) - before if f.startswith('tmp.')]


# ------------------------------------------------------------------ Backup und Prüfung

def test_backup_has_checksums_and_verifies(sim):
    r = run(sim, 'backup')
    assert r.returncode == 0, r.stdout + r.stderr
    b = latest_backup(sim)
    files = os.listdir(os.path.join(sim['bk'], b))
    assert {'checksums.sha256', 'info.json', 'database.sqlite.gz', 'nextcloud-code.tar.gz', 'config.php'} <= set(files)
    assert 'nextcloud-data.tar.gz' not in files
    r = run(sim, 'backup_verify', b)
    assert r.returncode == 0 and 'Backup in Ordnung' in r.stdout, r.stdout
    assert 'NICHT in diesem Backup' in r.stdout


def test_verify_detects_tampering_and_truncation(sim):
    run(sim, 'backup')
    b = latest_backup(sim)
    p = os.path.join(sim['bk'], b)
    open(os.path.join(p, 'config.php'), 'w').write('<?php $CONFIG = ["manipuliert" => 1];')
    r = run(sim, 'backup_verify', b)
    assert r.returncode == 1 and 'config.php wurde verändert' in r.stdout
    # Abgeschnittener Dump mit passend neu berechneter Prüfsumme -> Strukturprüfung muss es finden
    db = os.path.join(p, 'database.sqlite.gz')
    raw = open(db, 'rb').read()
    open(db, 'wb').write(raw[:len(raw) // 2])
    subprocess.run(f'cd {p} && sha256sum -- $(ls | grep -v -e checksums -e verify) > checksums.sha256', shell=True, check=True)
    r = run(sim, 'backup_verify', b)
    assert r.returncode == 1 and 'SQLite-Backup beschädigt' in r.stdout, r.stdout


def test_full_backup_includes_data_and_ends_maintenance(sim):
    r = run(sim, 'backup_full')
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'Wartungsmodus ein' in r.stdout and not maintenance(sim)
    b = latest_backup(sim)
    info = json.load(open(os.path.join(sim['bk'], b, 'info.json')))
    assert info['user_data_included'] is True
    r = run(sim, 'backup_verify', b)
    assert r.returncode == 0 and '✓ Benutzerdaten' in r.stdout, r.stdout


# ------------------------------------------------------------------ Restore (SQLite)

def test_restore_full_backup_sqlite(sim):
    run(sim, 'backup_full')
    b = latest_backup(sim)
    dbf = os.path.join(sim['data'], 'owncloud.db')
    subprocess.run(['runuser', '-u', 'www-data', '--', 'python3', '-c',
                    f"import sqlite3;c=sqlite3.connect('{dbf}');c.execute('create table oc_neu(a)');"
                    "c.execute('update oc_t set a=99');c.commit()"], check=True)
    open(os.path.join(sim['data'], 'admin', 'files', 'neu.txt'), 'w').write('neu')
    os.unlink(os.path.join(sim['data'], 'admin', 'files', 'f0'))
    open(os.path.join(sim['nc'], 'version.php'), 'w').write('<?php // geändert\n')

    r = run(sim, 'restore_backup', b)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'WIEDERHERSTELLUNG ABGESCHLOSSEN' in r.stdout
    con = sqlite3.connect(dbf)
    assert [t for (t,) in con.execute("select name from sqlite_master where type='table'")] == ['oc_t']
    assert con.execute('select a from oc_t').fetchone() == (1,)
    st = os.stat(dbf)
    assert pwd.getpwuid(st.st_uid).pw_name == 'www-data'                 # 0.6.0: root:root -> schreibgeschützt
    assert os.path.exists(os.path.join(sim['data'], 'admin', 'files', 'f0'))
    assert not os.path.exists(os.path.join(sim['data'], 'admin', 'files', 'neu.txt'))
    assert 'original' in open(os.path.join(sim['nc'], 'version.php')).read()
    assert not maintenance(sim)
    parent = os.path.dirname(sim['nc'])
    assert [n for n in os.listdir(parent) if n.startswith('nextcloud.ncm-before-restore-')]
    assert not [n for n in os.listdir(parent) if n.startswith('.ncm-restore-')]
    assert 'Sicherheits-Backup' in r.stdout and len([n for n in os.listdir(sim['bk']) if n[:2] == '20']) == 2


def test_restore_refuses_foreign_backup(sim):
    run(sim, 'backup')
    b = latest_backup(sim)
    p = os.path.join(sim['bk'], b, 'info.json')
    info = json.load(open(p))
    info['nc_path'] = '/srv/andere/nextcloud'
    json.dump(info, open(p, 'w'))
    subprocess.run(f"cd {os.path.join(sim['bk'], b)} && sha256sum -- $(ls | grep -v -e checksums -e verify) > checksums.sha256",
                   shell=True, check=True)
    r = run(sim, 'restore_backup', b)
    assert r.returncode == 67 and 'gehört zu /srv/andere/nextcloud' in r.stdout
    assert not maintenance(sim)


def test_restore_refuses_broken_backup_without_changes(sim):
    run(sim, 'backup')
    b = latest_backup(sim)
    open(os.path.join(sim['bk'], b, 'config.php'), 'a').write('// x')
    r = run(sim, 'restore_backup', b)
    assert r.returncode == 66 and 'nichts verändert' in r.stdout
    assert not maintenance(sim)


# ------------------------------------------------------------------ Hooks

def test_hooks_require_safe_permissions(sim):
    hooks = sim['env']['NCM_HOOKS_DIR']
    os.makedirs(hooks)
    os.chmod(hooks, 0o755)
    h = os.path.join(hooks, 'pre-update')
    open(h, 'w').write('#!/bin/sh\necho "HOOK LAEUFT $NCM_NC_PATH"\n')
    os.chmod(h, 0o777)
    r = run(sim, 'nextcloud_update')
    assert r.returncode == 18 and 'wird nicht ausgeführt' in r.stdout and 'HOOK LAEUFT' not in r.stdout
    os.chmod(h, 0o755)
    open(os.path.join(hooks, 'post-backup'), 'w').write('#!/bin/sh\necho "NACH BACKUP $NCM_BACKUP_PATH"\n')
    os.chmod(os.path.join(hooks, 'post-backup'), 0o700)
    r = run(sim, 'nextcloud_update', 'backup')
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'HOOK LAEUFT' in r.stdout and 'NACH BACKUP' in r.stdout


# ------------------------------------------------------------------ Restore (MariaDB)

@pytest.fixture()
def mysim():
    if not _mariadb_available():
        pytest.skip('kein lokaler MariaDB-Server')
    q = lambda sql: subprocess.run(['mariadb', '-N', '-e', sql], capture_output=True, text=True, check=True).stdout
    q("DROP DATABASE IF EXISTS ncm_test; CREATE DATABASE ncm_test; "
      "CREATE USER IF NOT EXISTS 'ncm_test'@'localhost' IDENTIFIED BY 'pw\"x'; GRANT ALL ON ncm_test.* TO 'ncm_test'@'localhost'; "
      "CREATE TABLE ncm_test.oc_t(a int primary key); CREATE TABLE ncm_test.oc_child(id int, t int, FOREIGN KEY (t) REFERENCES ncm_test.oc_t(a)); "
      "INSERT INTO ncm_test.oc_t VALUES (1); INSERT INTO ncm_test.oc_child VALUES (1,1);")
    s = make_sim('mysql')
    s['q'] = q
    yield s
    shutil.rmtree(s['dir'])
    q('DROP DATABASE IF EXISTS ncm_test')


def test_mysql_restore_removes_newer_tables(mysim):
    q = mysim['q']
    assert run(mysim, 'backup').returncode == 0
    b = latest_backup(mysim)
    q("CREATE TABLE ncm_test.oc_neu(id int, t int, FOREIGN KEY (t) REFERENCES ncm_test.oc_t(a)); "
      "INSERT INTO ncm_test.oc_neu VALUES (1,1); UPDATE ncm_test.oc_child SET id=42;")
    r = run(mysim, 'restore_backup', b)
    assert r.returncode == 0, r.stdout + r.stderr
    assert q('SHOW TABLES FROM ncm_test').split() == ['oc_child', 'oc_t']
    assert q('SELECT id FROM ncm_test.oc_child').strip() == '1'
    assert not maintenance(mysim)


def test_mysql_failed_import_rolls_back(mysim):
    q = mysim['q']
    run(mysim, 'backup')
    b = latest_backup(mysim)
    p = os.path.join(mysim['bk'], b)
    sql = gzip.open(os.path.join(p, 'database.sql.gz'), 'rt').read().replace('INSERT INTO `oc_t`', 'INSERT INTO `gibts_nicht`', 1)
    with gzip.open(os.path.join(p, 'database.sql.gz'), 'wt') as f:
        f.write(sql)
    subprocess.run(f'cd {p} && sha256sum -- $(ls | grep -v -e checksums -e verify) > checksums.sha256', shell=True, check=True)
    q('UPDATE ncm_test.oc_child SET id=7')
    r = run(mysim, 'restore_backup', b)
    assert r.returncode == 69, r.stdout
    assert 'auf den Stand vor dem Restore zurückgesetzt' in r.stdout
    assert q('SELECT id FROM ncm_test.oc_child').strip() == '7'
    assert maintenance(mysim)                                       # bleibt bewusst an
    assert 'original' in open(os.path.join(mysim['nc'], 'version.php')).read()


# ------------------------------------------------------------------ v0.6.3: Log archivieren und leeren

def test_log_archive_and_truncate(sim):
    lf = os.path.join(sim['data'], 'nextcloud.log')
    st0 = os.stat(lf)
    r = run(sim, 'log_archive')
    assert r.returncode == 0, r.stdout + r.stderr
    st1 = os.stat(lf)
    assert st1.st_size == 0 and st1.st_uid == st0.st_uid and (st1.st_mode & 0o777) == (st0.st_mode & 0o777)
    arch = os.path.join(sim['bk'], 'logs')
    files = os.listdir(arch)
    assert len(files) == 1 and files[0].endswith('.gz')
    content = gzip.open(os.path.join(arch, files[0]), 'rt').read()
    assert content.count('\n') == 2500 and 'Meldung 2499' in content
    assert (os.stat(os.path.join(arch, files[0])).st_mode & 0o777) == 0o600
    j = json.loads(run(sim, 'logs', '200').stdout)
    assert j['nextcloud']['size'] == 0 and j['archives'] == '1'


def test_log_archive_refuses_files_webuser_cannot_write(sim):
    # Ein in config.php eingetragener Pfad auf eine root-Datei darf nicht geleert werden.
    victim = os.path.join(sim['dir'], 'root-datei')
    open(victim, 'w').write('wichtig\n')
    os.chmod(victim, 0o644)
    cfg = os.path.join(sim['nc'], 'config', 'config.php')
    s = open(cfg).read().replace("'maintenance' => false", f"'maintenance' => false, 'logfile' => '{victim}'")
    open(cfg, 'w').write(s)
    r = run(sim, 'log_archive')
    assert r.returncode == 36 and 'nicht beschreibbar' in r.stdout
    assert open(victim).read() == 'wichtig\n'
    # Lesen der Datei über die Logs-Seite ebenfalls nur als Webserver-Benutzer
    os.chmod(victim, 0o600)
    j = json.loads(run(sim, 'logs', '200').stdout)
    assert j['nextcloud']['ok'] is False


def test_log_settings_validated(sim):
    assert run(sim, 'log_rotate_set', '12abc').returncode == 65
    assert run(sim, 'log_level_set', '7').returncode == 65
    r = run(sim, 'log_rotate_set', '52428800')
    assert r.returncode == 0 and '50 MiB' in r.stdout


# ------------------------------------------------------------------ v0.6.4: Werte aus config.php nicht blind vertrauen

def set_config(s, **kv):
    """Ändert Werte in config.php wie es ein Angreifer mit Rechten des Webserver-Benutzers könnte."""
    import re
    cfg = os.path.join(s['nc'], 'config', 'config.php')
    src = open(cfg).read()
    for k, v in kv.items():
        src = re.sub(rf", '{re.escape(k)}' => '[^']*'|'{re.escape(k)}' => '[^']*', ", '', src)
        src = src.replace("'maintenance' => false", f"'maintenance' => false, '{k}' => '{v}'")
    open(cfg, 'w').write(src)


def _victim(s):
    v = os.path.join(s['dir'], 'opfer')
    os.makedirs(os.path.join(v, 'sub'))
    open(os.path.join(v, 'sub', 'datei'), 'w').write('root\n')
    return v


def _owned_by_root(path):
    return all(os.stat(os.path.join(dp, n)).st_uid == 0
               for dp, dns, fns in os.walk(path) for n in dns + fns + ['.'])


def test_restore_refuses_manipulated_datadir(sim):
    assert run(sim, 'backup_full').returncode == 0
    b = latest_backup(sim)
    victim = _victim(sim)
    set_config(sim, datadirectory=victim)          # Datenverzeichnis nachträglich umgebogen
    r = run(sim, 'restore_backup', b)
    assert r.returncode == 67 and 'weicht vom bei der Installation festgehaltenen' in r.stdout, r.stdout
    assert 'nichts verändert' in r.stdout
    assert _owned_by_root(victim) and os.path.isdir(sim['data'])
    assert not maintenance(sim)


def test_backup_refuses_manipulated_datadir(sim):
    set_config(sim, datadirectory='/etc')
    for action in ('backup', 'backup_full'):
        r = run(sim, action)
        assert r.returncode == 38 and 'weicht vom' in r.stdout, r.stdout
    assert not [n for n in os.listdir(sim['bk']) if n[:2] == '20']
    assert not maintenance(sim)


def test_restore_plan_reports_datadir_problem(sim):
    run(sim, 'backup')
    b = latest_backup(sim)
    assert json.loads(run(sim, 'restore_plan', b).stdout)['datadir_problem'] == ''
    set_config(sim, datadirectory='/etc')
    assert 'weicht vom' in json.loads(run(sim, 'restore_plan', b).stdout)['datadir_problem']


def test_datadir_checks_even_if_env_matches(sim):
    def env_datadir(v):
        envf = sim['env']['NCM_ENV_FILE']
        lines = [ln for ln in open(envf).read().splitlines() if not ln.startswith('NCM_DATADIR=')]
        open(envf, 'w').write('\n'.join(lines + ([f'NCM_DATADIR={v}'] if v is not None else [])) + '\n')

    env_datadir(None)                                       # Installation vor 0.6.4
    r = run(sim, 'backup')
    assert r.returncode == 38 and 'install.sh erneut' in r.stdout
    env_datadir(sim['data'])
    os.unlink(os.path.join(sim['data'], '.ocdata'))         # kein Nextcloud-Datenverzeichnis
    r = run(sim, 'backup')
    assert r.returncode == 38 and '.ocdata' in r.stdout
    open(os.path.join(sim['data'], '.ocdata'), 'w').close()
    os.chown(sim['data'], 0, 0)                             # gehört nicht dem Webserver-Benutzer
    r = run(sim, 'backup')
    assert r.returncode == 38 and 'gehört root' in r.stdout
    for bad in ('/etc', '/usr/lib/x', '/'):
        set_config(sim, datadirectory=bad)
        env_datadir(bad)
        r = run(sim, 'backup')
        assert r.returncode == 38 and ('Systemverzeichnis' in r.stdout or 'nicht gesetzt' in r.stdout), (bad, r.stdout)


def _helper(action, conf, *args):
    return subprocess.run(['python3', os.path.join(ROOT, 'ncm_helper.py'), action, *args],
                          input=json.dumps(conf), capture_output=True, text=True)


@pytest.mark.parametrize('bad', [
    {'dbtype': 'mysql', 'dbname': 'nc', 'dbpassword': 'x"\n[mysqldump]\nresult-file=/etc/cron.d/x'},
    {'dbtype': 'mysql', 'dbname': 'nc', 'dbhost': 'localhost\nresult-file=/etc/x'},
    {'dbtype': 'mysql', 'dbname': 'nc', 'dbport': '3306\nresult-file=/etc/x'},
    {'dbtype': 'mysql', 'dbname': '--result-file=/etc/x'},
    {'dbtype': 'pgsql', 'dbname': 'nc', 'dbuser': '-x'},
    {'dbtype': 'sqlite3', 'dbname': '../../etc/passwd', 'datadirectory': '/srv/data'},
    {'dbtype': 'sqlite3', 'dbname': 'owncloud', 'datadirectory': ''},
])
def test_helper_rejects_injected_db_settings(bad, tmp_path):
    target = str(tmp_path / 'database.sql.gz')
    why = ('config.php', 'SQLite')                 # abgelehnt wegen der Prüfung, nicht wegen fehlender Werkzeuge
    r = _helper('db_dump', bad, target)
    assert r.returncode != 0 and any(w in r.stderr for w in why), r.stderr
    assert not os.listdir(tmp_path)
    src = tmp_path / 'x.sql.gz'
    with gzip.open(src, 'wb') as f:
        f.write(b'SELECT 1;')
    r = _helper('db_restore', bad, str(src), 'www-data')
    assert r.returncode != 0 and any(w in r.stderr for w in why), r.stderr
    info = json.loads(_helper('db_info', bad).stdout)
    assert info['ok'] is False and any(w in info['error'] for w in why), info
