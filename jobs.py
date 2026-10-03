#!/usr/bin/env python3
# Nextcloud Server Manager
# Copyright (c) 2026 roswitina@hotmail.com
# SPDX-License-Identifier: MIT
# Lizenz: siehe LICENSE · Gewährleistungs- und Haftungsausschluss: siehe HAFTUNGSAUSSCHLUSS.md
"""Gemeinsame Konfiguration, Datenbank und Hintergrund-Jobs.

Lange Aktionen (Update, Repair, BigInt …) laufen nicht im Gunicorn-Worker,
sondern in einem eigenständigen Prozess:

    python3 jobs.py run <job-id>

Der Prozess löst sich per Double-Fork vom Worker, schreibt die Ausgabe live in
eine Logdatei und trägt Exit-Code und Ausgabe am Ende in die Datenbank ein.
Ein Worker-Timeout oder -Neustart bricht die Aktion daher nicht mehr ab.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

WRAPPER = os.environ.get('NCM_WRAPPER', '/usr/local/sbin/nc-manager-cmd')
SUDO = os.environ.get('NCM_SUDO', 'sudo').split()   # NCM_SUDO='' = Wrapper ohne sudo aufrufen (nur Tests)
STATE_DIR = os.environ.get('NCM_STATE_DIR', '/var/lib/nc-manager')
DB = os.path.join(STATE_DIR, 'history.db')
JOB_DIR = os.path.join(STATE_DIR, 'jobs')

HISTORY_KEEP = 500          # abgeschlossene Jobs, die behalten werden
OUTPUT_KEEP = 200_000       # Zeichen Ausgabe pro Job in der Datenbank
STARTUP_GRACE = 30          # Sekunden, bis ein Job ohne PID als abgebrochen gilt

# Aktionen aus v0.4.3, die nur gelesen haben und nicht ins Protokoll gehören.
READONLY_ACTIONS = ('dashboard', 'php_platform', 'php_values', 'php_info', 'status', 'preflight')


def db():
    """Verbindung im Autocommit-Modus; Transaktionen werden explizit gestartet."""
    c = sqlite3.connect(DB, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(JOB_DIR, mode=0o700, exist_ok=True)
    with closing(db()) as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('BEGIN IMMEDIATE')
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, action TEXT NOT NULL,
                args TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
                rc INTEGER, duration REAL, output TEXT, pid INTEGER)''')
            c.execute('CREATE TABLE IF NOT EXISTS login_fail(ip TEXT NOT NULL, ts INTEGER NOT NULL)')
            # Migration aus v0.4.3: Tabelle "log" übernehmen, Lese-Aufrufe verwerfen.
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='log'").fetchone():
                marks = ','.join('?' * len(READONLY_ACTIONS))
                c.execute(f'''INSERT INTO jobs(ts, action, status, rc, duration, output)
                              SELECT ts, action, 'done', rc, duration, output FROM log
                              WHERE action NOT IN ({marks}) ORDER BY id''', READONLY_ACTIONS)
                c.execute('DROP TABLE log')
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise


def _pid_is_runner(pid):
    if not pid:
        return False
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return b'jobs.py' in f.read()
    except OSError:
        return False


def _reap_dead(c):
    """Jobs, deren Runner nicht mehr existiert, als abgebrochen markieren."""
    now = time.time()
    for r in c.execute("SELECT id, ts, pid FROM jobs WHERE status='running'").fetchall():
        if r['pid'] is None and now - r['ts'] < STARTUP_GRACE:
            continue
        if not _pid_is_runner(r['pid']):
            out = _read_log(r['id']) + '\n\n[Manager] Job-Prozess nicht mehr vorhanden – abgebrochen.'
            c.execute("UPDATE jobs SET status='done', rc=-1, output=? WHERE id=?",
                      (out[-OUTPUT_KEEP:], r['id']))
            _remove_log(r['id'])


def running_job_id():
    with closing(db()) as c:
        _reap_dead(c)
        r = c.execute("SELECT id FROM jobs WHERE status='running' ORDER BY id LIMIT 1").fetchone()
        return r['id'] if r else None


def start_job(action, args=()):
    """Startet einen Job. Rückgabe (job_id, None) oder (None, id_des_laufenden_jobs)."""
    with closing(db()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            _reap_dead(c)
            busy = c.execute("SELECT id FROM jobs WHERE status='running' LIMIT 1").fetchone()
            if busy:
                c.execute('ROLLBACK')
                return None, busy['id']
            jid = c.execute("INSERT INTO jobs(ts, action, args, status) VALUES(?,?,?,'running')",
                            (int(time.time()), action, json.dumps(list(args)))).lastrowid
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise
    # Der Runner forkt sofort und beendet sich, daher ist wait() kurz und es bleiben keine Zombies.
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), 'run', str(jid)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    p.wait(timeout=30)
    prune_history()
    return jid, None


def prune_history():
    with closing(db()) as c:
        c.execute('''DELETE FROM jobs WHERE status='done' AND id NOT IN
                     (SELECT id FROM jobs WHERE status='done' ORDER BY id DESC LIMIT ?)''',
                  (HISTORY_KEEP,))


def get_job(jid):
    with closing(db()) as c:
        _reap_dead(c)
        r = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    if not r:
        return None
    job = dict(r)
    job['args'] = json.loads(job['args'] or '[]')
    if job['status'] == 'running':
        job['output'] = _read_log(jid)
        job['duration'] = time.time() - job['ts']
    return job


def list_jobs(limit=100):
    with closing(db()) as c:
        _reap_dead(c)
        return [dict(r) for r in c.execute(
            'SELECT id, ts, action, args, status, rc, duration, output FROM jobs ORDER BY id DESC LIMIT ?',
            (limit,))]


def _log_path(jid):
    return os.path.join(JOB_DIR, f'{int(jid)}.log')


def _read_log(jid, limit=OUTPUT_KEEP):
    try:
        with open(_log_path(jid), 'rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return ''


def _remove_log(jid):
    try:
        os.unlink(_log_path(jid))
    except OSError:
        pass


def _run(jid):
    if os.fork():           # Elternprozess kehrt sofort zum Web-Worker zurück
        os._exit(0)
    os.setsid()
    with closing(db()) as c:
        row = c.execute('SELECT action, args FROM jobs WHERE id=?', (jid,)).fetchone()
        if not row:
            os._exit(1)
        c.execute('UPDATE jobs SET pid=? WHERE id=?', (os.getpid(), jid))
    action, args = row['action'], json.loads(row['args'] or '[]')
    started = time.time()
    # Bewusst kein Timeout: ein Update darf nicht mittendrin abgebrochen werden.
    with open(_log_path(jid), 'wb') as log:
        try:
            rc = subprocess.run([*SUDO, WRAPPER, action, *args], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT).returncode
        except OSError as e:
            log.write(f'FEHLER: {e}\n'.encode())
            rc = 255
    out = _read_log(jid).strip() or '(keine Ausgabe)'
    with closing(db()) as c:
        c.execute("UPDATE jobs SET status='done', rc=?, duration=?, output=? WHERE id=?",
                  (rc, time.time() - started, out, jid))
    _remove_log(jid)
    os._exit(0)


# ------------------------------------------------------------------ Cache für langsame Abfragen

def _cache_path(name):
    return os.path.join(STATE_DIR, f'cache-{name}.json')


def cache_get(name):
    """(daten, zeitstempel) oder (None, None)."""
    try:
        with open(_cache_path(name)) as f:
            c = json.load(f)
        return c['data'], c['ts']
    except (OSError, ValueError, KeyError):
        return None, None


def cache_set(name, data):
    tmp = _cache_path(name) + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'ts': int(time.time()), 'data': data}, f)
    os.replace(tmp, _cache_path(name))


def cache_clear(name):
    try:
        os.unlink(_cache_path(name))
    except OSError:
        pass


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'run':
        _run(int(sys.argv[2]))
    sys.exit('Verwendung: jobs.py run <job-id>')
