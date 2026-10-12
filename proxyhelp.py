# Nextcloud Server Manager
# Copyright (c) 2026 roswitina@hotmail.com
# SPDX-License-Identifier: MIT
# Lizenz: siehe LICENSE · Gewährleistungs- und Haftungsausschluss: siehe HAFTUNGSAUSSCHLUSS.md
"""Reverse-Proxy-Assistent: aus wenigen Angaben die nötigen config.php-Werte und die Proxy-Konfiguration ableiten.

Nur Berechnung und Text – hier wird nichts geändert. Grundlage sind die Proxy-Parameter aus config.sample.php
(trusted_proxies, overwritehost, overwriteprotocol, overwritewebroot, overwritecondaddr, overwrite.cli.url) und ein
in der Praxis bewährtes Muster für DietPi (Nextcloud unter /nextcloud hinter Nginx Proxy Manager)."""
import ipaddress
import re
from urllib.parse import urlparse

PROXIES = {'npm': 'Nginx Proxy Manager', 'nginx': 'nginx', 'apache': 'Apache', 'caddy': 'Caddy'}
HSTS = 'max-age=15552000; includeSubDomains'
URL_RX = re.compile(r'https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~-]+)*/?')


def _path(p):
    p = (p or '/').rstrip('/')
    return p or ''


def parse_inputs(public_url, internal_url, proxy_ip, proxy_kind, root_alias):
    """Prüft die Eingaben. Gibt (werte, fehler) zurück."""
    errors = []
    public_url, internal_url, proxy_ip = (public_url or '').strip(), (internal_url or '').strip(), (proxy_ip or '').strip()
    if not URL_RX.fullmatch(public_url):
        errors.append('Öffentliche Adresse: bitte vollständig angeben, z. B. https://nc.domain.tld')
    if not URL_RX.fullmatch(internal_url):
        errors.append('Interne Adresse: bitte vollständig angeben, z. B. http://192.168.1.55/nextcloud')
    net = None
    try:
        net = ipaddress.ip_network(proxy_ip, strict=False) if proxy_ip else None
    except ValueError:
        pass
    if net is None:
        errors.append('IP des Proxys: bitte eine IP-Adresse angeben, z. B. 192.168.1.198 (oder ein Netz wie 192.168.1.0/24)')
    if proxy_kind not in PROXIES:
        errors.append('Bitte ein Proxy-Programm wählen.')
    if errors:
        return None, errors
    pub, intl = urlparse(public_url), urlparse(internal_url)
    return dict(pub_scheme=pub.scheme, pub_host=pub.hostname, pub_port=pub.port, pub_path=_path(pub.path),
                int_scheme=intl.scheme, int_host=intl.hostname, int_port=intl.port, int_path=_path(intl.path),
                proxy_ip=proxy_ip, proxy_net=net, kind=proxy_kind, root_alias=bool(root_alias)), []


def plan(v, cfg):
    """Aus den geprüften Eingaben: config.php-Zeilen, Hinweise und die Proxy-Konfiguration."""
    warnings, notes = [], []
    path = v['int_path']                                   # öffentlicher Pfad = interner Pfad (bewährtes Muster)
    pub_host = v['pub_host'] + (f":{v['pub_port']}" if v['pub_port'] and v['pub_port'] != 443 else '')
    if v['pub_scheme'] != 'https':
        warnings.append('Die öffentliche Adresse sollte mit https:// beginnen – der Proxy übernimmt die Verschlüsselung.')
    if v['pub_path'] and v['pub_path'] != path:
        warnings.append(f'Öffentlicher Pfad „{v["pub_path"] or "/"}“ und interner Pfad „{path or "/"}“ unterscheiden sich. '
                        'Bewährt ist, den Pfad gleich zu lassen – der Assistent verwendet deshalb öffentlich '
                        f'„{path or "/"}“. Unterschiedliche Pfade machen erfahrungsgemäß Ärger mit WebDAV und den Clients.')
    public_base = f"https://{pub_host}{path}"
    single = v['proxy_net'].num_addresses == 1
    proxy_addr = str(v['proxy_net'].network_address) if single else v['proxy_ip']
    upstream = f"{v['int_scheme']}://{v['int_host']}" + (f":{v['int_port']}" if v['int_port'] else '')

    def as_list(x):
        return list(x.values()) if isinstance(x, dict) else list(x) if isinstance(x, list) else []
    td, tp = as_list(cfg.get('trusted_domains')), as_list(cfg.get('trusted_proxies'))
    settings = []

    def setting(key, value, current, ok, why, cmd_value=None, index=None):
        settings.append(dict(key=key, value=value, current=current, ok=ok, why=why,
                             cmd=f"config:system:set {key}" + (f" {index}" if index is not None else '')
                                 + f" --value={_shq(cmd_value if cmd_value is not None else value)}"))
    setting('trusted_domains', v['pub_host'], ', '.join(map(str, td)) or '–', v['pub_host'] in td,
            'Ohne die Domain in dieser Liste meldet Nextcloud „Zugriff über eine nicht vertrauenswürdige Domain“.',
            index=len(td))
    setting('trusted_proxies', proxy_addr, ', '.join(map(str, tp)) or '–', proxy_addr in tp,
            'Nur Anfragen von dieser Adresse gelten als Proxy; die echte Besucher-IP liest Nextcloud dann aus '
            'X-Forwarded-For (wichtig für Logs und den Schutz vor Anmeldeversuchen).', index=len(tp))
    setting('overwriteprotocol', 'https', cfg.get('overwriteprotocol') or '–', cfg.get('overwriteprotocol') == 'https',
            'Der Proxy spricht mit Nextcloud unverschlüsselt; ohne diese Einstellung erzeugt Nextcloud http-Links.')
    setting('overwritehost', pub_host, cfg.get('overwritehost') or '–', cfg.get('overwritehost') == pub_host,
            'Name, den Nextcloud für Links verwendet, wenn die Anfrage über den Proxy kommt.')
    if path:
        setting('overwritewebroot', path, cfg.get('overwritewebroot') or '–', cfg.get('overwritewebroot') == path,
                'Pfad der Nextcloud für Anfragen über den Proxy.')
    if single:
        cond = '^' + re.escape(proxy_addr) + '$'
        setting('overwritecondaddr', cond, cfg.get('overwritecondaddr') or '–', cfg.get('overwritecondaddr') == cond,
                'Die overwrite-Einstellungen gelten nur für Anfragen vom Proxy. So funktioniert der direkte Zugriff im '
                f'LAN über {upstream}{path} weiterhin.')
    else:
        notes.append('Für ein ganzes Netz als Proxy lässt der Assistent overwritecondaddr weg – die overwrite-Einstellungen '
                     'gelten dann für alle Zugriffe, auch im LAN. Mit einer einzelnen Proxy-IP geht das gezielter.')
    cli = str(cfg.get('overwrite.cli.url') or '').rstrip('/')
    root_base = f"https://{pub_host}"
    accepted = {public_base} | ({root_base} if v['root_alias'] and path else set())
    setting('overwrite.cli.url', public_base, cli or '–', cli in accepted,
            'Adresse für Links, die Hintergrundjobs und occ erzeugen (z. B. in Benachrichtigungs-Mails).'
            + (f' Mit „Auch unter der Domain-Wurzel erreichbar“ funktioniert auch {root_base} (ohne {path}), weil '
               'index.php, remote.php und ocs dann auch an der Wurzel ankommen.' if v['root_alias'] and path else
               (f' Ohne die Erreichbarkeit an der Domain-Wurzel muss {path} dabei sein, sonst führen Links ins Leere.'
                if path else '')))
    p = dict(host=v['pub_host'], path=path, up=upstream, pub=public_base, hsts=HSTS,
             port=v['pub_port'] if v['pub_port'] and v['pub_port'] != 443 else None)
    snippets = {'npm': _npm(p, v['root_alias']), 'nginx': _nginx(p, v['root_alias']), 'apache': _apache(p, v['root_alias']),
                'caddy': _caddy(p, v['root_alias'])}
    if v['root_alias'] and not path:
        notes.append('„Auch unter der Domain-Wurzel erreichbar“ ist nur nötig, wenn Nextcloud in einem Unterverzeichnis liegt.')
    return dict(settings=settings, warnings=warnings, notes=notes, snippets=snippets, public_base=public_base, pub_hostport=pub_host,
                upstream=upstream, path=path, proxy_addr=proxy_addr, missing=sum(1 for s in settings if not s['ok']))


def _shq(s):
    s = str(s)
    return s if re.fullmatch(r'[A-Za-z0-9._:/@+=-]+', s) else "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- Proxy-Konfigurationen

def _nginx_locations(p, root_alias, proto='https'):
    """Gemeinsamer Kern für nginx und Nginx Proxy Manager (Muster aus einer laufenden DietPi-Installation)."""
    P, U = p['path'], p['up']
    h = '$http_host' if p.get('port') else '$host'      # bei eigenem Port muss er in Weiterleitungen erhalten bleiben
    hdr = ('    proxy_set_header Host $host;\n'
           '    proxy_set_header X-Real-IP $remote_addr;\n'
           '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
           f'    proxy_set_header X-Forwarded-Proto {proto};\n')
    big = ('    client_max_body_size 0;          # keine Grenze; große Dateien laden die Clients ohnehin in Teilstücken\n'
           '    proxy_request_buffering off;\n'
           '    proxy_read_timeout 3600s;\n'
           '    proxy_send_timeout 3600s;\n')
    ws = ('    proxy_http_version 1.1;\n'
          '    proxy_set_header Upgrade $http_upgrade;\n'
          '    proxy_set_header Connection "upgrade";   # WebSockets (Talk, Benachrichtigungen)\n')
    out = []
    loc = f'{P}/' if P else '/'
    out.append(f'# Hauptweiterleitung\nlocation {loc} {{\n{hdr}{big}{ws}    proxy_pass {U}{P}/;\n}}\n')
    if P:
        out.append(f'# {P} ohne abschließenden Schrägstrich\nlocation = {P} {{\n    return 301 https://{h}{P}/;\n}}\n')
        out.append(f'# Domain-Wurzel auf Nextcloud umleiten\nlocation = / {{\n    return 301 https://{h}{P}/;\n}}\n')
    out.append('# Erkennung für Kalender, Kontakte und Föderation\n'
               f'location = /.well-known/carddav {{ return 301 https://{h}{P}/remote.php/dav/; }}\n'
               f'location = /.well-known/caldav  {{ return 301 https://{h}{P}/remote.php/dav/; }}\n'
               f'location = /.well-known/webfinger {{ return 301 https://{h}{P}/index.php/.well-known/webfinger; }}\n'
               f'location = /.well-known/nodeinfo  {{ return 301 https://{h}{P}/index.php/.well-known/nodeinfo; }}\n')
    if root_alias and P:
        parts = ['# Kern-Endpunkte zusätzlich ohne ' + P + ' (für Clients, in denen nur die Domain eingetragen ist)\n']
        for loc2, target, extra in (('^~ /remote.php', 'remote.php', big), ('^~ /ocs', 'ocs', ''),
                                    ('^~ /index.php', 'index.php', ''), ('= /status.php', 'status.php', '')):
            parts.append(f'location {loc2} {{\n{hdr}{extra}    proxy_pass {U}{P}/{target};\n}}\n')
        out.append(''.join(parts))
    return '\n'.join(out)


def _npm(p, root_alias):
    return dict(
        steps=[('Details', f'Domain Names: {p["host"]} · Scheme: {p["up"].split("://")[0]} · Forward Hostname/IP: '
                           f'{p["up"].split("://")[1].split(":")[0]} · Forward Port: '
                           f'{p["up"].rsplit(":", 1)[1] if p["up"].count(":") > 1 else "80"} · '
                           'Block Common Exploits und Websockets Support einschalten'),
               ('Custom locations', 'leer lassen'),
               ('SSL', 'Zertifikat (z. B. Let\'s Encrypt) wählen; Force SSL, HTTP/2 Support, HSTS Enabled und '
                       'HSTS Subdomains einschalten'),
               ('Advanced', 'den folgenden Text in „Custom Nginx Configuration“ einfügen')],
        config=_nginx_locations(p, root_alias),
        note='NPM legt den server-Block selbst an; in „Advanced“ stehen deshalb nur die location-Abschnitte. '
             'X-Forwarded-Proto ist fest auf https gesetzt, weil NPM die Verschlüsselung übernimmt.')


def _nginx(p, root_alias):
    body = _nginx_locations(p, root_alias)
    body = '\n'.join(('    ' + ln if ln else ln) for ln in body.split('\n'))
    return dict(config=f'''# /etc/nginx/sites-available/{p["host"]}  (danach: ln -s … sites-enabled/, nginx -t, systemctl reload nginx)
server {{
    listen 80;
    listen [::]:80;
    server_name {p["host"]};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;                        # nginx älter als 1.25.1: stattdessen „listen 443 ssl http2;“
    server_name {p["host"]};

    ssl_certificate     /etc/letsencrypt/live/{p["host"]}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{p["host"]}/privkey.pem;
    add_header Strict-Transport-Security "{p["hsts"]}" always;

{body}
}}
''', note='Zertifikat z. B. mit certbot holen. Pfade der Zertifikate bei Bedarf anpassen.')


def _apache(p, root_alias):
    P, U = p['path'], p['up']
    lines = [f'# /etc/apache2/sites-available/{p["host"]}.conf',
             '# Module: a2enmod proxy proxy_http proxy_wstunnel headers rewrite ssl',
             '<VirtualHost *:80>', f'    ServerName {p["host"]}', f'    Redirect permanent / https://{p["host"]}/',
             '</VirtualHost>', '', '<VirtualHost *:443>', f'    ServerName {p["host"]}', '    SSLEngine on',
             f'    SSLCertificateFile    /etc/letsencrypt/live/{p["host"]}/fullchain.pem',
             f'    SSLCertificateKeyFile /etc/letsencrypt/live/{p["host"]}/privkey.pem',
             f'    Header always set Strict-Transport-Security "{p["hsts"]}"', '',
             *(['    SSLProxyEngine on            # Nextcloud intern per https',
                '    # SSLProxyVerify none / SSLProxyCheckPeerName off  – nur bei selbst signiertem Zertifikat intern']
               if U.startswith('https') else []),
             '    ProxyPreserveHost On', '    ProxyRequests Off', '    RequestHeader set X-Forwarded-Proto "https"',
             '    ProxyTimeout 3600', '    LimitRequestBody 0', '', '    RewriteEngine On',
             f'    RewriteRule ^/\\.well-known/carddav https://%{{HTTP_HOST}}{P}/remote.php/dav/ [R=301,L]',
             f'    RewriteRule ^/\\.well-known/caldav  https://%{{HTTP_HOST}}{P}/remote.php/dav/ [R=301,L]',
             f'    RewriteRule ^/\\.well-known/webfinger https://%{{HTTP_HOST}}{P}/index.php/.well-known/webfinger [R=301,L]',
             f'    RewriteRule ^/\\.well-known/nodeinfo  https://%{{HTTP_HOST}}{P}/index.php/.well-known/nodeinfo [R=301,L]']
    if P:
        lines += [f'    RewriteRule ^/?$ https://%{{HTTP_HOST}}{P}/ [R=301,L]',
                  f'    RewriteRule ^{P}$ https://%{{HTTP_HOST}}{P}/ [R=301,L]']
    if root_alias and P:
        lines += ['', f'    # Kern-Endpunkte zusätzlich ohne {P}']
        for t in ('remote.php', 'ocs', 'index.php', 'status.php'):
            lines += [f'    ProxyPass        /{t} {U}{P}/{t}', f'    ProxyPassReverse /{t} {U}{P}/{t}']
    lines += ['', '    # Hauptweiterleitung (upgrade=websocket: WebSockets, ab Apache 2.4.47)',
              f'    ProxyPass        {P}/ {U}{P}/ upgrade=websocket', f'    ProxyPassReverse {P}/ {U}{P}/', '</VirtualHost>']
    return dict(config='\n'.join(lines) + '\n',
                note='mod_proxy_http setzt X-Forwarded-For selbst. Zertifikat z. B. mit certbot holen.')


def _caddy(p, root_alias):
    P, U = p['path'], p['up']
    hostport = U.split('://', 1)[1]
    lines = ['# /etc/caddy/Caddyfile  (Caddy holt das Zertifikat selbst; danach: systemctl reload caddy)',
             f'{p["host"]} {{', f'    header Strict-Transport-Security "{p["hsts"]}"', '',
             f'    redir /.well-known/carddav {P}/remote.php/dav/ 301',
             f'    redir /.well-known/caldav  {P}/remote.php/dav/ 301',
             f'    redir /.well-known/webfinger {P}/index.php/.well-known/webfinger 301',
             f'    redir /.well-known/nodeinfo  {P}/index.php/.well-known/nodeinfo 301']
    if P:
        lines += [f'    redir / {P}/ 301', f'    redir {P} {P}/ 301']
    if root_alias and P:
        lines += ['', f'    # Kern-Endpunkte zusätzlich ohne {P}',
                  '    @kern path /remote.php* /ocs* /index.php* /status.php',
                  '    handle @kern {', f'        rewrite * {P}{{uri}}', f'        reverse_proxy {hostport}', '    }']
    lines += ['', '    # Hauptweiterleitung – X-Forwarded-* und WebSockets erledigt Caddy selbst',
              f'    reverse_proxy {hostport}', '}']
    if U.startswith('https'):
        lines.insert(-1, '    # Nextcloud intern per https: in reverse_proxy „transport http { tls }“ ergänzen')
    return dict(config='\n'.join(lines) + '\n', note='Caddy setzt X-Forwarded-For und X-Forwarded-Proto automatisch.')


# ---------------------------------------------------------------- Test vom Nextcloud-Server aus

class _NoRedirect(__import__('urllib.request').request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def run_tests(public_base, host, path, root_alias, timeout=6):
    """Ruft die öffentliche Adresse vom Server aus auf. Nur lesend; nichts wird geändert."""
    import json
    import ssl
    import urllib.error
    import urllib.request
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    results = []

    def fetch(url):
        try:
            r = opener.open(urllib.request.Request(url, headers={'User-Agent': 'nc-manager-proxytest'}), timeout=timeout)
            return r.status, dict(r.headers), r.read(4096).decode('utf-8', 'replace'), ''
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), '', ''
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                return 0, {}, '', f'Zertifikat ungültig: {reason.verify_message}'
            return 0, {}, '', f'nicht erreichbar: {reason}'
        except (OSError, ValueError) as e:
            return 0, {}, '', f'nicht erreichbar: {e}'

    st, hdr, body, err = fetch(public_base + '/status.php')
    try:
        j = json.loads(body) if st == 200 else {}
    except ValueError:
        j = {}
    ok = st == 200 and j.get('installed') is True
    results.append(dict(name=f'{public_base}/status.php', ok=ok,
                        detail=(f"Nextcloud {j.get('versionstring', '')} antwortet" if ok else err or f'HTTP {st} – keine Nextcloud-Antwort')))
    reach = st != 0
    hsts = {k.lower(): v for k, v in hdr.items()}.get('strict-transport-security', '')
    results.append(dict(name='HSTS-Header', ok=bool(hsts), detail=hsts or ('kein Header' if reach else '–')))
    for wk in ('caldav', 'carddav'):
        st2, hdr2, _, err2 = fetch(f'https://{host}/.well-known/{wk}')
        loc = {k.lower(): v for k, v in hdr2.items()}.get('location', '')
        good = st2 in (301, 302, 307, 308) and '/remote.php/dav' in loc and not loc.startswith('http://')
        results.append(dict(name=f'/.well-known/{wk}', ok=good,
                            detail=f'{st2} → {loc}' if st2 else err2))
    if root_alias and path:
        st3, _, body3, err3 = fetch(f'https://{host}/status.php')
        results.append(dict(name=f'https://{host}/status.php (ohne {path})', ok=st3 == 200 and '"installed"' in body3,
                            detail=f'HTTP {st3}' if st3 else err3))
    hint = '' if reach else ('Der Server erreicht seine öffentliche Adresse nicht. Häufige Ursache: Der Router kann eine '
                             'öffentliche Adresse nicht von innen aufrufen (fehlendes „NAT-Loopback“). Abhilfe: lokaler '
                             'DNS-Eintrag, der die Domain im Heimnetz auf den Proxy zeigt – oder den Test von außen '
                             'machen (z. B. Handy ohne WLAN).')
    return dict(results=results, hint=hint, reach=reach)
