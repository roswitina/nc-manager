#!/usr/bin/env python3
# Nextcloud Server Manager
# Copyright (c) 2026 roswitina@hotmail.com
# SPDX-License-Identifier: MIT
# Lizenz: siehe LICENSE · Gewährleistungs- und Haftungsausschluss: siehe HAFTUNGSAUSSCHLUSS.md
"""Hilfsfunktionen für nc-manager-cmd. Läuft als root, installiert root-eigen
unter /usr/local/lib/nc-manager/ncm_helper.py.

Keine Zugangsdaten über Argumente: DB-Zugangsdaten kommen ausschließlich über stdin.
Kompatibel mit Python 3.9 (Debian 11 / DietPi).
"""
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib

OVERRIDE_NAME = 'zzzz-nextcloud-manager.conf'   # sortiert nach allen üblichen Pool-Dateien
POOL_KEYS = {
    'pm': r'dynamic|ondemand|static',
    'pm.max_children': r'[0-9]{1,4}',
    'pm.start_servers': r'[0-9]{1,4}',
    'pm.min_spare_servers': r'[0-9]{1,4}',
    'pm.max_spare_servers': r'[0-9]{1,4}',
    'pm.max_requests': r'[0-9]{1,6}',
}
BACKUP_RE = re.compile(r'^[0-9]{8}-[0-9]{6}$')


def out(obj):
    print(json.dumps(obj, ensure_ascii=False))


def tail_lines(path, n, max_bytes=8 * 1024 * 1024):
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return None
    lines = data.decode('utf-8', 'replace').splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]          # erste Zeile ist evtl. abgeschnitten
    return lines[-n:]


# ------------------------------------------------------------------ Logs

def _short(v, n):
    v = '' if v is None else str(v)
    return v if len(v) <= n else v[:n] + ' …'


def cmd_nclog(path, n):
    lines = tail_lines(path, int(n))
    if lines is None:
        return out({'ok': False, 'error': f'{path} nicht lesbar', 'entries': [], 'path': path})
    entries = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            entries.append({'level': -1, 'message': _short(line, 2000)})
            continue
        exc = e.get('exception')
        exc_txt = ''
        if isinstance(exc, dict):
            exc_txt = f"{exc.get('Exception', '')}: {exc.get('Message', '')}"
            if exc.get('File'):
                exc_txt += f" ({exc.get('File')}:{exc.get('Line', '')})"
        elif exc:
            exc_txt = str(exc)
        entries.append({
            'time': e.get('time', ''), 'level': e.get('level', -1), 'app': e.get('app', ''),
            'user': e.get('user', ''), 'method': e.get('method', ''), 'url': _short(e.get('url'), 300),
            'message': _short(e.get('message'), 3000), 'exception': _short(exc_txt, 1500),
            'version': e.get('version', ''), 'reqId': e.get('reqId', ''),
        })
    def size(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return None
    out({'ok': True, 'path': path, 'entries': entries, 'size': size(path), 'rotated_size': size(path + '.1')})


def cmd_textlog(path, n):
    lines = tail_lines(path, int(n), 2 * 1024 * 1024)
    out({'ok': lines is not None, 'path': path, 'lines': [_short(x, 2000) for x in (lines or [])]})


# ------------------------------------------------------------------ PHP-FPM

def parse_pool_file(path, pools):
    section = None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for raw in f:
                line = raw.strip()
                if not line or line[0] in ';#':
                    continue
                m = re.match(r'^\[([^\]]+)\]$', line)
                if m:
                    section = m.group(1).strip()
                    if section != 'global':
                        pools.setdefault(section, {'_files': []})
                        if path not in pools[section]['_files']:
                            pools[section]['_files'].append(path)
                    continue
                if section and section != 'global' and '=' in line:
                    k, v = line.split('=', 1)
                    v = v.split(';')[0].strip().strip('"').strip("'")
                    pools[section][k.strip()] = v
    except OSError:
        pass


def fpm_pool_files(ver):
    conf = f'/etc/php/{ver}/fpm/php-fpm.conf'
    patterns = []
    try:
        with open(conf, encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'^\s*include\s*=\s*(\S+)', line)
                if m:
                    p = m.group(1).replace('${prefix}', '/usr').replace('${pid}', '')
                    patterns.append(p if p.startswith('/') else os.path.join(os.path.dirname(conf), p))
    except OSError:
        pass
    patterns = patterns or [f'/etc/php/{ver}/fpm/pool.d/*.conf']
    files = [conf]
    for p in patterns:
        files += sorted(glob.glob(p))
    return files


def fpm_pools(ver):
    pools = {}
    for f in fpm_pool_files(ver):
        parse_pool_file(f, pools)
    return pools


def fpm_log_path(ver):
    try:
        with open(f'/etc/php/{ver}/fpm/php-fpm.conf', encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'^\s*error_log\s*=\s*(\S+)', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return f'/var/log/php{ver}-fpm.log'


def _proc_mem_kb(pid):
    """PSS (anteiliger Speicher, OPcache-SHM nicht mehrfach gezählt), sonst RSS."""
    for path, key in ((f'/proc/{pid}/smaps_rollup', 'Pss:'), (f'/proc/{pid}/status', 'VmRSS:')):
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith(key):
                        return int(line.split()[1])
        except (OSError, ValueError):
            continue
    return 0


def fpm_workers(ver):
    workers = {}
    for pid in filter(str.isdigit, os.listdir('/proc')):
        try:
            with open(f'/proc/{pid}/comm') as f:
                comm = f.read().strip()
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmd = f.read().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
        except OSError:
            continue
        if comm != f'php-fpm{ver}':
            continue
        m = re.search(r'pool (\S+)', cmd)
        if m:
            workers.setdefault(m.group(1), []).append(_proc_mem_kb(pid))
    return workers


def meminfo():
    info = {}
    with open('/proc/meminfo') as f:
        for line in f:
            k, v = line.split(':', 1)
            info[k] = int(v.split()[0])
    return info


def cmd_fpm_info(*versions):
    mi = meminfo()
    res = {'mem_total_kb': mi.get('MemTotal', 0), 'mem_available_kb': mi.get('MemAvailable', 0), 'versions': {}}
    for ver in versions:
        pools = fpm_pools(ver)
        workers = fpm_workers(ver)
        log = fpm_log_path(ver)
        hits, last = 0, ''
        for line in tail_lines(log, 20000) or []:
            if 'max_children' in line and 'reached' in line:
                hits += 1
                last = line[:200]
        pv = {}
        for name, cfg in pools.items():
            mem = workers.get(name, [])
            pv[name] = {
                'config': {k: v for k, v in cfg.items() if k.startswith('pm') or k in ('user', 'listen')},
                'files': cfg['_files'],
                'workers': len(mem), 'worker_kb_total': sum(mem),
                'worker_kb_avg': int(sum(mem) / len(mem)) if mem else 0, 'worker_kb_max': max(mem) if mem else 0,
            }
        res['versions'][ver] = {'pools': pv, 'log': log, 'max_children_hits': hits, 'max_children_last': last,
                                'override': f'/etc/php/{ver}/fpm/pool.d/{OVERRIDE_NAME}'}
    out(res)


def _check_pm(cfg):
    def num(k):
        try:
            return int(cfg.get(k, ''))
        except ValueError:
            return None
    mc = num('pm.max_children')
    if not mc or mc < 1:
        return 'pm.max_children muss mindestens 1 sein'
    if cfg.get('pm', 'dynamic') == 'dynamic':
        lo, st, hi = num('pm.min_spare_servers'), num('pm.start_servers'), num('pm.max_spare_servers')
        if None in (lo, st, hi):
            return 'Für pm = dynamic müssen start/min_spare/max_spare gesetzt sein'
        if not (1 <= lo <= st <= hi <= mc):
            return ('Ungültige Kombination: es muss gelten 1 ≤ min_spare ≤ start ≤ max_spare ≤ max_children '
                    f'(aktuell {lo} ≤ {st} ≤ {hi} ≤ {mc})')
    return None


def cmd_fpm_write(ver, pool, *pairs):
    if not re.fullmatch(r'[0-9]\.[0-9]{1,2}', ver) or not os.path.isdir(f'/etc/php/{ver}/fpm/pool.d'):
        sys.exit('Ungültige PHP-Version')
    pools = fpm_pools(ver)
    if pool not in pools:
        sys.exit(f'Pool {pool} existiert nicht')
    changes = {}
    for p in pairs:
        k, _, v = p.partition('=')
        if k not in POOL_KEYS or not re.fullmatch(POOL_KEYS[k], v):
            sys.exit(f'Nicht erlaubt: {p}')
        changes[k] = v
    if not changes:
        sys.exit('Keine Änderungen')
    effective = {k: v for k, v in pools[pool].items() if not k.startswith('_')}
    effective.update(changes)
    err = _check_pm(effective)
    if err:
        sys.exit('FEHLER: ' + err)

    path = f'/etc/php/{ver}/fpm/pool.d/{OVERRIDE_NAME}'
    own = {}
    parse_pool_file(path, own)
    for sec in own.values():
        sec.pop('_files', None)
    own.setdefault(pool, {}).update(changes)
    text = ['; Verwaltet vom Nextcloud Server Manager – überschreibt Werte aus den übrigen Pool-Dateien.\n']
    for name, cfg in own.items():
        text.append(f'\n[{name}]\n')
        text += [f'{k} = {v}\n' for k, v in cfg.items()]
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.writelines(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    for k, v in changes.items():
        print(f'Gesetzt: [{pool}] {k} = {v}')
    # Prüfen, ob eine später geladene Datei die Werte wieder überschreibt.
    now = fpm_pools(ver)[pool]
    for k, v in changes.items():
        if now.get(k) != v:
            print(f'WARNUNG: {k} wird von einer anderen Datei auf {now.get(k)} gesetzt.')


# ------------------------------------------------------------------ Backup

def _split_host(host, port):
    host = host or 'localhost'
    socket = ''
    m = re.match(r'^\[([^\]]+)\](?::(\d+))?$', host)          # [::1]:3306
    if m:
        return m.group(1), m.group(2) or port, ''
    if ':' in host:
        h, rest = host.split(':', 1)
        if rest.startswith('/'):
            return h or 'localhost', port, rest
        if rest.isdigit():
            return h, rest, ''
    return host, port, socket


def _opt(v):
    return '"' + str(v).replace('\\', '\\\\').replace('"', '\\"') + '"'


_CTRL = re.compile(r'[\x00-\x1f\x7f]')


def _db_conf(c):
    """Prüft die Datenbank-Angaben aus config.php, bevor sie (als root) verwendet werden.

    config.php ist für den Webserver-Benutzer beschreibbar. Ohne Prüfung könnten z. B. Zeilenumbrüche
    zusätzliche Optionen in die MySQL-Optionsdatei schreiben (etwa result-file=…), ein Datenbankname
    mit führendem '-' als Programm-Option gelten oder ein SQLite-Name mit '../' aus dem
    Datenverzeichnis herausführen. Wirft ValueError mit verständlicher Meldung."""
    if not isinstance(c, dict):
        raise ValueError('Datenbank-Konfiguration nicht lesbar')
    for k in ('dbtype', 'dbhost', 'dbport', 'dbname', 'dbuser', 'dbpassword', 'datadirectory'):
        v = c.get(k)
        if v is None:
            continue
        if not isinstance(v, (str, int)) or isinstance(v, bool):
            raise ValueError(f'{k} in config.php hat einen ungültigen Typ')
        if _CTRL.search(str(v)):
            raise ValueError(f'{k} in config.php enthält Steuerzeichen (z. B. Zeilenumbruch) – abgelehnt')
    for k in ('dbhost', 'dbname', 'dbuser'):
        if str(c.get(k) or '').startswith('-'):
            raise ValueError(f'{k} in config.php darf nicht mit "-" beginnen')
    port = str(c.get('dbport') or '')
    if port and not port.isdigit():
        raise ValueError('dbport in config.php ist keine Zahl')
    host, hport, sock = _split_host(c.get('dbhost'), port)
    if hport and not str(hport).isdigit():
        raise ValueError('Port in dbhost ist keine Zahl')
    if len(str(c.get('dbname') or '')) > 64:
        raise ValueError('dbname in config.php ist zu lang')
    if (c.get('dbtype') or 'sqlite3') == 'sqlite3':
        name = str(c.get('dbname') or 'owncloud')
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', name):
            raise ValueError('dbname für SQLite darf nur Buchstaben, Ziffern, . _ - enthalten')
        if not str(c.get('datadirectory') or '').startswith('/'):
            raise ValueError('Datenverzeichnis für SQLite fehlt oder ist kein absoluter Pfad')
    return c


def _read_db_conf():
    """Zugangsdaten von stdin lesen und prüfen; bei Fehlern mit Meldung beenden."""
    try:
        return _db_conf(json.load(sys.stdin))
    except ValueError as e:
        sys.exit(f'FEHLER: {e}')


def _sqlite_file(c):
    return os.path.join(str(c.get('datadirectory')), str(c.get('dbname') or 'owncloud') + '.db')


def cmd_db_dump(target):
    """DB-Dump nach target (gzip). Zugangsdaten als JSON über stdin."""
    c = _read_db_conf()
    dbtype = c.get('dbtype') or 'sqlite3'
    tmp = target + '.part'
    try:
        if dbtype == 'mysql':
            tool = shutil.which('mariadb-dump') or shutil.which('mysqldump')
            if not tool:
                sys.exit('FEHLER: mysqldump/mariadb-dump nicht gefunden (Paket mariadb-client)')
            cnf = _mysql_cnf(c)
            try:
                cmd = [tool, f'--defaults-extra-file={cnf}', '--single-transaction', '--quick',
                       '--default-character-set=utf8mb4', '--no-tablespaces', '--routines', '--triggers',
                       str(c.get('dbname') or 'nextcloud')]
                _dump_to_gz(cmd, tmp, os.environ)
            finally:
                os.unlink(cnf)
        elif dbtype == 'pgsql':
            tool = shutil.which('pg_dump')
            if not tool:
                sys.exit('FEHLER: pg_dump nicht gefunden (Paket postgresql-client)')
            host, port, sock = _split_host(c.get('dbhost'), str(c.get('dbport') or ''))
            env = dict(os.environ, PGPASSWORD=str(c.get('dbpassword', '')))
            cmd = [tool, '-h', sock or host, '-U', str(c.get('dbuser', '')), '--no-owner', '-d', str(c.get('dbname') or 'nextcloud')]
            if port:
                cmd[1:1] = ['-p', port]
            _dump_to_gz(cmd, tmp, env)
        elif dbtype == 'sqlite3':
            src = _sqlite_file(c)
            raw = tmp + '.db'
            s, d = sqlite3.connect(f'file:{src}?mode=ro', uri=True), sqlite3.connect(raw)
            with d:
                s.backup(d)
            s.close(), d.close()
            with open(raw, 'rb') as fi, gzip.open(tmp, 'wb', 6) as fo:
                shutil.copyfileobj(fi, fo)
            os.unlink(raw)
            target = target.replace('.sql.gz', '.sqlite.gz')
        else:
            sys.exit(f'FEHLER: Datenbanktyp {dbtype} wird nicht unterstützt')
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        for p in (tmp, tmp + '.db'):
            if os.path.exists(p):
                os.unlink(p)
        raise
    print(f'Datenbank ({dbtype}) gesichert: {target} ({os.path.getsize(target) / 1048576:.1f} MiB)')


def _dump_to_gz(cmd, target, env):
    with gzip.open(target, 'wb', 6) as fo:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        shutil.copyfileobj(p.stdout, fo, 1024 * 1024)
        err = p.stderr.read().decode('utf-8', 'replace')
        rc = p.wait()
    if rc != 0:
        raise SystemExit(f'FEHLER: {os.path.basename(cmd[0])} beendet mit Code {rc}: {err.strip()[:2000]}')
    if err.strip():
        print(err.strip()[:2000])


def cmd_backup_list(root):
    items = []
    legacy = 0
    if os.path.isdir(root):
        for name in sorted(os.listdir(root), reverse=True):
            p = os.path.join(root, name)
            if os.path.isdir(p) and BACKUP_RE.match(name):
                files = []
                for fn in sorted(os.listdir(p)):
                    fp = os.path.join(p, fn)
                    if os.path.isfile(fp):
                        files.append({'name': fn, 'size': os.path.getsize(fp)})
                info = {}
                try:
                    with open(os.path.join(p, 'info.json')) as f:
                        info = json.load(f)
                except (OSError, ValueError):
                    pass
                ver = {}
                try:
                    with open(os.path.join(p, 'verify.json')) as f:
                        v = json.load(f)
                    ver = {'ok': v.get('ok'), 'at': v.get('verified_at'), 'errors': v.get('errors', [])[:5]}
                except (OSError, ValueError):
                    pass
                items.append({'name': name, 'files': files, 'size': sum(f['size'] for f in files),
                              'info': info, 'complete': bool(info.get('complete')),
                              'user_data': bool(info.get('user_data_included')),
                              'checksums': os.path.isfile(os.path.join(p, 'checksums.sha256')), 'verify': ver})
            elif name.startswith('config.php.'):
                legacy += 1
    st = shutil.disk_usage(root) if os.path.isdir(root) else None
    out({'root': root, 'backups': items, 'legacy_config_copies': legacy,
         'free': st.free if st else 0, 'total': st.total if st else 0})


def cmd_backup_prune(root, keep, protect=''):
    """Behält die neuesten `keep` Backups; `protect` (z.B. das gerade wiederhergestellte) bleibt immer."""
    keep = int(keep)
    dirs = sorted((d for d in os.listdir(root) if BACKUP_RE.match(d) and os.path.isdir(os.path.join(root, d))),
                  reverse=True)
    for d in dirs[keep:]:
        if d == protect:
            continue
        shutil.rmtree(os.path.join(root, d))
        print(f'Altes Backup entfernt: {d}')


def cmd_nc_overrides(ncpath):
    """PHP-Werte aus Nextclouds .user.ini (PHP-FPM/CGI) und .htaccess (php_value/php_flag, mod_php)."""
    res = {'user_ini_path': '', 'user_ini': {}, 'htaccess_path': '', 'htaccess': {}}
    ui = os.path.join(ncpath, '.user.ini')
    if os.path.isfile(ui):
        res['user_ini_path'] = ui
        with open(ui, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in ';#[' or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                res['user_ini'][k.strip()] = v.split(';')[0].strip().strip('"').strip("'")
    ht = os.path.join(ncpath, '.htaccess')
    if os.path.isfile(ht):
        res['htaccess_path'] = ht
        with open(ht, encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'^\s*php_(?:value|flag)\s+(\S+)\s+(.+?)\s*$', line)
                if m:
                    res['htaccess'][m.group(1)] = m.group(2).strip('"').strip("'")
    out(res)


def cmd_fpm_log(ver):
    print(fpm_log_path(ver))

# ================================================================== Backup-Prüfung

def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _gzip_tail(path, keep=4096):
    """Liest die gzip-Datei komplett (prüft dabei die CRC) und liefert Anfang und Ende des Inhalts."""
    head, tail, size = b'', b'', 0
    with gzip.open(path, 'rb') as g:
        for chunk in iter(lambda: g.read(1024 * 1024), b''):
            if not head:
                head = chunk[:keep]
            tail = (tail + chunk)[-keep:]
            size += len(chunk)
    return head, tail, size


def _tar_scan(path):
    """Liest ein tar.gz vollständig. Rückgabe (Einträge, Bytes, Namen der obersten Ebene, Namensmenge-Stichprobe)."""
    count, total, tops, names = 0, 0, set(), set()
    with tarfile.open(path, 'r|gz') as t:
        for m in t:
            count += 1
            total += m.size
            tops.add(m.name.split('/', 1)[0])
            if count <= 200000:
                names.add(m.name)
    return count, total, tops, names


def verify_backup(root, name):
    d = os.path.join(root, name)
    res = {'ok': False, 'errors': [], 'warnings': [], 'checks': {}, 'verified_at': int(time.time())}
    err, warn, chk = res['errors'], res['warnings'], res['checks']

    def step(text):
        print(text, flush=True)

    if not BACKUP_RE.match(name) or not os.path.isdir(d):
        err.append('Backup nicht gefunden')
        return res
    info = {}
    try:
        with open(os.path.join(d, 'info.json')) as f:
            info = json.load(f)
        chk['info.json'] = True
    except (OSError, ValueError) as e:
        err.append(f'info.json fehlt oder ist ungültig: {e}')
        chk['info.json'] = False
    res['info'] = info
    res['user_data_included'] = bool(info.get('user_data_included'))

    # 1. Prüfsummen aus der Entstehung des Backups
    sums = os.path.join(d, 'checksums.sha256')
    if os.path.isfile(sums):
        step('Prüfe SHA-256-Prüfsummen ...')
        bad = []
        with open(sums) as f:
            for line in f:
                line = line.rstrip('\n')
                if not line:
                    continue
                digest, _, fn = line.partition('  ')
                fn = fn.lstrip('*')
                fp = os.path.join(d, fn)
                if '/' in fn or not os.path.isfile(fp):
                    bad.append(f'{fn} fehlt')
                elif _sha256(fp) != digest:
                    bad.append(f'{fn} wurde verändert oder ist beschädigt')
        chk['Prüfsummen'] = not bad
        err.extend(bad)
    else:
        chk['Prüfsummen'] = None
        warn.append('Keine Prüfsummen vorhanden (Backup vor Version 0.6.1) – nur Strukturprüfung möglich.')

    # 2. config.php
    cfg = os.path.join(d, 'config.php')
    try:
        with open(cfg, 'rb') as f:
            ok = b'$CONFIG' in f.read()
    except OSError:
        ok = False
    chk['config.php'] = ok
    if not ok:
        err.append('config.php fehlt oder enthält keine Nextcloud-Konfiguration')

    # 3. Datenbank
    sql, lite = os.path.join(d, 'database.sql.gz'), os.path.join(d, 'database.sqlite.gz')
    if os.path.isfile(sql):
        step('Prüfe Datenbank-Dump (vollständig entpacken) ...')
        try:
            head, tail, size = _gzip_tail(sql)
            h = head.decode('utf-8', 'replace')
            t = tail.decode('utf-8', 'replace')
            if 'PostgreSQL database dump' in h:
                done = 'PostgreSQL database dump complete' in t
                res['dbtype'] = 'pgsql'
            else:
                done = '-- Dump completed' in t
                res['dbtype'] = 'mysql'
            chk['Datenbank'] = done
            if not done:
                err.append('Datenbank-Dump ist unvollständig (Abschlusszeile fehlt) – vermutlich abgebrochen oder abgeschnitten')
            res['db_bytes'] = size
        except (OSError, EOFError, zlib.error) as e:
            chk['Datenbank'] = False
            err.append(f'Datenbank-Dump beschädigt: {e}')
    elif os.path.isfile(lite):
        step('Prüfe SQLite-Datenbank (integrity_check) ...')
        res['dbtype'] = 'sqlite3'
        fd, tmp = tempfile.mkstemp(prefix='ncm-verify-', dir=d)
        os.close(fd)
        try:
            with gzip.open(lite, 'rb') as fi, open(tmp, 'wb') as fo:
                shutil.copyfileobj(fi, fo, 1024 * 1024)
            con = sqlite3.connect(f'file:{tmp}?mode=ro', uri=True)
            r = con.execute('PRAGMA integrity_check').fetchone()[0]
            con.close()
            chk['Datenbank'] = r == 'ok'
            if r != 'ok':
                err.append(f'SQLite-Datenbank fehlerhaft: {r}')
        except (OSError, EOFError, zlib.error, sqlite3.Error) as e:
            chk['Datenbank'] = False
            err.append(f'SQLite-Backup beschädigt: {e}')
        finally:
            os.unlink(tmp)
    else:
        chk['Datenbank'] = False
        err.append('Datenbank-Backup fehlt')

    # 4. Programmcode
    code = os.path.join(d, 'nextcloud-code.tar.gz')
    base = os.path.basename(str(info.get('nc_path') or '').rstrip('/'))
    if os.path.isfile(code):
        step('Prüfe Programmcode-Archiv (vollständig lesen) ...')
        try:
            count, total, tops, names = _tar_scan(code)
            ok = bool(count) and (not base or (tops == {base} and f'{base}/occ' in names
                                                and f'{base}/version.php' in names))
            chk['Programmcode'] = ok
            res['code_bytes'] = total
            if not ok:
                err.append(f'Programmcode-Archiv enthält keine vollständige Nextcloud unter „{base}/“')
        except (OSError, EOFError, tarfile.TarError, zlib.error) as e:
            chk['Programmcode'] = False
            err.append(f'Programmcode-Archiv beschädigt: {e}')
    else:
        chk['Programmcode'] = False
        err.append('nextcloud-code.tar.gz fehlt')

    # 5. Benutzerdaten
    data = os.path.join(d, 'nextcloud-data.tar.gz')
    if res['user_data_included']:
        if os.path.isfile(data):
            step('Prüfe Benutzerdaten-Archiv (vollständig lesen, kann dauern) ...')
            dbase = os.path.basename(str(info.get('datadir') or '').rstrip('/'))
            try:
                count, total, tops, _ = _tar_scan(data)
                ok = bool(count) and (not dbase or tops == {dbase})
                chk['Benutzerdaten'] = ok
                res['data_bytes'] = total
                res['data_entries'] = count
                if not ok:
                    err.append('Benutzerdaten-Archiv passt nicht zum Datenverzeichnis')
            except (OSError, EOFError, tarfile.TarError, zlib.error) as e:
                chk['Benutzerdaten'] = False
                err.append(f'Benutzerdaten-Archiv beschädigt: {e}')
        else:
            chk['Benutzerdaten'] = False
            err.append('info.json meldet Benutzerdaten, aber nextcloud-data.tar.gz fehlt')
    else:
        warn.append('Benutzerdaten (Datenverzeichnis) sind NICHT in diesem Backup enthalten.')

    res['ok'] = not err
    return res


def cmd_backup_verify(root, name):
    res = verify_backup(root, name)
    if os.path.isdir(os.path.join(root, name)) and BACKUP_RE.match(name):
        p = os.path.join(root, name, 'verify.json')
        with open(p + '.tmp', 'w') as f:
            json.dump(res, f)
        os.chmod(p + '.tmp', 0o600)
        os.replace(p + '.tmp', p)
    for k, v in res['checks'].items():
        print(f"  {'✓' if v else ('–' if v is None else '✗')} {k}")
    for w in res['warnings']:
        print(f'  Hinweis: {w}')
    for e in res['errors']:
        print(f'  FEHLER: {e}')
    print('ERGEBNIS: Backup in Ordnung.' if res['ok'] else 'ERGEBNIS: Backup FEHLERHAFT.')
    sys.exit(0 if res['ok'] else 1)


def cmd_backup_info(root, name, key):
    """Einzelnen Wert aus info.json / verify.json (für den Wrapper)."""
    d = os.path.join(root, name)
    for fn in ('verify.json', 'info.json'):
        try:
            with open(os.path.join(d, fn)) as f:
                j = json.load(f)
        except (OSError, ValueError):
            continue
        for src in (j, j.get('info') or {}):
            if key in src:
                v = src[key]
                print(str(v).lower() if isinstance(v, bool) else v)
                return
    print('')


# ================================================================== Datenbank-Restore

def _mysql_cnf(c):
    host, port, sock = _split_host(c.get('dbhost'), str(c.get('dbport') or ''))
    fd, cnf = tempfile.mkstemp(prefix='ncm-', suffix='.cnf')
    with os.fdopen(fd, 'w') as f:
        f.write('[client]\n')
        f.write(f"user={_opt(c.get('dbuser', ''))}\npassword={_opt(c.get('dbpassword', ''))}\n")
        if sock:
            f.write(f'socket={_opt(sock)}\n')
        else:
            f.write(f'host={_opt(host)}\n')
            if port:
                f.write(f'port={port}\n')
    return cnf


def _mysql_tool():
    return shutil.which('mariadb') or shutil.which('mysql')


def _pg_cmd(c, tool='psql'):
    host, port, sock = _split_host(c.get('dbhost'), str(c.get('dbport') or ''))
    cmd = [shutil.which(tool) or tool, '-h', sock or host, '-U', str(c.get('dbuser', '')),
           '-d', str(c.get('dbname') or 'nextcloud')]
    if port:
        cmd[1:1] = ['-p', port]
    return cmd, dict(os.environ, PGPASSWORD=str(c.get('dbpassword', '')))


def _feed(cmd, prefix, source, env=None):
    """Startet cmd, schreibt prefix und danach den ENTPACKTEN Inhalt von source auf stdin."""
    with tempfile.TemporaryFile() as errf:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errf, env=env)
        try:
            p.stdin.write(prefix.encode())
            with gzip.open(source, 'rb') as g:
                shutil.copyfileobj(g, p.stdin, 1024 * 1024)
        except BrokenPipeError:
            pass
        finally:
            try:
                p.stdin.close()
            except BrokenPipeError:
                pass
        rc = p.wait()
        errf.seek(0)
        return rc, errf.read().decode('utf-8', 'replace')


def _first_error(text):
    """Die aussagekräftige Fehlerzeile zuerst (MariaDB beginnt mit einer Trennlinie und der Abfrage)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    errs = [ln for ln in lines if ln.startswith(('ERROR', 'psql:', 'FATAL'))]
    return '\n'.join(errs[:3] + ['(Details:)'] + lines[:15]) if errs else '\n'.join(lines[:20])


def _q_mysql(name):
    return '`' + name.replace('`', '``') + '`'


def cmd_db_restore(source, webuser):
    """Spielt einen Dump in die Nextcloud-Datenbank ein. Zugangsdaten als JSON über stdin.

    MySQL/MariaDB: vorher werden ALLE Tabellen gelöscht, damit keine nach dem Backup entstandenen
    Tabellen übrig bleiben (sonst scheitert das nächste Upgrade). PostgreSQL: alles in EINER
    Transaktion mit ON_ERROR_STOP – bei einem Fehler bleibt die Datenbank unverändert.
    """
    c = _read_db_conf()
    typ = c.get('dbtype') or 'sqlite3'
    if not os.path.isfile(source):
        sys.exit(f'FEHLER: {source} fehlt')
    if typ == 'mysql':
        if not source.endswith('.sql.gz'):
            sys.exit('FEHLER: Für MySQL/MariaDB wird database.sql.gz erwartet')
        tool = _mysql_tool()
        if not tool:
            sys.exit('FEHLER: mariadb/mysql-Client fehlt (Paket mariadb-client)')
        cnf = _mysql_cnf(c)
        try:
            db = str(c.get('dbname') or 'nextcloud')
            r = subprocess.run([tool, f'--defaults-extra-file={cnf}', '-N', '-B', db, '-e',
                                "SHOW FULL TABLES WHERE Table_type IN ('BASE TABLE','VIEW')"],
                               capture_output=True, text=True)
            if r.returncode:
                sys.exit(f'FEHLER: Tabellenliste nicht lesbar: {r.stderr.strip()[:1000]}')
            rows = [ln.split('\t') for ln in r.stdout.splitlines() if ln.strip()]
            views = [n for n, t in rows if t == 'VIEW']
            tables = [n for n, t in rows if t != 'VIEW']
            prefix = 'SET FOREIGN_KEY_CHECKS=0;\n'
            if views:
                prefix += 'DROP VIEW IF EXISTS ' + ', '.join(map(_q_mysql, views)) + ';\n'
            if tables:
                prefix += 'DROP TABLE IF EXISTS ' + ', '.join(map(_q_mysql, tables)) + ';\n'
            prefix += 'SET FOREIGN_KEY_CHECKS=1;\n'
            print(f'Lösche {len(tables)} Tabellen und spiele den Dump ein ...', flush=True)
            rc, err_ = _feed([tool, f'--defaults-extra-file={cnf}', db], prefix, source)
            if rc:
                sys.exit(f'FEHLER: Import fehlgeschlagen: {_first_error(err_)[:3000]}')
        finally:
            os.unlink(cnf)
    elif typ == 'pgsql':
        if not shutil.which('psql'):
            sys.exit('FEHLER: psql fehlt (Paket postgresql-client)')
        cmd, env = _pg_cmd(c)
        r = subprocess.run(cmd + ['-At', '-c', "SELECT quote_ident(schemaname)||'.'||quote_ident(tablename) "
                                               "FROM pg_tables WHERE schemaname = current_schema()"],
                           capture_output=True, text=True, env=env)
        if r.returncode:
            sys.exit(f'FEHLER: Tabellenliste nicht lesbar: {r.stderr.strip()[:1000]}')
        tables = [t for t in r.stdout.split() if t]
        prefix = ''.join(f'DROP TABLE IF EXISTS {t} CASCADE;\n' for t in tables)
        print(f'Lösche {len(tables)} Tabellen und spiele den Dump in einer Transaktion ein ...', flush=True)
        rc, err_ = _feed(cmd + ['-q', '-v', 'ON_ERROR_STOP=1', '--single-transaction', '-f', '-'], prefix, source, env)
        if rc:
            sys.exit(f'FEHLER: Import fehlgeschlagen, Datenbank unverändert: {_first_error(err_)[:3000]}')
    elif typ == 'sqlite3':
        target = _sqlite_file(c)
        tmp = target + '.ncm-restore'
        with gzip.open(source, 'rb') as fi, open(tmp, 'wb') as fo:
            shutil.copyfileobj(fi, fo, 1024 * 1024)
        con = sqlite3.connect(tmp)
        check = con.execute('PRAGMA integrity_check').fetchone()[0]
        con.close()
        if check != 'ok':
            os.unlink(tmp)
            sys.exit(f'FEHLER: SQLite-Backup fehlerhaft: {check}')
        # Besitzer und Rechte wie bisher, sonst Webserver-Benutzer – sonst ist die DB für Nextcloud schreibgeschützt.
        if os.path.exists(target):
            st = os.stat(target)
            uid, gid, mode = st.st_uid, st.st_gid, st.st_mode & 0o777
        else:
            import pwd
            pw = pwd.getpwnam(webuser)
            uid, gid, mode = pw.pw_uid, pw.pw_gid, 0o640
        os.chown(tmp, uid, gid)
        os.chmod(tmp, mode)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        for suffix in ('', '-wal', '-shm', '-journal'):
            if os.path.exists(target + suffix):
                os.replace(target + suffix, f'{target}.ncm-before-restore-{stamp}{suffix}')
        os.replace(tmp, target)
        print(f'Vorherige SQLite-Datei: {target}.ncm-before-restore-{stamp}')
    else:
        sys.exit(f'FEHLER: Datenbanktyp {typ} wird nicht unterstützt')
    print('Datenbank wiederhergestellt.')


# ================================================================== Diagnose

def cmd_db_info():
    c = json.load(sys.stdin)
    typ = c.get('dbtype') or 'sqlite3'
    res = {'type': typ, 'host': c.get('dbhost'), 'name': c.get('dbname'), 'ok': False}
    try:
        _db_conf(c)
        if typ == 'mysql':
            tool = _mysql_tool()
            if not tool:
                raise RuntimeError('mariadb/mysql-Client nicht installiert')
            cnf = _mysql_cnf(c)
            try:
                db = str(c.get('dbname') or 'nextcloud')
                r = subprocess.run([tool, f'--defaults-extra-file={cnf}', '-N', '-B', db, '-e',
                                    'SELECT VERSION(), (SELECT COALESCE(SUM(data_length+index_length),0) FROM '
                                    'information_schema.tables WHERE table_schema=DATABASE()), (SELECT COUNT(*) FROM '
                                    'information_schema.tables WHERE table_schema=DATABASE()), '
                                    '@@max_connections, @@innodb_buffer_pool_size'],
                                   capture_output=True, text=True, timeout=30)
            finally:
                os.unlink(cnf)
            if r.returncode:
                raise RuntimeError(r.stderr.strip()[:500])
            v, size, tables, maxc, pool = r.stdout.strip().split('\t')
            res.update(ok=True, version=v, size_bytes=int(size), tables=int(tables),
                       max_connections=int(maxc), buffer_pool_bytes=int(pool))
        elif typ == 'pgsql':
            cmd, env = _pg_cmd(c)
            r = subprocess.run(cmd + ['-At', '-F', '\t', '-c',
                                      "SELECT version(), pg_database_size(current_database()), (SELECT count(*) FROM "
                                      "pg_tables WHERE schemaname=current_schema()), current_setting('max_connections')"],
                               capture_output=True, text=True, env=env, timeout=30)
            if r.returncode:
                raise RuntimeError(r.stderr.strip()[:500])
            v, size, tables, maxc = r.stdout.strip().split('\t')
            res.update(ok=True, version=v.split(',')[0], size_bytes=int(size), tables=int(tables),
                       max_connections=int(maxc))
        else:
            fp = _sqlite_file(c)
            con = sqlite3.connect(f'file:{fp}?mode=ro', uri=True)
            tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            con.close()
            res.update(ok=True, version='SQLite ' + sqlite3.sqlite_version, size_bytes=os.path.getsize(fp),
                       tables=tables, note='SQLite ist laut Nextcloud nur für Tests und sehr kleine Instanzen geeignet.')
    except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as e:
        res['error'] = str(e)
    out(res)


def cmd_redis_info():
    """Redis-Erreichbarkeit mit den Zugangsdaten aus config.php (stdin). Das Passwort wird nie ausgegeben."""
    c = json.load(sys.stdin)
    r = c.get('redis') or {}
    res = {'configured': bool(r), 'host': r.get('host'), 'port': r.get('port'), 'dbindex': r.get('dbindex'),
           'password_set': bool(r.get('password')), 'ping': None}
    if not r:
        out(res)
        return
    cli = shutil.which('redis-cli')
    if not cli:
        res['error'] = 'redis-cli nicht installiert (Paket redis-tools) – Verbindung nicht prüfbar'
        out(res)
        return
    host = str(r.get('host') or '127.0.0.1')
    cmd = [cli] + (['-s', host] if host.startswith('/') else ['-h', host, '-p', str(r.get('port') or 6379)])
    if r.get('dbindex') not in (None, ''):
        cmd += ['-n', str(r.get('dbindex'))]
    env = dict(os.environ)
    if r.get('password'):
        env['REDISCLI_AUTH'] = str(r['password'])
    if r.get('user'):
        cmd += ['--user', str(r['user'])]
    try:
        p = subprocess.run(cmd + ['ping'], capture_output=True, text=True, env=env, timeout=10)
        res['ping'] = p.stdout.strip() or p.stderr.strip()[:200]
        if res['ping'] == 'PONG':
            i = subprocess.run(cmd + ['info'], capture_output=True, text=True, env=env, timeout=10).stdout
            kv = dict(ln.split(':', 1) for ln in i.splitlines() if ':' in ln and not ln.startswith('#'))
            res.update(version=kv.get('redis_version', '').strip(), used_memory=kv.get('used_memory_human', '').strip(),
                       maxmemory=kv.get('maxmemory_human', '').strip(), uptime_days=kv.get('uptime_in_days', '').strip())
    except subprocess.TimeoutExpired:
        res['ping'] = 'Zeitüberschreitung'
    out(res)


def cmd_ini_sources(keys, *files):
    """Fundstellen der Schlüssel in den tatsächlich geladenen INI-Dateien (Reihenfolge = Ladereihenfolge)."""
    wanted = keys.split(',')
    found = {k: [] for k in wanted}
    rx = re.compile(r'^\s*([A-Za-z0-9_.]+)\s*=\s*(.*?)\s*$')
    for spec in files:
        sapi, _, fp = spec.partition(':')
        try:
            with open(fp, encoding='utf-8', errors='replace') as f:
                section = ''
                for line in f:
                    s = line.strip()
                    if not s or s[0] in ';#':
                        continue
                    if s.startswith('['):
                        section = s
                        continue
                    if section.upper().startswith(('[PATH=', '[HOST=')):
                        continue
                    m = rx.match(s)
                    if m and m.group(1) in found:
                        val = re.sub(r'\s*;.*$', '', m.group(2)).strip().strip('"').strip("'")
                        found[m.group(1)].append({'sapi': sapi, 'file': fp, 'value': val})
        except OSError:
            continue
    out(found)


COMMANDS = {
    'fpm_log': cmd_fpm_log, 'backup_verify': cmd_backup_verify, 'backup_info': cmd_backup_info, 'db_restore': cmd_db_restore, 'db_info': cmd_db_info, 'redis_info': cmd_redis_info, 'ini_sources': cmd_ini_sources, 'nc_overrides': cmd_nc_overrides, 'nclog': cmd_nclog, 'textlog': cmd_textlog, 'fpm_info': cmd_fpm_info, 'fpm_write': cmd_fpm_write,
    'db_dump': cmd_db_dump, 'backup_list': cmd_backup_list, 'backup_prune': cmd_backup_prune,
}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit('Unbekannter Befehl')
    COMMANDS[sys.argv[1]](*sys.argv[2:])
