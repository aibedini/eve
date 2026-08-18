"""Subscription-page, general, SSL, session, and health-log settings API routes (extracted from app.py)."""
import os
import re
import subprocess
from datetime import timedelta

from flask import Blueprint, jsonify, request, send_file, session
from sqlalchemy import func

from panel.extensions import db
from panel.models import (
    Admin, HealthLog, SystemSetting, UsageCounterState, UsageDaily,
    UsageHourly,
)
from panel.routes.common import login_required, user_management_required
from panel.services.backup import _parse_int, _set_system_setting_value


bp = Blueprint('settings', __name__)

PERSISTED_DOMAIN_PATH = '/etc/eve-manager/domain'


def _nginx_conf_path() -> str:
    return '/etc/nginx/sites-available/eve-manager'


def _build_nginx_config(domain: str, app_port: str, cert_path: str = '', key_path: str = '') -> str:
    """Build nginx config for HTTP or HTTPS depending on whether cert paths are given."""
    sse_block = f"""
    location ~* /stream$ {{
        proxy_pass http://127.0.0.1:{app_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
    }}"""
    proxy_block = f"""
    location / {{
        proxy_pass http://127.0.0.1:{app_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        proxy_request_buffering off;
    }}"""
    backup_block = """
    location /protected-backups/ {
        internal;
        alias /opt/eve-xui-manager/instance/backups/;
    }"""

    if cert_path and key_path:
        return f"""server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {domain};
    ssl_certificate     {cert_path};
    ssl_certificate_key {key_path};
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    client_max_body_size 2048m;
{sse_block}
{proxy_block}
{backup_block}
}}
"""
    else:
        return f"""server {{
    listen 80;
    server_name {domain};
    client_max_body_size 2048m;
{sse_block}
{proxy_block}
{backup_block}
}}
"""


def _apply_nginx_config(domain: str, cert_path: str = '', key_path: str = '') -> tuple[bool, str]:
    """Write nginx config, reload it, and persist the canonical domain."""
    if not re.fullmatch(r'[A-Za-z0-9.-]+', domain or '') or domain.startswith(('.', '-')):
        return False, 'Invalid panel domain or IP address'
    app_port = os.environ.get('API_PORT', '5000')
    config = _build_nginx_config(domain, app_port, cert_path, key_path)
    conf_path = _nginx_conf_path()

    previous_config = None
    try:
        with open(conf_path, 'r', errors='ignore') as config_file:
            previous_config = config_file.read()
    except OSError:
        pass

    def run(cmd, stdin=None):
        try:
            return subprocess.run(
                cmd, input=stdin, text=True, capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            return subprocess.CompletedProcess(cmd, 1, stdout='', stderr=str(exc))

    def restore_previous():
        if previous_config is None:
            return
        run(['sudo', 'tee', conf_path], previous_config)
        run(['sudo', 'nginx', '-t'])
        run(['sudo', 'systemctl', 'reload', 'nginx'])

    write_result = run(['sudo', 'tee', conf_path], config)
    if write_result.returncode != 0:
        return False, f'sudo tee failed: {write_result.stderr.strip()}'

    test_result = run(['sudo', 'nginx', '-t'])
    if test_result.returncode != 0:
        restore_previous()
        return False, f'sudo nginx failed: {test_result.stderr.strip()}'

    reload_result = run(['sudo', 'systemctl', 'reload', 'nginx'])
    if reload_result.returncode != 0:
        restore_previous()
        return False, f'sudo systemctl failed: {reload_result.stderr.strip()}'

    persist_result = run(['sudo', 'tee', PERSISTED_DOMAIN_PATH], domain)
    if persist_result.returncode != 0:
        return False, f'sudo tee failed: {persist_result.stderr.strip()}'
    return True, ''


@bp.route('/api/settings/subscription-page', methods=['GET'])
@user_management_required
def get_subscription_page_settings():
    from app import (  # deferred: app-level helper, avoids circular import
        _get_or_create_system_setting, app,
    )
    lang = (_get_or_create_system_setting('subscription_page_lang', 'en') or 'en').strip().lower()
    if lang not in ('fa', 'en'):
        lang = 'en'
    return jsonify({'success': True, 'lang': lang})


@bp.route('/api/settings/subscription-page', methods=['POST'])
@user_management_required
def save_subscription_page_settings():
    from app import app  # deferred: app-level helper, avoids circular import
    try:
        data = request.get_json() or {}
    except Exception:
        data = {}

    lang = (data.get('lang') or '').strip().lower()
    if lang not in ('fa', 'en'):
        return jsonify({'success': False, 'error': 'Invalid language. Allowed: fa, en'}), 400

    setting = db.session.get(SystemSetting, 'subscription_page_lang')
    if not setting:
        setting = SystemSetting(key='subscription_page_lang', value=lang)
        db.session.add(setting)
    else:
        setting.value = lang

    db.session.commit()
    return jsonify({'success': True, 'message': 'Subscription page settings saved', 'lang': lang})


@bp.route('/api/settings/general', methods=['GET'])
@user_management_required
def get_general_settings():
    from app import (  # deferred: app-level helper, avoids circular import
        PANEL_DOMAIN_SETTING_KEY, _get_app_calendar_name,
        _get_app_timezone_name, _get_dashboard_status_thresholds,
        _get_or_create_system_setting, _get_panel_ui_lang,
        _get_standard_timezone_options, _is_ip_address, app,
    )
    thresholds = _get_dashboard_status_thresholds()
    # Last compact rollup timestamp (across all accounts).
    try:
        last_at = db.session.query(func.max(UsageCounterState.observed_at)).scalar()
        last_snapshot_at = (last_at.isoformat() + 'Z') if last_at else None
        total_snapshots = UsageDaily.query.count() + UsageHourly.query.count()
    except Exception:
        last_snapshot_at = None
        total_snapshots = 0
    panel_domain = (_get_or_create_system_setting(PANEL_DOMAIN_SETTING_KEY, '') or '').strip()
    ssl_cert = (db.session.get(SystemSetting, 'ssl_cert_path') or SystemSetting(key='', value='')).value or ''
    has_ssl  = bool(ssl_cert and os.path.isfile(ssl_cert))

    return jsonify({
        'success': True,
        'timezone': _get_app_timezone_name(),
        'timezone_options': _get_standard_timezone_options(),
        'calendar': _get_app_calendar_name(),
        'panel_lang': _get_panel_ui_lang(),
        'near_expiry_days': thresholds.get('near_expiry_days', 3),
        'near_expiry_hours': thresholds.get('near_expiry_hours', 0),
        'low_volume_gb': thresholds.get('low_volume_gb', 1.0),
        'snapshot_interval_minutes': _USAGE_ROLLUP_INTERVAL_MIN,
        'last_snapshot_at': last_snapshot_at,
        'total_snapshots': total_snapshots,
        'panel_domain': panel_domain,
        'is_ip': _is_ip_address(panel_domain),
        'has_ssl': has_ssl,
    })


@bp.route('/api/settings/general', methods=['POST'])
@user_management_required
def save_general_settings():
    from app import (  # deferred: app-level helper, avoids circular import
        DEFAULT_APP_CALENDAR, DEFAULT_APP_TIMEZONE,
        GENERAL_CALENDAR_SETTING_KEY, GENERAL_EXPIRY_WARNING_DAYS_KEY,
        GENERAL_EXPIRY_WARNING_HOURS_KEY,
        GENERAL_LOW_VOLUME_WARNING_GB_KEY, GENERAL_TIMEZONE_SETTING_KEY,
        PANEL_DOMAIN_SETTING_KEY, PANEL_UI_LANG_SETTING_KEY,
        _get_or_create_system_setting, _get_standard_timezone_options,
        _is_ip_address, _is_valid_timezone_name, _normalize_calendar_name,
        _normalize_timezone_name, _normalize_ui_lang, app,
    )
    try:
        data = request.get_json() or {}
    except Exception:
        data = {}

    tz_name = (data.get('timezone') or '').strip()
    if not tz_name:
        tz_name = DEFAULT_APP_TIMEZONE

    tz_name = _normalize_timezone_name(tz_name) or tz_name

    if not _is_valid_timezone_name(tz_name):
        return jsonify({
            'success': False,
            'error': 'Invalid timezone. Example: Asia/Tehran'
        }), 400

    panel_lang = _normalize_ui_lang(data.get('panel_lang'), default='en')
    calendar_name = _normalize_calendar_name(data.get('calendar') or DEFAULT_APP_CALENDAR)
    if not calendar_name:
        return jsonify({'success': False, 'error': 'Invalid calendar. Allowed: jalali, gregorian'}), 400

    near_expiry_days = _parse_int(data.get('near_expiry_days'), 3, min_value=0, max_value=365)
    near_expiry_hours = _parse_int(data.get('near_expiry_hours'), 0, min_value=0, max_value=23)

    try:
        low_volume_gb = float(data.get('low_volume_gb', 1.0) or 1.0)
    except Exception:
        low_volume_gb = 1.0
    low_volume_gb = max(0.01, min(low_volume_gb, 1024.0))

    # Domain update + nginx reload
    new_domain = (data.get('panel_domain') or '').strip()
    old_domain = (_get_or_create_system_setting(PANEL_DOMAIN_SETTING_KEY, '') or '').strip()
    nginx_updated = False
    nginx_error = ''

    if new_domain and new_domain != old_domain:
        cert_path = (db.session.get(SystemSetting, 'ssl_cert_path') or SystemSetting(key='', value='')).value or ''
        key_path  = (db.session.get(SystemSetting, 'ssl_key_path')  or SystemSetting(key='', value='')).value or ''
        use_ssl   = bool(cert_path and key_path
                         and os.path.isfile(cert_path) and os.path.isfile(key_path)
                         and not _is_ip_address(new_domain))
        ok, nginx_error = _apply_nginx_config(
            new_domain,
            cert_path if use_ssl else '',
            key_path  if use_ssl else '',
        )
        nginx_updated = ok
        if not ok:
            app.logger.warning("nginx update failed when changing domain to %s: %s", new_domain, nginx_error)

    _set_system_setting_value(GENERAL_TIMEZONE_SETTING_KEY, tz_name)
    _set_system_setting_value(GENERAL_CALENDAR_SETTING_KEY, calendar_name)
    _set_system_setting_value(PANEL_UI_LANG_SETTING_KEY, panel_lang)
    _set_system_setting_value(GENERAL_EXPIRY_WARNING_DAYS_KEY, str(near_expiry_days))
    _set_system_setting_value(GENERAL_EXPIRY_WARNING_HOURS_KEY, str(near_expiry_hours))
    _set_system_setting_value(GENERAL_LOW_VOLUME_WARNING_GB_KEY, str(low_volume_gb))
    if new_domain:
        _set_system_setting_value(PANEL_DOMAIN_SETTING_KEY, new_domain)
    db.session.commit()

    is_ip    = _is_ip_address(new_domain or old_domain)
    protocol = 'http' if (is_ip or not nginx_updated) else 'https'
    panel_url = f'{protocol}://{new_domain or old_domain}' if (new_domain or old_domain) else ''

    return jsonify({
        'success': True,
        'message': 'General settings saved',
        'timezone': tz_name,
        'timezone_options': _get_standard_timezone_options(),
        'calendar': calendar_name,
        'panel_lang': panel_lang,
        'near_expiry_days': near_expiry_days,
        'near_expiry_hours': near_expiry_hours,
        'low_volume_gb': low_volume_gb,
        'snapshot_interval_minutes': _USAGE_ROLLUP_INTERVAL_MIN,
        'panel_domain': new_domain or old_domain,
        'is_ip': is_ip,
        'nginx_updated': nginx_updated,
        'nginx_error': nginx_error,
        'panel_url': panel_url,
    })


@bp.route('/api/settings/ssl/diagnose', methods=['GET'])
@login_required
def diagnose_ssl():
    """Figure out where HTTPS is actually coming from for THIS request.

    Three cases the panel could be in:
      1. own_ssl  — origin server has its own cert (nginx serves HTTPS directly)
      2. cdn_ssl  — a CDN (e.g. Cloudflare) terminates TLS at the edge; the
                    origin may be plain HTTP (browser still shows the padlock)
      3. none     — no SSL anywhere; served over plain HTTP

    We read the headers of the admin's own request (which reflect exactly how
    their browser reached the panel) plus the origin certificate status.
    """
    from app import (  # deferred: app-level helper, avoids circular import
        _autodetect_ssl_paths, app,
    )
    h = request.headers

    # --- CDN detection (Cloudflare is the common one) ---
    cf_ray = h.get('CF-Ray')
    cf_conn_ip = h.get('CF-Connecting-IP')
    cf_visitor = (h.get('CF-Visitor') or '').lower()
    via = h.get('Via') or ''
    behind_cloudflare = bool(cf_ray or cf_conn_ip)
    # Other CDNs / proxies leave a trail in Via or known headers
    other_cdn = None
    for hdr, name in (('X-Sucuri-ID', 'Sucuri'), ('X-Fastly-Request-ID', 'Fastly'),
                      ('X-Amz-Cf-Id', 'CloudFront'), ('X-Cache', 'Generic CDN')):
        if h.get(hdr):
            other_cdn = name
            break

    # --- Edge scheme: what scheme did the END USER's browser use? ---
    # CF-Visitor carries the real client scheme even when CF→origin is HTTP.
    xfp = (h.get('X-Forwarded-Proto') or '').lower()
    xfp_first = xfp.split(',')[0].strip() if xfp else ''
    edge_https = (
        'https' in cf_visitor
        or xfp_first == 'https'
        or bool(request.is_secure)
    )

    # --- Origin certificate status (reuse autodetect) ---
    cert_path, key_path = _autodetect_ssl_paths()
    # also honor an explicitly saved path
    saved_cert = db.session.get(SystemSetting, 'ssl_cert_path')
    if saved_cert and saved_cert.value:
        cert_path = saved_cert.value
    origin_has_cert = bool(cert_path) and os.path.isfile(cert_path) and os.access(cert_path, os.R_OK)

    # --- Verdict ---
    if behind_cloudflare or other_cdn:
        cdn_name = 'Cloudflare' if behind_cloudflare else other_cdn
        if origin_has_cert:
            mode = 'cdn_plus_origin'
            title = f'HTTPS via {cdn_name} (origin also has a certificate)'
            detail = (f'You are reaching the panel through {cdn_name}, which provides the '
                      f'browser padlock. Your origin server ALSO has its own certificate, '
                      f'so a Full/Strict CDN SSL mode works and direct access stays secure.')
        else:
            mode = 'cdn_only'
            title = f'HTTPS is provided by {cdn_name} — your server has NO certificate'
            detail = (f'The padlock you see comes from {cdn_name} at the edge. Your origin '
                      f'server itself has no SSL certificate. If you turn off the CDN proxy '
                      f'(grey-cloud the DNS record) the panel will fall back to plain HTTP, '
                      f'and any subscription/dash links that depend on HTTPS may break. '
                      f'Consider installing a free Origin/Let\'s Encrypt cert too.')
    elif origin_has_cert and edge_https:
        mode = 'own_ssl'
        title = 'Your server has its own SSL certificate (direct HTTPS)'
        detail = 'No CDN detected. nginx is serving HTTPS directly from a certificate on this server.'
    elif edge_https:
        mode = 'edge_only'
        title = 'HTTPS detected, but no certificate found on this server'
        detail = ('The connection looks secure but no certificate file was found on the origin. '
                  'A reverse proxy or load balancer in front is likely terminating TLS.')
    else:
        mode = 'none'
        title = 'No SSL — the panel is served over plain HTTP'
        detail = ('No CDN and no certificate detected, and this request is not HTTPS. '
                  'Install a certificate (Settings → SSL) or put the domain behind a CDN.')

    return jsonify({
        'success': True,
        'mode': mode,
        'title': title,
        'detail': detail,
        'edge_https': edge_https,
        'behind_cdn': bool(behind_cloudflare or other_cdn),
        'cdn': ('Cloudflare' if behind_cloudflare else other_cdn),
        'origin_has_cert': origin_has_cert,
        'origin_cert_path': cert_path or None,
        'request_scheme': request.scheme,
        'signals': {
            'cf_ray': bool(cf_ray),
            'cf_connecting_ip': bool(cf_conn_ip),
            'cf_visitor': cf_visitor or None,
            'x_forwarded_proto': xfp or None,
            'via': via or None,
            'host': h.get('Host'),
        },
    })


@bp.route('/api/settings/ssl', methods=['GET'])
@login_required
def get_ssl_settings():
    from app import (  # deferred: app-level helper, avoids circular import
        _autodetect_ssl_paths, app,
    )
    cert = db.session.get(SystemSetting, 'ssl_cert_path')
    key = db.session.get(SystemSetting, 'ssl_key_path')
    cert_path = cert.value if cert else ''
    key_path = key.value if key else ''

    auto_detected = False
    if not cert_path and not key_path:
        detected_cert, detected_key = _autodetect_ssl_paths()
        if detected_cert and detected_key:
            cert_path = detected_cert
            key_path = detected_key
            auto_detected = True
            # Persist so the settings page shows the correct state going forward
            try:
                c_row = SystemSetting(key='ssl_cert_path', value=cert_path)
                k_row = SystemSetting(key='ssl_key_path', value=key_path)
                db.session.merge(c_row)
                db.session.merge(k_row)
                db.session.commit()
            except Exception:
                db.session.rollback()

    cert_ok = bool(cert_path) and os.path.isfile(cert_path) and os.access(cert_path, os.R_OK)
    key_ok = bool(key_path) and os.path.isfile(key_path) and os.access(key_path, os.R_OK)

    if cert_path or key_path:
        ssl_status = 'active' if (cert_ok and key_ok) else 'error'
    else:
        ssl_status = 'not_configured'

    # Provisional SSL type from path; refined below once the cert is parsed.
    ssl_type = 'none'
    if cert_path:
        if '/etc/letsencrypt/' in cert_path:
            ssl_type = 'letsencrypt'
        elif '/etc/ssl/eve-manager/' in cert_path:
            ssl_type = 'self_signed'
        elif cert_path:
            ssl_type = 'custom'

    # Parse cert metadata
    cert_expiry = None
    cert_issuer = None
    cert_subject = None
    if cert_ok:
        try:
            from cryptography import x509 as _x509
            from cryptography.hazmat.backends import default_backend as _default_backend
            from cryptography.x509.oid import NameOID as _NameOID
            with open(cert_path, 'rb') as _f:
                _cert = _x509.load_pem_x509_certificate(_f.read(), _default_backend())
            _exp = getattr(_cert, 'not_valid_after_utc', None) or _cert.not_valid_after
            cert_expiry = _exp.isoformat()
            try:
                cert_issuer = _cert.issuer.get_attributes_for_oid(_NameOID.COMMON_NAME)[0].value
            except Exception:
                cert_issuer = None
            try:
                cert_subject = _cert.subject.get_attributes_for_oid(_NameOID.COMMON_NAME)[0].value
            except Exception:
                cert_subject = None
            # Classify by the cert itself, not the file path: a real Let's Encrypt
            # cert is copied into /etc/ssl/eve-manager/ so path-based detection
            # mislabels it "self_signed". Self-signed iff issuer DN == subject DN.
            if _cert.issuer == _cert.subject:
                ssl_type = 'self_signed'
            else:
                _issuer_org = ''
                try:
                    _issuer_org = (_cert.issuer.get_attributes_for_oid(_NameOID.ORGANIZATION_NAME)[0].value or '')
                except Exception:
                    _issuer_org = ''
                if '/etc/letsencrypt/' in cert_path or "let's encrypt" in _issuer_org.lower():
                    ssl_type = 'letsencrypt'
                else:
                    ssl_type = 'custom'
        except Exception:
            pass

    return jsonify({
        'success': True,
        'cert_path': cert_path,
        'key_path': key_path,
        'cert_ok': cert_ok,
        'key_ok': key_ok,
        'ssl_status': ssl_status,
        'ssl_type': ssl_type,
        'cert_expiry': cert_expiry,
        'cert_issuer': cert_issuer,
        'cert_subject': cert_subject,
        'auto_detected': auto_detected
    })


@bp.route('/api/settings/ssl', methods=['POST'])
@login_required
def save_ssl_settings():
    from app import app  # deferred: app-level helper, avoids circular import
    data = request.json
    cert_path = data.get('cert_path', '').strip()
    key_path = data.get('key_path', '').strip()

    # Both must be provided together or both empty
    if bool(cert_path) != bool(key_path):
        missing = 'Private key path' if cert_path else 'Certificate path'
        return jsonify({'success': False, 'error': f'{missing} is required'}), 400

    if cert_path:
        if not os.path.isfile(cert_path):
            return jsonify({'success': False, 'error': f'Certificate file not found: {cert_path}'}), 400
        if not os.access(cert_path, os.R_OK):
            return jsonify({'success': False, 'error': f'Certificate file is not readable (check permissions): {cert_path}'}), 400

    if key_path:
        if not os.path.isfile(key_path):
            return jsonify({'success': False, 'error': f'Private key file not found: {key_path}'}), 400
        if not os.access(key_path, os.R_OK):
            return jsonify({'success': False, 'error': f'Private key file is not readable (check permissions): {key_path}'}), 400

    cert_setting = db.session.get(SystemSetting, 'ssl_cert_path')
    if not cert_setting:
        cert_setting = SystemSetting(key='ssl_cert_path', value=cert_path)
        db.session.add(cert_setting)
    else:
        cert_setting.value = cert_path

    key_setting = db.session.get(SystemSetting, 'ssl_key_path')
    if not key_setting:
        key_setting = SystemSetting(key='ssl_key_path', value=key_path)
        db.session.add(key_setting)
    else:
        key_setting.value = key_path

    db.session.commit()

    if cert_path and key_path:
        return jsonify({'success': True, 'message': 'SSL settings saved. Certificate and key files verified.'})
    return jsonify({'success': True, 'message': 'SSL configuration cleared.'})


# ── SSL Sync — copy LetsEncrypt certs to /etc/ssl/eve-manager/ via sudo ──────
@bp.route('/api/settings/ssl/sync', methods=['POST'])
@login_required
def ssl_sync():
    """Copy LetsEncrypt cert+key to /etc/ssl/eve-manager/.

    Strategy (no broad sudo needed):
    - /etc/ssl/eve-manager/ must be owned by evemgr (one-time admin setup)
    - Only `sudo cat` is used to read the protected privkey.pem
    - Everything else is done directly as evemgr

    If the destination dir isn't writable, a clear fix command is returned.
    """
    from app import app  # deferred: app-level helper, avoids circular import
    import glob as _glob, re as _re

    FIX_CMD = (
        "sudo bash -c '"
        "mkdir -p /etc/ssl/eve-manager && "
        "chown evemgr:evemgr /etc/ssl/eve-manager && "
        "chmod 700 /etc/ssl/eve-manager && "
        "cat > /etc/sudoers.d/eve-ssl <<EOF\n"
        "evemgr ALL=(root) NOPASSWD: /bin/cat /etc/letsencrypt/live/*/fullchain.pem\n"
        "evemgr ALL=(root) NOPASSWD: /bin/cat /etc/letsencrypt/live/*/privkey.pem\n"
        "evemgr ALL=(root) NOPASSWD: /bin/cat /etc/letsencrypt/archive/*/fullchain*.pem\n"
        "evemgr ALL=(root) NOPASSWD: /bin/cat /etc/letsencrypt/archive/*/privkey*.pem\n"
        "evemgr ALL=(root) NOPASSWD: /bin/systemctl reload nginx\n"
        "evemgr ALL=(root) NOPASSWD: /usr/sbin/nginx -t\n"
        "evemgr ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/sites-available/eve-manager\n"
        "evemgr ALL=(root) NOPASSWD: /bin/tee /etc/nginx/sites-available/eve-manager\n"
        "EOF\n"
        "chmod 440 /etc/sudoers.d/eve-ssl'"
    )

    dest_dir  = '/etc/ssl/eve-manager'
    cert_dest = os.path.join(dest_dir, 'fullchain.pem')
    key_dest  = os.path.join(dest_dir, 'privkey.pem')

    # Check destination directory is writable (owned by evemgr)
    if not os.path.isdir(dest_dir) or not os.access(dest_dir, os.W_OK):
        return jsonify({
            'success': False,
            'error': (
                f'/etc/ssl/eve-manager/ does not exist or is not writable by the app user.\n'
                f'Run this command on the server once, then try again:\n\n{FIX_CMD}'
            ),
            'fix_command': FIX_CMD,
        }), 500

    # Find source cert paths
    cert_src = key_src = ''
    for _nc in ['/etc/nginx/sites-available/eve-manager',
                '/etc/nginx/sites-enabled/eve-manager',
                '/etc/nginx/sites-available/eve-xui-manager']:
        if not os.path.isfile(_nc):
            continue
        try:
            with open(_nc, 'r', errors='ignore') as _f:
                _conf = _f.read()
            _cm = _re.search(r'ssl_certificate\s+([^;]+);', _conf)
            _km = _re.search(r'ssl_certificate_key\s+([^;]+);', _conf)
            if _cm and _km:
                cert_src = _cm.group(1).strip()
                key_src  = _km.group(1).strip()
                break
        except Exception:
            pass

    if not cert_src:
        for _lc in sorted(_glob.glob('/etc/letsencrypt/live/*/fullchain.pem')):
            cert_src = _lc
            key_src  = os.path.join(os.path.dirname(_lc), 'privkey.pem')
            break

    if not cert_src or not key_src:
        return jsonify({'success': False,
                        'error': 'Cannot find SSL cert paths. Is nginx configured with SSL?'}), 400

    # Helper: read a source file, falling back to `sudo cat` for mode-600 keys.
    # Never lets an unexpected subprocess error (missing sudo / timeout) bubble
    # up as a generic 500 — always returns a clean JSON error tuple instead.
    def _read_src(path, label):
        try:
            with open(path, 'rb') as _f:
                return _f.read(), None
        except PermissionError:
            pass
        except Exception as _e:
            return None, (jsonify({'success': False,
                                   'error': f'Cannot read {label}: {_e}'}), 500)
        try:
            r = subprocess.run(['sudo', 'cat', path], capture_output=True, timeout=10)
        except FileNotFoundError:
            return None, (jsonify({'success': False,
                                   'error': f'sudo not available — cannot read {label}.\n\nRun this on the server:\n\n{FIX_CMD}',
                                   'fix_command': FIX_CMD}), 500)
        except Exception as _e:
            return None, (jsonify({'success': False,
                                   'error': f'Cannot read {label} ({_e}).\n\nRun: {FIX_CMD}',
                                   'fix_command': FIX_CMD}), 500)
        if r.returncode != 0:
            err = r.stderr.decode(errors='ignore').strip()
            if 'password' in err.lower() or 'askpass' in err.lower() or 'terminal' in err.lower():
                return None, (jsonify({
                    'success': False,
                    'error': f'sudo not configured for this app user.\n\nRun this on the server:\n\n{FIX_CMD}',
                    'fix_command': FIX_CMD}), 500)
            return None, (jsonify({'success': False,
                                   'error': f'Cannot read {label}: {err}\n\nRun: {FIX_CMD}',
                                   'fix_command': FIX_CMD}), 500)
        return r.stdout, None

    # Helper: overwrite a destination file even if a stale copy is owned by root.
    # The dir is owned by evemgr (700), so we can unlink the old file and recreate
    # it fresh — this avoids PermissionError on open('wb') of a root-owned file
    # (the bug that produced a generic 500 on sync).
    def _write_dest(path, data, mode):
        try:
            if os.path.lexists(path):
                try:
                    os.remove(path)
                except PermissionError:
                    # Dir not writable enough to unlink — try sudo rm, best effort
                    subprocess.run(['sudo', 'rm', '-f', path], capture_output=True, timeout=10)
            with open(path, 'wb') as _f:
                _f.write(data)
            os.chmod(path, mode)
            return None
        except Exception as _e:
            return (jsonify({'success': False,
                             'error': f'Cannot write {path}: {_e}\n\nRun: {FIX_CMD}',
                             'fix_command': FIX_CMD}), 500)

    cert_data, _err = _read_src(cert_src, 'certificate')
    if _err:
        return _err
    key_data, _err = _read_src(key_src, 'private key')
    if _err:
        return _err

    _err = _write_dest(cert_dest, cert_data, 0o644)
    if _err:
        return _err
    _err = _write_dest(key_dest, key_data, 0o600)
    if _err:
        return _err

    # Persist paths in DB
    for k, v in [('ssl_cert_path', cert_dest), ('ssl_key_path', key_dest)]:
        row = db.session.get(SystemSetting, k) or SystemSetting(key=k, value=v)
        row.value = v
        db.session.merge(row)
    db.session.commit()

    return jsonify({
        'success': True,
        'message': f'Synced → {dest_dir}/',
        'cert_path': cert_dest,
        'key_path':  key_dest,
        'source_cert': cert_src,
    })


# ── SSL Export — download cert + key as a zip ───────────────────────────────
@bp.route('/api/settings/ssl/export')
@login_required
def ssl_export():
    """Return a zip containing the SSL certificate and private key."""
    from app import (  # deferred: app-level helper, avoids circular import
        _autodetect_ssl_paths, app,
    )
    import zipfile, io as _io

    cert = db.session.get(SystemSetting, 'ssl_cert_path')
    key  = db.session.get(SystemSetting, 'ssl_key_path')
    cert_path = (cert.value if cert else '').strip()
    key_path  = (key.value  if key  else '').strip()

    # Auto-detect if not saved
    if not cert_path or not key_path:
        cert_path, key_path = _autodetect_ssl_paths()

    # If still not found, try syncing from LetsEncrypt first
    if not cert_path or not key_path:
        return jsonify({
            'success': False,
            'error': 'SSL certificate not configured. Click "Sync from LetsEncrypt" first.'
        }), 400

    # Try to read — if permission denied, suggest sync
    errors = []
    for label, path in [('Certificate', cert_path), ('Private key', key_path)]:
        if not path or not os.path.isfile(path):
            errors.append(f'{label} file not found: {path or "(empty)"}')
        elif not os.access(path, os.R_OK):
            errors.append(f'{label} not readable (permission denied): {path} — click "Sync from LetsEncrypt" to fix')
    if errors:
        return jsonify({'success': False, 'error': ' | '.join(errors)}), 400

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write(cert_path, 'ssl/fullchain.pem')
        zf.write(key_path,  'ssl/privkey.pem')
    buf.seek(0)

    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name='eve-ssl-bundle.zip',
    )


# ── SSL Upload — receive zip, extract cert+key ──────────────────────────────
SSL_DEST_DIR = '/etc/ssl/eve-manager'


@bp.route('/api/settings/ssl/upload', methods=['POST'])
@login_required
def ssl_upload():
    """Accept a zip (with fullchain.pem + privkey.pem) or two individual files.

    Strategy:
    1. Write uploaded files to a temp dir that evemgr CAN write to (/tmp)
    2. Use sudo cp/mkdir/chown/chmod to install them under /etc/ssl/eve-manager/
       (sudoers entry created by setup.sh)
    """
    from app import app  # deferred: app-level helper, avoids circular import
    import zipfile, tempfile

    tmp_dir = tempfile.mkdtemp(prefix='eve-ssl-upload-')
    tmp_cert = os.path.join(tmp_dir, 'fullchain.pem')
    tmp_key  = os.path.join(tmp_dir, 'privkey.pem')

    try:
        if 'ssl_zip' in request.files:
            zf_file = request.files['ssl_zip']
            if not zf_file.filename.lower().endswith('.zip'):
                return jsonify({'success': False, 'error': 'Expected a .zip file'}), 400
            raw = zf_file.read()
            try:
                with zipfile.ZipFile(_io_BytesIO(raw)) as zf:
                    names = zf.namelist()
                    cert_member = next(
                        (n for n in names if n.endswith('fullchain.pem')
                         or n.endswith('.crt') or n.endswith('.cer')), None)
                    key_member = next(
                        (n for n in names if n.endswith('privkey.pem')
                         or n.endswith('.key')), None)
                    if not cert_member or not key_member:
                        return jsonify({'success': False,
                                        'error': 'Zip must contain fullchain.pem (or .crt) and privkey.pem (or .key)'}), 400
                    with open(tmp_cert, 'wb') as f:
                        f.write(zf.read(cert_member))
                    with open(tmp_key, 'wb') as f:
                        f.write(zf.read(key_member))
            except zipfile.BadZipFile:
                return jsonify({'success': False, 'error': 'Invalid zip file'}), 400

        elif 'cert_file' in request.files and 'key_file' in request.files:
            request.files['cert_file'].save(tmp_cert)
            request.files['key_file'].save(tmp_key)
        else:
            return jsonify({'success': False, 'error': 'Send ssl_zip OR cert_file+key_file'}), 400

        # Sanity check: must be PEM text
        with open(tmp_cert, 'r', errors='ignore') as _f:
            _head = _f.read(64)
        if '-----BEGIN' not in _head:
            return jsonify({'success': False,
                            'error': 'Certificate does not look like PEM — check the file'}), 400

        cert_dest = f'{SSL_DEST_DIR}/fullchain.pem'
        key_dest  = f'{SSL_DEST_DIR}/privkey.pem'

        FIX_CMD = (
            "sudo bash -c '"
            "mkdir -p /etc/ssl/eve-manager && "
            "chown evemgr:evemgr /etc/ssl/eve-manager && "
            "chmod 700 /etc/ssl/eve-manager && "
            "cat > /etc/sudoers.d/eve-ssl <<EOF\n"
            "evemgr ALL=(root) NOPASSWD: /bin/cat /etc/letsencrypt/live/*/fullchain.pem\n"
            "evemgr ALL=(root) NOPASSWD: /bin/cat /etc/letsencrypt/live/*/privkey.pem\n"
            "evemgr ALL=(root) NOPASSWD: /bin/cat /etc/letsencrypt/archive/*/fullchain*.pem\n"
            "evemgr ALL=(root) NOPASSWD: /bin/cat /etc/letsencrypt/archive/*/privkey*.pem\n"
            "evemgr ALL=(root) NOPASSWD: /bin/systemctl reload nginx\n"
            "evemgr ALL=(root) NOPASSWD: /usr/sbin/nginx -t\n"
            "evemgr ALL=(root) NOPASSWD: /usr/bin/tee /etc/nginx/sites-available/eve-manager\n"
            "evemgr ALL=(root) NOPASSWD: /bin/tee /etc/nginx/sites-available/eve-manager\n"
            "EOF\n"
            "chmod 440 /etc/sudoers.d/eve-ssl'"
        )

        # /etc/ssl/eve-manager/ must be owned by evemgr (one-time setup).
        # Then we write directly — no sudo needed at all for upload.
        if not os.path.isdir(SSL_DEST_DIR) or not os.access(SSL_DEST_DIR, os.W_OK):
            return jsonify({
                'success': False,
                'error': (
                    f'{SSL_DEST_DIR}/ does not exist or is not writable.\n'
                    f'Run this on the server once:\n\n{FIX_CMD}'
                ),
                'fix_command': FIX_CMD,
            }), 500

        import shutil as _shutil
        _shutil.copy2(tmp_cert, cert_dest)
        os.chmod(cert_dest, 0o644)
        _shutil.copy2(tmp_key, key_dest)
        os.chmod(key_dest, 0o600)

    finally:
        # Always clean up temp files
        import shutil as _sh
        _sh.rmtree(tmp_dir, ignore_errors=True)

    # Persist paths in DB
    for k, v in [('ssl_cert_path', cert_dest), ('ssl_key_path', key_dest)]:
        row = db.session.get(SystemSetting, k) or SystemSetting(key=k, value=v)
        row.value = v
        db.session.merge(row)
    db.session.commit()

    return jsonify({
        'success': True,
        'message': f'SSL files installed to {SSL_DEST_DIR}/',
        'cert_path': cert_dest,
        'key_path':  key_dest,
    })


def _io_BytesIO(data):
    import io
    return io.BytesIO(data)


# ── SSL Apply — write nginx config + reload ─────────────────────────────────
@bp.route('/api/settings/ssl/apply', methods=['POST'])
@login_required
def ssl_apply():
    """Write HTTPS nginx config and reload nginx."""
    cert = db.session.get(SystemSetting, 'ssl_cert_path')
    key  = db.session.get(SystemSetting, 'ssl_key_path')
    cert_path = (cert.value if cert else '').strip()
    key_path  = (key.value  if key  else '').strip()

    if not cert_path or not key_path:
        return jsonify({'success': False, 'error': 'SSL paths not configured. Upload or enter paths first.'}), 400

    if not os.path.isfile(cert_path):
        return jsonify({'success': False, 'error': f'Certificate not found: {cert_path}'}), 400
    if not os.path.isfile(key_path):
        return jsonify({'success': False, 'error': f'Private key not found: {key_path}'}), 400

    # Prefer the request and database setting over nginx. Older updaters could
    # temporarily rewrite nginx to the server IP even though the canonical
    # hostname remained correctly stored in the database.
    data = request.get_json(silent=True) or {}
    domain = (data.get('domain') or '').strip()
    if not domain:
        panel_domain = db.session.get(SystemSetting, 'panel_domain')
        domain = (panel_domain.value if panel_domain else '').strip()
    if not domain:
        try:
            with open(PERSISTED_DOMAIN_PATH, 'r', errors='ignore') as domain_file:
                domain = domain_file.read().strip()
        except Exception:
            pass

    nginx_conf_path = '/etc/nginx/sites-available/eve-manager'
    if not domain:
        try:
            with open(nginx_conf_path, 'r', errors='ignore') as nginx_file:
                match = re.search(r'server_name\s+([^;]+);', nginx_file.read())
                if match:
                    domain = match.group(1).strip().split()[0]
        except Exception:
            pass
    if not domain:
        return jsonify({'success': False, 'error': 'Cannot detect domain. Pass {"domain":"your.domain"} in request body.'}), 400

    # Apply and persist through the shared transactional nginx helper.
    ok, error = _apply_nginx_config(domain, cert_path, key_path)
    if not ok:
        return jsonify({'success': False, 'error': error}), 500

    return jsonify({
        'success': True,
        'message': f'SSL applied — nginx reloaded. Site is now HTTPS on {domain}',
        'domain': domain,
    })


@bp.route('/api/settings/session', methods=['GET'])
@login_required
def get_session_settings():
    from app import app  # deferred: app-level helper, avoids circular import
    setting = db.session.get(SystemSetting, 'session_timeout_hours')
    return jsonify({
        'success': True,
        'timeout_hours': int(setting.value) if setting else 168 # Default 7 days = 168 hours
    })


@bp.route('/api/settings/session', methods=['POST'])
@login_required
def save_session_settings():
    from app import app  # deferred: app-level helper, avoids circular import
    data = request.json
    try:
        hours = int(data.get('timeout_hours', 168))
        if hours < 1:
            return jsonify({'success': False, 'message': 'Timeout must be at least 1 hour'}), 400
            
        setting = db.session.get(SystemSetting, 'session_timeout_hours')
        if not setting:
            setting = SystemSetting(key='session_timeout_hours', value=str(hours))
            db.session.add(setting)
        else:
            setting.value = str(hours)
            
        db.session.commit()
        
        # Update config immediately
        app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=hours)
        
        return jsonify({'success': True, 'message': 'Session settings saved'})
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid value'}), 400


# ---------------------------------------------------------------------------
# Health Logs API
# ---------------------------------------------------------------------------
@bp.route('/api/settings/health-logs', methods=['GET'])
@login_required
def get_health_logs():
    """Paginated health logs for the Settings > System Logs tab."""
    from app import app  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    if not user or not user.is_superadmin:
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    level = request.args.get('level', '')
    category = request.args.get('category', '')

    query = HealthLog.query
    if level:
        query = query.filter(HealthLog.level == level)
    if category:
        query = query.filter(HealthLog.category == category)

    pagination = query.order_by(HealthLog.id.desc()).paginate(
        page=page, per_page=min(per_page, 200), error_out=False
    )
    return jsonify({
        'success': True,
        'logs': [l.to_dict() for l in pagination.items],
        'total': pagination.total,
        'page': pagination.page,
        'pages': pagination.pages,
    })


@bp.route('/api/settings/health-logs/clear', methods=['POST'])
@login_required
def clear_health_logs():
    """Delete all health logs."""
    from app import app  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    if not user or not user.is_superadmin:
        return jsonify({'success': False, 'message': 'Forbidden'}), 403
    try:
        deleted = HealthLog.query.delete()
        db.session.commit()
        return jsonify({'success': True, 'message': f'Cleared {deleted} log entries'})
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(exc)}), 500


@bp.route('/api/settings/health-logs/run-check', methods=['POST'])
@login_required
def run_health_check_now():
    """Trigger a manual health-check cycle and return results."""
    from app import (  # deferred: app-level helper, avoids circular import
        _add_health_log, _run_single_health_cycle, app,
    )
    user = db.session.get(Admin, session['admin_id'])
    if not user or not user.is_superadmin:
        return jsonify({'success': False, 'message': 'Forbidden'}), 403
    try:
        results = _run_single_health_cycle()
        summary = {}
        for key, (ok, detail) in results.items():
            summary[key] = {'ok': ok, 'detail': str(detail) if detail else None}
        _add_health_log('info', 'general', 'Manual health check triggered by admin',
                        details=summary, resolved=True)
        return jsonify({'success': True, 'results': summary})
    except Exception as exc:
        return jsonify({'success': False, 'message': str(exc)}), 500


# Compact usage rollups supersede the append-only raw snapshot implementation
# above. Keeping the compatibility name lets the manual refresh endpoint call
# the new collector while rolling upgrades are in progress.
_USAGE_ROLLUP_INTERVAL_MIN = 60
