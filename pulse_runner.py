"""Eve Pulse CLI bridge — resolve panel configs and run health probes.

Invoked by the ``eve`` CLI (setup.sh pulse menu). stdout carries exactly one
JSON document per invocation; human progress goes to stderr so the bash menu
can show it live while still capturing machine-readable output.
"""
import argparse
import json
import os
import sys
from datetime import datetime

os.environ.setdefault('DISABLE_BACKGROUND_THREADS', 'true')

import app as app_module  # noqa: E402
from app import (  # noqa: E402
    PulseResultRecord,
    PulseRun,
    Server,
    app,
    db,
)
from telegram_xray import find_xray_binary  # noqa: E402

# pulse is imported lazily (see _load_pulse) so listing/history work even if
# the probe engine or the xray runtime is unavailable.
pulse = None

DEFAULT_LIMIT = 10
TRAFFIC_CAVEAT = (
    "Full-profile download tests consume real client traffic; prefer configs "
    "whose email contains 'probe' (flagged as is_probe)."
)


def _load_pulse():
    global pulse
    if pulse is None:
        import pulse as pulse_module
        pulse = pulse_module
    return pulse


def _emit(payload):
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write('\n')
    sys.stdout.flush()


def _fail(message, **extra):
    payload = {'error': message}
    payload.update(extra)
    _emit(payload)
    return 2


def _progress(message):
    print(f'[pulse] {message}', file=sys.stderr, flush=True)


def _parse_site_spec(value):
    """Parse ``name=url[::expect]`` into a dict (same grammar as pulse CLI)."""
    expect = None
    if '::' in value:
        value, expect = value.rsplit('::', 1)
    name, sep, url = value.partition('=')
    name = name.strip()
    url = url.strip()
    if not sep or not name or not url:
        raise ValueError(f"invalid site spec {value!r}; expected name=url[::expect]")
    return {'name': name, 'url': url, 'expect_substring': expect or None}


def _load_sites_file(path):
    """Read a sites checklist file: one ``name=url[::expect]`` per line."""
    sites = []
    with open(path, 'r', encoding='utf-8') as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            try:
                sites.append(_parse_site_spec(line))
            except ValueError as exc:
                raise ValueError(f'{path}:{lineno}: {exc}')
    return sites


def _site_checks(specs):
    engine = _load_pulse()
    return [
        engine.SiteCheck(
            name=spec['name'],
            url=spec['url'],
            expect_substring=spec.get('expect_substring') or None,
        )
        for spec in specs
    ]


def _get_server(server_id):
    return Server.query.get(server_id)


def _fetch_server_inbounds(server):
    session_obj, login_err = app_module.get_xui_session(server)
    if not session_obj:
        return None, f'panel login failed: {login_err or "unknown error"}'
    inbounds, fetch_err, _detected = app_module.fetch_inbounds(
        session_obj, server.host, server.panel_type)
    if fetch_err or not inbounds:
        return None, f'failed to fetch inbounds: {fetch_err or "empty response"}'
    return inbounds, None


def _inbound_clients(inbound):
    settings = app_module._json_field(inbound.get('settings'), {})
    clients = settings.get('clients') or []
    return clients if isinstance(clients, list) else []


def cmd_list_servers(args):
    servers = (Server.query.filter_by(enabled=True)
               .order_by(Server.name.asc()).all())
    _emit({
        'servers': [
            {
                'id': srv.id,
                'name': srv.name,
                'host': srv.host,
                'panel_type': srv.panel_type,
            }
            for srv in servers
        ],
    })
    return 0


def cmd_list_inbounds(args):
    server = _get_server(args.server_id)
    if not server:
        return _fail(f'server {args.server_id} not found')
    inbounds, error = _fetch_server_inbounds(server)
    if error:
        return _fail(error, server_id=server.id)
    _emit({
        'server': {'id': server.id, 'name': server.name},
        'inbounds': [
            {
                'id': inb.get('id'),
                'remark': inb.get('remark') or '',
                'protocol': inb.get('protocol') or '',
                'port': inb.get('port'),
                'enabled': bool(inb.get('enable', True)),
                'clients': len(_inbound_clients(inb)),
            }
            for inb in inbounds
        ],
    })
    return 0


def _collect_configs(server, inbounds, inbound_id=None):
    """Build PulseConfig entries for every client of the selected inbounds."""
    engine = _load_pulse()
    configs = []
    skipped = 0
    for inb in inbounds:
        if inbound_id is not None and inb.get('id') != inbound_id:
            continue
        if not inb.get('enable', True):
            continue
        remark = inb.get('remark') or f"inbound-{inb.get('id')}"
        for client in _inbound_clients(inb):
            if client.get('enable') is False:
                continue
            uri = app_module.generate_client_link(client, inb, server.host)
            if not uri:
                skipped += 1
                continue
            email = str(client.get('email') or '')
            label = f'{email} @ {remark}' if email else remark
            configs.append({
                'config': engine.PulseConfig(
                    uri=uri, label=label,
                    server=server.name, inbound=remark),
                'is_probe': 'probe' in email.lower(),
            })
    return configs, skipped


def _result_metrics(result_dict):
    tests = result_dict.get('tests') or {}
    latency = tests.get('latency') or {}
    loss = tests.get('loss') or {}
    download = tests.get('download') or {}
    sites = tests.get('sites') or {}
    failed_sites = [
        entry.get('name') for entry in (sites.get('checks') or [])
        if not entry.get('ok')
    ]
    return {
        'latency_avg_ms': latency.get('avg_ms'),
        'loss_pct': loss.get('loss_pct'),
        'download_mbps': download.get('mbps'),
        'sites': sites.get('checks') or [],
        'failed_sites': failed_sites,
    }


def cmd_run(args):
    server = _get_server(args.server_id)
    if not server:
        return _fail(f'server {args.server_id} not found')

    if not find_xray_binary():
        return _fail(
            'xray runtime not found; install it with: eve --install-xray',
            hint='run: eve --install-xray')

    site_specs = []
    for raw in (args.site or []):
        try:
            site_specs.append(_parse_site_spec(raw))
        except ValueError as exc:
            return _fail(str(exc))
    if args.sites_file:
        if not os.path.isfile(args.sites_file):
            return _fail(f'sites file not found: {args.sites_file}')
        try:
            site_specs.extend(_load_sites_file(args.sites_file))
        except (ValueError, OSError) as exc:
            return _fail(str(exc))

    inbounds, error = _fetch_server_inbounds(server)
    if error:
        return _fail(error, server_id=server.id)

    inbound_id = None if args.all_inbounds else args.inbound_id
    if inbound_id is None and not args.all_inbounds:
        return _fail('pass --inbound-id M or --all-inbounds')

    selected = [inb for inb in inbounds
                if inbound_id is None or inb.get('id') == inbound_id]
    if not selected:
        return _fail(f'inbound {inbound_id} not found on server {server.id}',
                     server_id=server.id)

    engine = _load_pulse()
    configs, skipped = _collect_configs(server, inbounds, inbound_id)
    if not configs:
        return _fail('no client configs could be generated for the selection',
                     server_id=server.id, skipped=skipped)

    total_available = len(configs)
    limit = max(1, args.limit)
    truncated = total_available > limit
    configs = configs[:limit]

    scope = 'server' if inbound_id is None else 'inbound'
    inbound_label = None if inbound_id is None else (
        selected[0].get('remark') or f'inbound-{inbound_id}')

    run = PulseRun(
        server_id=server.id,
        server_name=server.name,
        scope=scope,
        inbound_label=inbound_label,
        profile=args.profile,
        vantage='local',
        status='running',
        triggered_by='cli',
    )
    db.session.add(run)
    db.session.commit()

    profile_factory = engine.full_profile if args.profile == 'full' else engine.quick_profile
    profile = profile_factory(site_checks=_site_checks(site_specs))

    summary = {'healthy': 0, 'degraded': 0, 'down': 0}
    results = []
    try:
        for index, entry in enumerate(configs, 1):
            cfg = entry['config']
            _progress(f'{index}/{len(configs)} probing {cfg.label} ...')
            probe = engine.run_probe(cfg, profile)
            data = probe.to_dict()
            metrics = _result_metrics(data)
            summary[data['verdict']] = summary.get(data['verdict'], 0) + 1
            _progress(f'{index}/{len(configs)} {probe.label}: {probe.verdict}'
                      + (f' ({probe.error})' if probe.error else ''))
            record = PulseResultRecord(
                run_id=run.id,
                config_label=probe.label,
                uri_scheme=probe.scheme,
                verdict=probe.verdict,
                latency_avg_ms=metrics['latency_avg_ms'],
                loss_pct=metrics['loss_pct'],
                download_mbps=metrics['download_mbps'],
                sites_json=json.dumps(metrics['sites'], ensure_ascii=False),
                detail_json=json.dumps(data, ensure_ascii=False),
                is_probe=entry['is_probe'],
                error=probe.error,
            )
            db.session.add(record)
            results.append({
                'label': probe.label,
                'scheme': probe.scheme,
                'verdict': probe.verdict,
                'latency_avg_ms': metrics['latency_avg_ms'],
                'loss_pct': metrics['loss_pct'],
                'download_mbps': metrics['download_mbps'],
                'failed_sites': metrics['failed_sites'],
                'error': probe.error,
                'is_probe': entry['is_probe'],
            })
        run.status = 'done'
        run.finished_at = datetime.utcnow()
        run.summary_json = json.dumps(summary)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        run.status = 'failed'
        run.error = str(exc)
        run.finished_at = datetime.utcnow()
        run.summary_json = json.dumps(summary)
        db.session.commit()
        return _fail(f'probe run failed: {exc}', run_id=run.id)

    warnings = []
    if truncated:
        warnings.append(
            f'only the first {len(configs)} of {total_available} configs were '
            f'probed (--limit {limit}); raise --limit to probe more')
    if skipped:
        warnings.append(f'{skipped} client(s) skipped: no shareable config URI')
    if args.profile == 'full' and any(not r['is_probe'] for r in results):
        warnings.append(TRAFFIC_CAVEAT)

    _emit({
        'run_id': run.id,
        'server': {'id': server.id, 'name': server.name},
        'scope': scope,
        'inbound_label': inbound_label,
        'profile': args.profile,
        'total_available': total_available,
        'probed': len(results),
        'truncated': truncated,
        'summary': summary,
        'results': results,
        'warnings': warnings,
    })
    return 0 if summary.get('down', 0) == 0 else 1


def cmd_history(args):
    query = PulseRun.query.order_by(PulseRun.created_at.desc(), PulseRun.id.desc())
    if args.server_id:
        query = query.filter(PulseRun.server_id == args.server_id)
    runs = query.limit(max(1, args.limit)).all()
    _emit({'runs': [run.to_dict() for run in runs]})
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Eve Pulse config health runner')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('list-servers', help='list enabled servers as JSON')

    p_inbounds = sub.add_parser('list-inbounds', help='list inbounds of a server')
    p_inbounds.add_argument('--server-id', type=int, required=True)

    p_run = sub.add_parser('run', help='probe configs of a server/inbound')
    p_run.add_argument('--server-id', type=int, required=True)
    p_run.add_argument('--inbound-id', type=int, default=None)
    p_run.add_argument('--all-inbounds', action='store_true')
    p_run.add_argument('--limit', type=int, default=DEFAULT_LIMIT)
    p_run.add_argument('--profile', choices=('quick', 'full'), default='quick')
    p_run.add_argument('--site', action='append', default=[],
                       metavar='name=url[::expect]')
    p_run.add_argument('--sites-file', default=None,
                       help='text file with one name=url[::expect] per line')

    p_history = sub.add_parser('history', help='recent pulse runs as JSON')
    p_history.add_argument('--server-id', type=int, default=None)
    p_history.add_argument('--limit', type=int, default=20)

    args = parser.parse_args(argv)

    handlers = {
        'list-servers': cmd_list_servers,
        'list-inbounds': cmd_list_inbounds,
        'run': cmd_run,
        'history': cmd_history,
    }
    with app.app_context():
        db.create_all()
        return handlers[args.command](args)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'[pulse] failed: {exc}', file=sys.stderr)
        _emit({'error': f'pulse runner crashed: {exc}'})
        raise SystemExit(2)
