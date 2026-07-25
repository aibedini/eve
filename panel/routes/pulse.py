"""Eve Pulse web UI + remote-agent API routes (extracted from app.py)."""
import json
import re
import secrets
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import unquote, urlparse

from flask import Blueprint, jsonify, redirect, request, session, url_for

from telegram_xray import build_xray_config_from_uri

from panel.extensions import db
from panel.models import (
    Admin, PulseAgent, PulseRun, PulseTemplate, Server, get_pulse_settings,
)
from panel.routes.common import login_required
from panel.services.subscription import generate_client_link

bp = Blueprint('pulse', __name__)


# ---------------------------------------------------------------------------
# Eve Pulse web UI – health-check dashboard, on-demand runs, scheduling
# ---------------------------------------------------------------------------
def _pulse_runner_module():
    """Lazy import: pulse_runner imports app, so a top-level import would cycle."""
    import pulse_runner
    return pulse_runner


def _pulse_accessible_server(user, server_id):
    """The Server row when the logged-in admin may see it, else None."""
    from app import get_accessible_servers  # deferred: app-level helper, avoids circular import
    server = db.session.get(Server, server_id) if server_id else None
    if not server:
        return None
    accessible_ids = {srv.id for srv in get_accessible_servers(user)}
    return server if server.id in accessible_ids else None


def _pulse_parse_sites_text(text):
    """Parse the sites textarea: one ``name=url[::expect]`` spec per line."""
    pr = _pulse_runner_module()
    specs = []
    for raw_line in (text or '').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        specs.append(pr._parse_site_spec(line))  # raises ValueError on bad lines
    return specs


def _pulse_form_int(value, default, lo=1, hi=1000):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


PULSE_COPY = {
    'en': {
        'xray_missing_title': 'Xray runtime is not installed',
        'xray_missing_help': 'Pulse cannot run local probes until Xray is installed. Install it from Settings → Telegram → Xray Runtime, or run eve --install-xray on the server.',
        'subtitle': 'Build an explicit test plan, see every queued job, and reuse it as a template.',
        'queue': 'Live queue', 'queue_help': 'Active jobs and jobs finished in the last 5 minutes update automatically.',
        'empty_queue': 'The queue is empty.', 'position': 'Position', 'job': 'Job',
        'target': 'Target', 'configs': 'Configs', 'status': 'Status', 'source': 'Source',
        'wizard': 'Create a test plan', 'wizard_help': 'Choose the server, inbound, and exact configs in order. Nothing is selected automatically.',
        'step1': '1. Choose server', 'step2': '2. Choose inbound(s)', 'step3': '3. Choose client(s)',
        'v3_multi_hint': 'v3+ detected: choose one or more inbounds, then choose one client attached to all of them.',
        'legacy_hint': 'Choose one inbound, then select the configs to test.',
        'search_configs': 'Search clients…', 'common_clients': 'clients match every selected inbound',
        'selection_empty': 'Choose an inbound and client to see the final test count.',
        'selected_inbounds': 'inbounds selected', 'common_clients_count': 'common clients',
        'tests_ready': 'tests will be queued for this selection',
        'clear_search': 'Clear search', 'clear_selection': 'Clear selection',
        'no_search_results': 'No matching client was found.',
        'edit': 'Edit', 'edit_template': 'Edit template', 'update_template': 'Update template',
        'update_step': 'Update this plan step', 'cancel_edit': 'Cancel edit',
        'panel_configs': 'Panel clients', 'manual_links': 'Manual links',
        'manual_links_help': 'Paste one VLESS, VMess, Trojan, Shadowsocks, or WireGuard link per line.',
        'manual_links_placeholder': 'vless://…\nvmess://…', 'manual_configs': 'manual configs',
        'choose_server': 'Choose a server…', 'choose_inbound': 'Choose an inbound…',
        'loading': 'Loading…', 'load_error': 'Could not load this selection.',
        'no_configs': 'No enabled, shareable configs were found.', 'select_all': 'Select all',
        'clear': 'Clear', 'add_step': 'Add this selection to the plan',
        'plan': 'Ordered plan', 'plan_empty': 'Add at least one server/inbound/config selection.',
        'move_up': 'Up', 'move_down': 'Down', 'remove': 'Remove',
        'options': '4. Test options', 'profile': 'Test profile', 'quick': 'Quick (latency, loss, sites)',
        'full': 'Full (also consumes traffic for speed tests)', 'vantage': 'Run from',
        'download_size': 'Download sample (MB)', 'upload_size': 'Upload sample (MB)',
        'speed_size_help': 'Defaults: 10 MB download and 2 MB upload. Larger samples improve stability but consume more client traffic.',
        'local': 'This Eve server (local)', 'sites': 'Custom sites (optional)',
        'sites_help': 'One per line: name=url or name=url::expected text',
        'run_now': 'Queue this plan now', 'queued_ok': 'jobs were added to the queue.',
        'template_name': 'Template name', 'save_template': 'Save as template',
        'schedule': 'Run this template automatically', 'interval': 'Every (minutes)',
        'templates': 'Saved templates', 'templates_help': 'Each template keeps the exact target order and config selection.',
        'no_templates': 'No template has been saved yet.', 'run_template': 'Queue now',
        'delete': 'Delete', 'scheduled': 'Scheduled', 'manual': 'Manual', 'steps': 'steps',
        'recent': 'Recent completed/failed runs', 'time': 'Time', 'server': 'Server',
        'scope': 'Selection', 'healthy': 'Healthy', 'degraded': 'Degraded', 'down': 'Down',
        'duration': 'Duration', 'details': 'Click a row for details.', 'never': 'Never',
        'agent': 'Agent', 'agents': 'External test agents', 'agents_help': 'Optional: run tests from another country.',
        'agent_name': 'New agent name', 'create_agent': 'Create agent', 'last_seen': 'Last seen',
        'enabled': 'Enabled', 'disabled': 'Disabled', 'created': 'Agent created. Save this one-time command.',
        'confirm_delete': 'Delete this item?', 'queued': 'Queued', 'running': 'Running',
        'done': 'Done', 'failed': 'Failed', 'web': 'Panel', 'template': 'Template',
        'schedule_source': 'Schedule', 'seconds': 'sec', 'config': 'Config', 'result': 'Result',
        'latency': 'Latency', 'loss': 'Loss', 'speed': 'Speed', 'error': 'Error',
        'save_ok': 'Template saved.', 'request_error': 'The request failed.',
    },
    'fa': {
        'xray_missing_title': 'هسته Xray نصب نیست',
        'xray_missing_help': 'Pulse تا قبل از نصب Xray نمی‌تواند تست محلی اجرا کند. آن را از تنظیمات ← تلگرام ← Xray Runtime نصب کنید یا روی سرور دستور eve --install-xray را اجرا کنید.',
        'subtitle': 'برنامه تست را دقیق بسازید، تمام کارهای صف را ببینید و آن را به‌صورت تمپلیت دوباره اجرا کنید.',
        'queue': 'صف زنده', 'queue_help': 'کارهای فعال و اجراهای تمام‌شده در ۵ دقیقه اخیر خودکار به‌روز می‌شوند.',
        'empty_queue': 'صف خالی است.', 'position': 'ردیف', 'job': 'کار',
        'target': 'مقصد', 'configs': 'کانفیگ‌ها', 'status': 'وضعیت', 'source': 'منشأ',
        'wizard': 'ساخت برنامه تست', 'wizard_help': 'سرور، اینباند و کانفیگ‌های دقیق را به‌ترتیب انتخاب کنید؛ چیزی خودکار انتخاب نمی‌شود.',
        'step1': '۱. انتخاب سرور', 'step2': '۲. انتخاب اینباندها', 'step3': '۳. انتخاب کلاینت',
        'v3_multi_hint': 'نسخه ۳ یا جدیدتر تشخیص داده شد: چند اینباند را انتخاب کنید، سپس یک کاربر مشترک میان همه را برگزینید.',
        'legacy_hint': 'یک اینباند را انتخاب کنید، سپس کانفیگ‌های موردنظر را برگزینید.',
        'search_configs': 'جست‌وجوی کلاینت…', 'common_clients': 'کلاینت با همه اینباندهای انتخاب‌شده مطابقت دارد',
        'selection_empty': 'برای دیدن تعداد تست نهایی، اینباند و کلاینت را انتخاب کنید.',
        'selected_inbounds': 'اینباند انتخاب‌شده', 'common_clients_count': 'کلاینت مشترک',
        'tests_ready': 'تست برای این انتخاب وارد صف می‌شود',
        'clear_search': 'پاک‌کردن جست‌وجو', 'clear_selection': 'پاک‌کردن انتخاب',
        'no_search_results': 'کلاینتی مطابق این جست‌وجو پیدا نشد.',
        'edit': 'ویرایش', 'edit_template': 'ویرایش تمپلیت', 'update_template': 'به‌روزرسانی تمپلیت',
        'update_step': 'به‌روزرسانی این مرحله', 'cancel_edit': 'لغو ویرایش',
        'panel_configs': 'کلاینت‌های پنل', 'manual_links': 'لینک‌های دستی',
        'manual_links_help': 'در هر خط یک لینک VLESS، VMess، Trojan، Shadowsocks یا WireGuard وارد کنید.',
        'manual_links_placeholder': 'vless://…\nvmess://…', 'manual_configs': 'کانفیگ دستی',
        'choose_server': 'یک سرور انتخاب کنید…', 'choose_inbound': 'یک اینباند انتخاب کنید…',
        'loading': 'در حال دریافت…', 'load_error': 'دریافت این انتخاب ناموفق بود.',
        'no_configs': 'کانفیگ فعال و قابل اشتراکی پیدا نشد.', 'select_all': 'انتخاب همه',
        'clear': 'پاک‌کردن', 'add_step': 'افزودن این انتخاب به برنامه',
        'plan': 'ترتیب اجرای برنامه', 'plan_empty': 'حداقل یک انتخاب سرور/اینباند/کانفیگ اضافه کنید.',
        'move_up': 'بالا', 'move_down': 'پایین', 'remove': 'حذف',
        'options': '۴. تنظیمات تست', 'profile': 'پروفایل تست', 'quick': 'سریع (تأخیر، افت و سایت‌ها)',
        'full': 'کامل (تست سرعت نیز ترافیک مصرف می‌کند)', 'vantage': 'محل اجرا',
        'download_size': 'حجم نمونه دانلود (MB)', 'upload_size': 'حجم نمونه آپلود (MB)',
        'speed_size_help': 'پیش‌فرض: دانلود ۱۰ MB و آپلود ۲ MB. حجم بیشتر نتیجه را پایدارتر می‌کند اما ترافیک بیشتری مصرف می‌شود.',
        'local': 'همین سرور Eve (محلی)', 'sites': 'سایت‌های سفارشی (اختیاری)',
        'sites_help': 'هر خط: name=url یا name=url::متن مورد انتظار',
        'run_now': 'افزودن این برنامه به صف', 'queued_ok': 'کار به صف اضافه شد.',
        'template_name': 'نام تمپلیت', 'save_template': 'ذخیره به‌عنوان تمپلیت',
        'schedule': 'این تمپلیت خودکار اجرا شود', 'interval': 'هر چند دقیقه',
        'templates': 'تمپلیت‌های ذخیره‌شده', 'templates_help': 'هر تمپلیت ترتیب مقصدها و کانفیگ‌های انتخاب‌شده را نگه می‌دارد.',
        'no_templates': 'هنوز تمپلیتی ذخیره نشده است.', 'run_template': 'افزودن به صف',
        'delete': 'حذف', 'scheduled': 'زمان‌بندی‌شده', 'manual': 'دستی', 'steps': 'مرحله',
        'recent': 'اجراهای تمام‌شده یا ناموفق اخیر', 'time': 'زمان', 'server': 'سرور',
        'scope': 'انتخاب', 'healthy': 'سالم', 'degraded': 'ضعیف', 'down': 'قطع',
        'duration': 'مدت', 'details': 'برای دیدن جزئیات روی ردیف کلیک کنید.', 'never': 'هرگز',
        'agent': 'ایجنت', 'agents': 'ایجنت‌های تست خارج', 'agents_help': 'اختیاری: تست را از کشور دیگری اجرا کنید.',
        'agent_name': 'نام ایجنت جدید', 'create_agent': 'ساخت ایجنت', 'last_seen': 'آخرین اتصال',
        'enabled': 'فعال', 'disabled': 'غیرفعال', 'created': 'ایجنت ساخته شد؛ این دستور یک‌بارمصرف را ذخیره کنید.',
        'confirm_delete': 'این مورد حذف شود؟', 'queued': 'در صف', 'running': 'در حال اجرا',
        'done': 'تمام‌شده', 'failed': 'ناموفق', 'web': 'پنل', 'template': 'تمپلیت',
        'schedule_source': 'زمان‌بندی', 'seconds': 'ثانیه', 'config': 'کانفیگ', 'result': 'نتیجه',
        'latency': 'تأخیر', 'loss': 'افت', 'speed': 'سرعت', 'error': 'خطا',
        'save_ok': 'تمپلیت ذخیره شد.', 'request_error': 'انجام درخواست ناموفق بود.',
    },
}


def _pulse_queue_snapshot(limit=100, now=None):
    """Return active jobs plus a short terminal-state visibility window."""
    limit = max(1, min(int(limit or 100), 200))
    active = (PulseRun.query
              .filter(PulseRun.status.in_(('queued', 'running')))
              .order_by(PulseRun.created_at.asc(), PulseRun.id.asc())
              .limit(limit).all())
    remaining = limit - len(active)
    recent = []
    if remaining > 0:
        cutoff = (now or datetime.utcnow()) - timedelta(minutes=5)
        recent = (PulseRun.query
                  .filter(PulseRun.status.in_(('done', 'failed')),
                          PulseRun.created_at >= cutoff)
                  .order_by(PulseRun.created_at.desc(), PulseRun.id.desc())
                  .limit(remaining).all())
    queue_position = 0
    for run in active + recent:
        if run.status == 'queued':
            queue_position += 1
            run.queue_position = queue_position
        else:
            run.queue_position = None
    return active + recent




def _pulse_run_comparison(run):
    """Latest done run for the same server from a DIFFERENT vantage.

    Used to show local-vs-remote (ایران/خارج) verdicts side-by-side on the
    run detail view. Returns None when no other-vantage run exists.
    """
    if not run.server_id:
        return None
    other = (PulseRun.query
             .filter(PulseRun.server_id == run.server_id,
                     PulseRun.id != run.id,
                     PulseRun.status == 'done',
                     PulseRun.vantage != (run.vantage or 'local'))
             .order_by(PulseRun.created_at.desc(), PulseRun.id.desc())
             .first())
    if other is None:
        return None
    configs = {}
    for rec in other.results:
        configs[rec.config_label] = {
            'verdict': rec.verdict,
            'latency_avg_ms': rec.latency_avg_ms,
            'loss_pct': rec.loss_pct,
        }
    return {
        'run_id': other.id,
        'vantage': other.vantage or 'local',
        'created_at': other.created_at.isoformat() + 'Z' if other.created_at else None,
        'configs': configs,
    }


@bp.route('/pulse/run/<int:run_id>')
@login_required
def pulse_run_detail(run_id):
    run = db.session.get(PulseRun, run_id)
    if run is None:
        return jsonify({'ok': False, 'error': 'run not found'}), 404
    payload = run.to_dict()
    payload['ok'] = True
    payload['results'] = [rec.to_dict() for rec in run.results]
    payload['comparison'] = _pulse_run_comparison(run)
    return jsonify(payload)


@bp.route('/pulse/run', methods=['POST'])
@login_required
def pulse_run_create():
    user = db.session.get(Admin, session['admin_id'])
    data = request.get_json(silent=True) or request.form
    wants_json = (request.is_json
                  or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                  or 'application/json' in (request.headers.get('Accept') or ''))

    def _error(message, status=400):
        if wants_json:
            return jsonify({'ok': False, 'error': message}), status
        return redirect(url_for('pages.pulse_page'))

    server = _pulse_accessible_server(user, _pulse_form_int(data.get('server_id'), 0, lo=0))
    if server is None:
        return _error('سرور نامعتبر است')

    inbound_id = None
    inbound_raw = str(data.get('inbound_id') or '').strip()
    if inbound_raw and inbound_raw != 'all':
        try:
            inbound_id = int(inbound_raw)
        except ValueError:
            return _error('اینباند نامعتبر است')

    profile = str(data.get('profile') or 'quick').strip()
    if profile not in ('quick', 'full'):
        profile = 'quick'
    limit = _pulse_form_int(data.get('limit'), 10, lo=1, hi=200)
    config_ids = data.get('config_ids') if hasattr(data, 'get') else None
    if not isinstance(config_ids, list):
        config_ids = []
    config_ids = [str(value).strip() for value in config_ids if str(value).strip()]
    download_bytes = _pulse_form_int(
        data.get('download_mb'), 10, lo=1, hi=200) * 1_000_000
    upload_bytes = _pulse_form_int(
        data.get('upload_mb'), 2, lo=1, hi=200) * 1_000_000

    vantage = str(data.get('vantage') or 'local').strip()
    if vantage.startswith('agent:'):
        agent_name = vantage.split(':', 1)[1].strip()
        agent_row = PulseAgent.query.filter_by(name=agent_name, enabled=True).first()
        if agent_row is None:
            return _error('ایجنت نامعتبر یا غیرفعال است')
        vantage = f'agent:{agent_name}'
    elif vantage != 'local':
        return _error('دیدگاه (vantage) نامعتبر است')

    try:
        site_specs = _pulse_parse_sites_text(data.get('sites'))
    except ValueError as exc:
        return _error(str(exc))

    run = PulseRun(
        server_id=server.id,
        server_name=server.name,
        scope='inbound' if inbound_id is not None else 'server',
        profile=profile,
        vantage=vantage,
        status='queued',
        triggered_by='web',
        params_json=json.dumps({
            'inbound_id': inbound_id,
            'limit': limit,
            'config_ids': config_ids,
            'download_bytes': download_bytes,
            'upload_bytes': upload_bytes,
            'sites': site_specs,
        }, ensure_ascii=False),
    )
    db.session.add(run)
    db.session.commit()
    if wants_json:
        return jsonify({'ok': True, 'run_id': run.id})
    return redirect(url_for('pages.pulse_page'))


def _pulse_normalize_manual_configs(raw_configs):
    if isinstance(raw_configs, str):
        raw_configs = raw_configs.splitlines()
    if not isinstance(raw_configs, list):
        raw_configs = []
    configs = []
    for line_number, raw in enumerate(raw_configs, 1):
        supplied_label = ''
        if isinstance(raw, dict):
            uri = str(raw.get('uri') or '').strip()
            supplied_label = str(raw.get('label') or '').strip()
        else:
            uri = str(raw or '').strip()
        if not uri:
            continue
        if len(uri) > 16_384:
            raise ValueError('a manual config link is too long')
        try:
            build_xray_config_from_uri(uri, 12_080)
        except Exception as exc:
            raise ValueError(
                f'invalid manual config on line {line_number}: {exc}') from exc
        fragment = unquote(urlparse(uri).fragment or '').strip()
        configs.append({
            'uri': uri,
            'label': (supplied_label or fragment or
                      f'Manual config {len(configs) + 1}')[:160],
        })
        if len(configs) > 200:
            raise ValueError('too many manual configs (maximum 200)')
    if not configs:
        raise ValueError('enter at least one manual config link')
    return configs


def _pulse_normalize_targets(user, raw_targets):
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError('test plan must contain at least one target')
    if len(raw_targets) > 100:
        raise ValueError('test plan is too large')
    targets = []
    for raw in raw_targets:
        if not isinstance(raw, dict):
            raise ValueError('invalid test-plan target')
        config_source = str(raw.get('config_source') or 'panel').strip().lower()
        if config_source == 'manual':
            manual_configs = _pulse_normalize_manual_configs(
                raw.get('manual_configs'))
            targets.append({
                'server_id': None,
                'server_name': 'Manual links',
                'inbound_id': None,
                'inbound_ids': [],
                'inbound_label': 'Manual links',
                'inbound_labels': [],
                'config_ids': [],
                'config_labels': [entry['label'] for entry in manual_configs],
                'config_source': 'manual',
                'manual_configs': manual_configs,
                'v3_mode': False,
            })
            continue
        if config_source != 'panel':
            raise ValueError('invalid config source')
        server = _pulse_accessible_server(
            user, _pulse_form_int(raw.get('server_id'), 0, lo=0))
        if server is None:
            raise ValueError('invalid server in test plan')
        raw_inbound_ids = raw.get('inbound_ids')
        if not isinstance(raw_inbound_ids, list):
            raw_inbound_ids = [raw.get('inbound_id')]
        inbound_ids = []
        for value in raw_inbound_ids:
            inbound_id = _pulse_form_int(value, 0, lo=0) or None
            if inbound_id is not None and inbound_id not in inbound_ids:
                inbound_ids.append(inbound_id)
        if not inbound_ids:
            raise ValueError('each test-plan target needs an inbound')
        config_ids = raw.get('config_ids')
        if not isinstance(config_ids, list):
            config_ids = []
        config_ids = list(dict.fromkeys(
            str(value).strip() for value in config_ids if str(value).strip()))
        if not config_ids:
            raise ValueError('select at least one config for every target')
        if len(config_ids) > 200:
            raise ValueError('too many configs in one target')
        targets.append({
            'server_id': server.id,
            'server_name': server.name,
            'inbound_id': inbound_ids[0] if len(inbound_ids) == 1 else None,
            'inbound_ids': inbound_ids,
            'inbound_label': str(
                raw.get('inbound_label') or ', '.join(
                    f'inbound-{value}' for value in inbound_ids))[:255],
            'inbound_labels': [str(value)[:255] for value in
                               (raw.get('inbound_labels') or [])],
            'config_ids': config_ids,
            'config_labels': [str(value)[:255] for value in (raw.get('config_labels') or [])],
            'config_source': 'panel',
            'manual_configs': [],
            'v3_mode': bool(raw.get('v3_mode')),
        })
    return targets


def _pulse_template_values(user, data):
    name = str(data.get('name') or '').strip()
    if not name or len(name) > 120:
        raise ValueError('template name is required')
    targets = _pulse_normalize_targets(user, data.get('targets'))
    sites = _pulse_parse_sites_text(data.get('sites'))
    vantage = str(data.get('vantage') or 'local').strip()
    if vantage.startswith('agent:'):
        agent_name = vantage.split(':', 1)[1]
        if PulseAgent.query.filter_by(name=agent_name, enabled=True).first() is None:
            raise ValueError('invalid or disabled agent')
    elif vantage != 'local':
        raise ValueError('invalid vantage')
    return {
        'name': name,
        'targets_json': json.dumps(targets, ensure_ascii=False),
        'profile': (str(data.get('profile') or 'quick')
                    if data.get('profile') in ('quick', 'full') else 'quick'),
        'vantage': vantage,
        'sites_json': json.dumps(sites, ensure_ascii=False) if sites else None,
        'download_bytes': _pulse_form_int(
            data.get('download_mb'), 10, lo=1, hi=200) * 1_000_000,
        'upload_bytes': _pulse_form_int(
            data.get('upload_mb'), 2, lo=1, hi=200) * 1_000_000,
        'schedule_enabled': bool(data.get('schedule_enabled')),
        'interval_minutes': _pulse_form_int(
            data.get('interval_minutes'), 60, lo=5, hi=1440),
    }


@bp.route('/pulse/plan/run', methods=['POST'])
@login_required
def pulse_plan_run():
    from app import _pulse_enqueue_targets  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    data = request.get_json(silent=True) or {}
    try:
        targets = _pulse_normalize_targets(user, data.get('targets'))
        sites = _pulse_parse_sites_text(data.get('sites'))
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    vantage = str(data.get('vantage') or 'local').strip()
    if vantage.startswith('agent:'):
        name = vantage.split(':', 1)[1]
        if PulseAgent.query.filter_by(name=name, enabled=True).first() is None:
            return jsonify({'ok': False, 'error': 'invalid or disabled agent'}), 400
    elif vantage != 'local':
        return jsonify({'ok': False, 'error': 'invalid vantage'}), 400
    run_ids = _pulse_enqueue_targets(
        targets, profile=str(data.get('profile') or 'quick'), vantage=vantage,
        sites=sites, triggered_by='web',
        download_bytes=_pulse_form_int(data.get('download_mb'), 10, lo=1, hi=200) * 1_000_000,
        upload_bytes=_pulse_form_int(data.get('upload_mb'), 2, lo=1, hi=200) * 1_000_000)
    return jsonify({'ok': True, 'run_ids': run_ids, 'queued': len(run_ids)})


@bp.route('/pulse/templates', methods=['POST'])
@login_required
def pulse_template_create():
    user = db.session.get(Admin, session['admin_id'])
    data = request.get_json(silent=True) or {}
    try:
        values = _pulse_template_values(user, data)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    row = PulseTemplate(**values)
    db.session.add(row)
    db.session.commit()
    return jsonify({'ok': True, 'template': row.to_dict()})


@bp.route('/pulse/templates/<int:template_id>', methods=['GET', 'PUT'])
@login_required
def pulse_template_detail(template_id):
    row = db.session.get(PulseTemplate, template_id)
    if row is None:
        return jsonify({'ok': False, 'error': 'template not found'}), 404
    if request.method == 'GET':
        return jsonify({'ok': True, 'template': row.to_dict()})
    user = db.session.get(Admin, session['admin_id'])
    try:
        values = _pulse_template_values(user, request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    for key, value in values.items():
        setattr(row, key, value)
    db.session.commit()
    return jsonify({'ok': True, 'template': row.to_dict()})


@bp.route('/pulse/templates/<int:template_id>/run', methods=['POST'])
@login_required
def pulse_template_run(template_id):
    from app import _pulse_enqueue_targets  # deferred: app-level helper, avoids circular import
    row = db.session.get(PulseTemplate, template_id)
    if row is None:
        return jsonify({'ok': False, 'error': 'template not found'}), 404
    run_ids = _pulse_enqueue_targets(
        row.targets(), profile=row.profile, vantage=row.vantage, sites=row.sites(),
        triggered_by='template', template_name=row.name,
        download_bytes=row.download_bytes or 10_000_000,
        upload_bytes=row.upload_bytes or 2_000_000)
    return jsonify({'ok': True, 'run_ids': run_ids, 'queued': len(run_ids)})


@bp.route('/pulse/templates/<int:template_id>/delete', methods=['POST'])
@login_required
def pulse_template_delete(template_id):
    row = db.session.get(PulseTemplate, template_id)
    if row is not None:
        db.session.delete(row)
        db.session.commit()
    return jsonify({'ok': True})


@bp.route('/pulse/queue')
@login_required
def pulse_queue_status():
    runs = _pulse_queue_snapshot()
    payload = []
    for run in runs:
        item = run.to_dict()
        item['position'] = run.queue_position
        item['recent'] = run.status in ('done', 'failed')
        payload.append(item)
    return jsonify({'ok': True, 'runs': payload})


@bp.route('/pulse/settings', methods=['POST'])
@login_required
def pulse_settings_save():
    data = request.form
    wants_json = (request.is_json
                  or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                  or 'application/json' in (request.headers.get('Accept') or ''))

    def _error(message, status=400):
        if wants_json:
            return jsonify({'ok': False, 'error': message}), status
        return redirect(url_for('pages.pulse_page'))

    try:
        site_specs = _pulse_parse_sites_text(data.get('sites'))
    except ValueError as exc:
        return _error(str(exc))

    settings = get_pulse_settings()
    settings.enabled = bool(data.get('enabled'))
    settings.interval_minutes = _pulse_form_int(
        data.get('interval_minutes'), 60, lo=5, hi=24 * 60)

    server_raw = str(data.get('server_id') or '').strip()
    if server_raw and server_raw != 'all':
        settings.server_id = _pulse_form_int(server_raw, 0, lo=0) or None
    else:
        settings.server_id = None

    inbound_raw = str(data.get('inbound_id') or '').strip()
    if settings.server_id and inbound_raw and inbound_raw != 'all':
        try:
            settings.inbound_id = int(inbound_raw)
        except ValueError:
            settings.inbound_id = None
    else:
        settings.inbound_id = None

    profile = str(data.get('profile') or 'quick').strip()
    settings.profile = profile if profile in ('quick', 'full') else 'quick'
    settings.probe_limit = _pulse_form_int(data.get('limit'), 10, lo=1, hi=200)

    settings.sites_json = json.dumps(site_specs, ensure_ascii=False) if site_specs else None

    settings.alert_on_down = bool(data.get('alert_on_down'))
    settings.alert_on_degraded = bool(data.get('alert_on_degraded'))
    db.session.commit()
    if wants_json:
        return jsonify({'ok': True, 'settings': settings.to_dict()})
    return redirect(url_for('pages.pulse_page'))


@bp.route('/pulse/servers/<int:server_id>/inbounds')
@login_required
def pulse_server_inbounds(server_id):
    from app import get_xui_session, server_is_v3  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    server = _pulse_accessible_server(user, server_id)
    if server is None:
        return jsonify({'ok': False, 'error': 'server not found'}), 404
    pr = _pulse_runner_module()
    inbounds, error = pr._fetch_server_inbounds(server)
    if error:
        return jsonify({'ok': False, 'error': error}), 502
    panel_session, panel_error = get_xui_session(server)
    is_v3 = bool(panel_session and not panel_error
                 and server_is_v3(server, panel_session))
    return jsonify({
        'ok': True,
        'server': {'id': server.id, 'name': server.name},
        'is_v3': is_v3,
        'inbounds': [
            {
                'id': inb.get('id'),
                'remark': inb.get('remark') or '',
                'protocol': inb.get('protocol') or '',
                'port': inb.get('port'),
                'enabled': bool(inb.get('enable', True)),
                'clients': len(pr._inbound_clients(inb)),
            }
            for inb in inbounds
        ],
    })


@bp.route('/pulse/servers/<int:server_id>/configs')
@login_required
def pulse_v3_common_configs(server_id):
    """Return v3 clients attached to every requested inbound, without URIs."""
    from app import (  # deferred: app-level helper, avoids circular import
        _v3_client_rows, _v3_get, get_xui_session, server_is_v3,
    )
    user = db.session.get(Admin, session['admin_id'])
    server = _pulse_accessible_server(user, server_id)
    if server is None:
        return jsonify({'ok': False, 'error': 'server not found'}), 404
    inbound_ids = []
    for raw in request.args.getlist('inbound_id'):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in inbound_ids:
            inbound_ids.append(value)
    if not inbound_ids:
        return jsonify({'ok': False, 'error': 'select at least one inbound'}), 400
    panel_session, panel_error = get_xui_session(server)
    if not panel_session or panel_error:
        return jsonify({'ok': False, 'error': panel_error or 'panel login failed'}), 502
    if not server_is_v3(server, panel_session):
        return jsonify({'ok': False, 'error': 'server is not v3+'}), 400
    ok, payload, api_error = _v3_get(
        server, panel_session, '/panel/api/clients/list')
    if not ok:
        return jsonify({'ok': False, 'error': api_error or 'failed to fetch clients'}), 502
    required = set(inbound_ids)
    configs = []
    for client in _v3_client_rows(payload):
        if client.get('enable') is False:
            continue
        raw_ids = client.get('inboundIds')
        if raw_ids is None:
            raw_ids = client.get('inbound_ids')
        assigned = set()
        for raw in raw_ids if isinstance(raw_ids, list) else []:
            try:
                assigned.add(int(raw))
            except (TypeError, ValueError):
                continue
        if not required.issubset(assigned):
            continue
        key = str(client.get('id') or client.get('email') or '').strip()
        if not key:
            continue
        email = str(client.get('email') or '').strip()
        configs.append({
            'id': key,
            'label': email or key,
            'is_probe': 'probe' in email.lower(),
            'inbound_count': len(assigned),
        })
    configs.sort(key=lambda item: item['label'].lower())
    return jsonify({
        'ok': True,
        'server': {'id': server.id, 'name': server.name},
        'inbound_ids': inbound_ids,
        'configs': configs,
    })


@bp.route('/pulse/servers/<int:server_id>/inbounds/<int:inbound_id>/configs')
@login_required
def pulse_inbound_configs(server_id, inbound_id):
    """List exact selectable client configs; never return their share URIs."""
    user = db.session.get(Admin, session['admin_id'])
    server = _pulse_accessible_server(user, server_id)
    if server is None:
        return jsonify({'ok': False, 'error': 'server not found'}), 404
    pr = _pulse_runner_module()
    inbounds, error = pr._fetch_server_inbounds(server)
    if error:
        return jsonify({'ok': False, 'error': error}), 502
    inbound = next((row for row in inbounds
                    if str(row.get('id')) == str(inbound_id)), None)
    if inbound is None:
        return jsonify({'ok': False, 'error': 'inbound not found'}), 404
    remark = inbound.get('remark') or f'inbound-{inbound_id}'
    configs = []
    for client in pr._inbound_clients(inbound):
        if client.get('enable') is False:
            continue
        key = pr._client_key(client)
        if not key:
            continue
        if not generate_client_link(client, inbound, server.host):
            continue
        email = str(client.get('email') or '').strip()
        configs.append({
            'id': key,
            'label': email or key,
            'is_probe': 'probe' in email.lower(),
        })
    return jsonify({
        'ok': True,
        'server': {'id': server.id, 'name': server.name},
        'inbound': {'id': inbound_id, 'label': remark},
        'configs': configs,
    })


# ---------------------------------------------------------------------------
# Eve Pulse remote agents – bearer-token pull/push API + admin CRUD
# ---------------------------------------------------------------------------
def _pulse_agent_required(view):
    """Bearer-token auth for the agent API (no session auth involved).

    On success the agent row is passed as the first view argument and its
    last_seen_at / last_ip are refreshed.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization') or ''
        token = auth[7:].strip() if auth.startswith('Bearer ') else ''
        agent = PulseAgent.query.filter_by(token=token).first() if token else None
        if agent is None or not agent.enabled:
            return jsonify({'ok': False, 'error': 'invalid agent token'}), 401
        agent.last_seen_at = datetime.utcnow()
        agent.last_ip = request.remote_addr
        db.session.commit()
        return view(agent, *args, **kwargs)
    return wrapper


@bp.route('/api/pulse/agent/tasks')
@_pulse_agent_required
def pulse_agent_tasks(agent):
    """Claim the oldest queued remote run for this agent (queued → running)."""
    name = str(request.args.get('agent') or '').strip()
    if name != agent.name:
        return jsonify({'ok': False, 'error': 'agent name mismatch'}), 403
    run = (PulseRun.query
           .filter(PulseRun.status == 'queued',
                   PulseRun.vantage.in_([f'agent:{agent.name}', 'agent:any']))
           .order_by(PulseRun.created_at.asc(), PulseRun.id.asc())
           .first())
    if run is None:
        return jsonify({'ok': True, 'run_id': None})
    pr = _pulse_runner_module()
    try:
        task = pr.build_agent_task(run)
    except Exception as exc:
        db.session.rollback()
        run.status = 'failed'
        run.error = str(exc)
        run.finished_at = datetime.utcnow()
        db.session.commit()
        return jsonify({'ok': False, 'error': f'failed to prepare run: {exc}'}), 502
    task['ok'] = True
    return jsonify(task)


@bp.route('/api/pulse/agent/report', methods=['POST'])
@_pulse_agent_required
def pulse_agent_report(agent):
    """Accept the ProbeResult dicts for a claimed run and finalize it."""
    from app import _pulse_maybe_alert, app  # deferred: app-level helper, avoids circular import
    payload = request.get_json(silent=True) or {}
    run_id = payload.get('run_id')
    run = db.session.get(PulseRun, run_id) if run_id else None
    if run is None or run.vantage not in (f'agent:{agent.name}', 'agent:any'):
        return jsonify({'ok': False, 'error': 'run not found'}), 404
    if run.status != 'running':
        return jsonify({'ok': False,
                        'error': f'run is not running (status={run.status})'}), 409
    pr = _pulse_runner_module()
    try:
        summary, _results = pr.persist_agent_report(run, payload.get('results'))
    except Exception as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc)}), 500
    try:
        _pulse_maybe_alert(run)
    except Exception as exc:
        app.logger.warning(f'[pulse] telegram alert failed: {exc}')
    return jsonify({'ok': True, 'run_id': run.id, 'summary': summary})


@bp.route('/pulse/agents', methods=['POST'])
@login_required
def pulse_agent_create():
    data = request.get_json(silent=True) or request.form
    wants_json = (request.is_json
                  or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                  or 'application/json' in (request.headers.get('Accept') or ''))

    def _error(message, status=400):
        if wants_json:
            return jsonify({'ok': False, 'error': message}), status
        return redirect(url_for('pages.pulse_page'))

    name = str(data.get('name') or '').strip()
    if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$', name):
        return _error('نام ایجنت نامعتبر است (حروف، عدد، خط تیره؛ حداقل ۲ کاراکتر)')
    if PulseAgent.query.filter_by(name=name).first() is not None:
        return _error('ایجنتی با این نام از قبل وجود دارد', status=409)
    agent = PulseAgent(name=name, token=secrets.token_hex(16))
    db.session.add(agent)
    db.session.commit()
    if wants_json:
        # the token is returned exactly once, at creation time
        return jsonify({'ok': True, 'agent': agent.to_dict(include_token=True)})
    return redirect(url_for('pages.pulse_page'))


@bp.route('/pulse/agents/<int:agent_id>/delete', methods=['POST'])
@login_required
def pulse_agent_delete(agent_id):
    agent = db.session.get(PulseAgent, agent_id)
    if agent is not None:
        db.session.delete(agent)
        db.session.commit()
    wants_json = (request.is_json
                  or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                  or 'application/json' in (request.headers.get('Accept') or ''))
    if wants_json:
        return jsonify({'ok': True})
    return redirect(url_for('pages.pulse_page'))
