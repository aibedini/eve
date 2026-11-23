"""Persist dashboard submissions and execute them through the existing client routes."""
import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote

from flask import g, jsonify, request, session
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from werkzeug.exceptions import HTTPException

from panel.extensions import db
from panel.models import Admin, Server
from panel.models.finance import ClientMutationJob as Job
from panel.adapters import xui
from panel.core.advisory_lock import resource_lock
from panel.services import xui_compat

_WAKE = threading.Event()
_ACTIONS = {'add': 'add_client', 'edit': 'edit_client', 'renew': 'renew_client'}
_PARALLELISM = 10


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _scope(email):
    # v3 strips spaces in client names. Include both names of a rename in the queue.
    return hashlib.sha256(''.join(email.lower().split()).encode()).hexdigest()


def enqueue_dashboard_mutation(action):
    """Opt in from the dashboard. Existing API and Telegram callers stay synchronous."""
    def decorate(view):
        @wraps(view)
        def wrapped(server_id, inbound_id, email=None, **kwargs):
            key = request.headers.get('X-Eve-Client-Job')
            if not key:
                args = (server_id, inbound_id) if email is None else (server_id, inbound_id, email)
                return view(*args, **kwargs)
            payload = request.get_json(silent=True)
            if not re.fullmatch(r'[a-zA-Z0-9-]{16,64}', key) or not isinstance(payload, dict):
                return jsonify(success=False, error='Invalid client operation'), 400
            email = email if email is not None else payload.get('email')
            target = payload.get('new_email', email) if action == 'edit' else email
            if any(not isinstance(name, str) or not 1 <= len(name.strip()) <= 255
                   for name in (email, target)):
                return jsonify(success=False, error='Invalid account name'), 400
            membership = payload.get('membership') if action == 'edit' else None
            if membership is not None:
                targets = membership.get('inbound_ids') if isinstance(membership, dict) else None
                if (not isinstance(targets, list) or not targets or len(targets) > 100
                        or any(type(item) is not int or item <= 0 for item in targets)
                        or membership.get('mode') != 'set'):
                    return jsonify(success=False, error='Invalid inbound selection'), 400
            admin_id = session['admin_id']
            admin = db.session.get(Admin, admin_id)
            server = db.session.get(Server, server_id)
            if server is None:
                return jsonify(success=False, error='Server not found'), 404
            serialized = json.dumps({
                'inbound_id': inbound_id, 'payload': payload, 'base_url': request.url_root,
                'remote_addr': request.remote_addr,
                'server_identity': [server.host, str(server.created_at)],
            }, sort_keys=True, separators=(',', ':'))
            if len(serialized.encode()) > 32768:
                return jsonify(success=False, error='Client operation is too large'), 413
            fingerprint = hashlib.sha256(
                json.dumps([action, server_id, inbound_id, email, payload], sort_keys=True).encode()
            ).hexdigest()
            job = Job(public_id=key, admin_id=admin_id, admin_created_at=admin.created_at,
                      server_id=server_id, action=action,
                      scope=_scope(email), target_scope=_scope(target), email=email.strip(),
                      state='queued', ready_at=_now(), request_data=serialized, request_hash=fingerprint)
            db.session.add(job)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                job = Job.query.filter_by(admin_id=admin_id, public_id=key).first()
                if job is None or job.admin_created_at != admin.created_at or job.request_hash != fingerprint:
                    return jsonify(success=False, error='Client operation ID already used'), 409
            _WAKE.set()
            return jsonify(success=True, job_id=key, state=job.state), 202
        return wrapped
    return decorate


def job_status(admin_id, job_id):
    admin = db.session.get(Admin, admin_id)
    job = Job.query.filter_by(admin_id=admin_id, admin_created_at=admin.created_at,
                             public_id=job_id).first_or_404()
    return {
        'state': job.state, 'job_id': job.public_id,
        'result': json.loads(job.result_data) if job.result_data else None,
    }


def pending_jobs(admin_id):
    admin = db.session.get(Admin, admin_id)
    jobs = (Job.query.filter(Job.admin_id == admin_id, Job.admin_created_at == admin.created_at,
                             Job.state.in_(('queued', 'running')))
            .order_by(Job.id).limit(100).all())
    return [{'job_id': job.public_id, 'email': job.email, 'state': job.state,
             'server_id': job.server_id} for job in jobs]


def _claim_job(blocked_servers, active_servers):
    earlier = aliased(Job)
    query = Job.query.filter(
        Job.state == 'queued', Job.ready_at <= _now(),
        ~db.session.query(earlier.id).filter(
            earlier.server_id == Job.server_id,
            earlier.scope.in_([Job.scope, Job.target_scope]) |
            earlier.target_scope.in_([Job.scope, Job.target_scope]),
            or_(earlier.state == 'running', (earlier.state == 'queued') & (earlier.id < Job.id)),
        ).exists(),
    )
    if blocked_servers:
        query = query.filter(Job.server_id.notin_(blocked_servers))
    last_id = 0
    while True:
        candidates = query.filter(Job.id > last_id).order_by(Job.id).limit(32).all()
        if not candidates:
            return None
        for job in candidates:
            last_id = job.id
            compat = xui_compat.cached_compatibility(job.server_id)
            version = xui_compat.normalize_version(compat.detected_version if compat else None)
            probe = xui.XUI_CAPABILITY_CACHE.get(job.server_id)
            known_modern = (probe['v3_clients'] if probe and probe['expiry'] > time.time()
                            else version.family and version.family >= (3, 1))
            # Legacy writes can replace the whole inbound, so serialize until the
            # server has established its first-class client API.
            legacy = not known_modern
            if legacy and job.server_id in active_servers:
                continue
            job_id, server_id = job.id, job.server_id
            claimed = Job.query.filter_by(id=job_id, state='queued').update(
                {Job.state: 'running'}, synchronize_session=False)
            db.session.commit()
            if claimed:
                return job_id, server_id, legacy


def _dispatch(app, job, data, endpoint, path, payload, **arguments):
    with app.test_request_context(path, method='POST', json=payload, base_url=data['base_url'],
                                  environ_overrides={'REMOTE_ADDR': data['remote_addr']}):
        session['admin_id'] = job.admin_id
        g.client_mutation_job = True
        try:
            result = app.view_functions['clients.' + endpoint](**arguments)
        except HTTPException as exc:
            result = jsonify(success=False, error=exc.name), exc.code
        except Exception:
            db.session.rollback()
            raise
        response = app.process_response(app.make_response(result))
        return int(response.headers.get('X-Eve-Status', response.status_code)), response.get_json(silent=True)


def _execute(app, job_id):
    from panel.routes.clients import _acquire_renew_lock, _release_renew_lock, _start_renew_lock_heartbeat

    job = db.session.get(Job, job_id)
    data = json.loads(job.request_data)
    admin = db.session.get(Admin, job.admin_id)
    server = db.session.get(Server, job.server_id)
    if admin is None or not admin.enabled or admin.created_at != job.admin_created_at:
        _complete(job, 403, {'success': False, 'error': 'The administrator is no longer authorized'})
        return
    if server is None or [server.host, str(server.created_at)] != data['server_identity']:
        _complete(job, 409, {'success': False, 'error': 'The server changed before this operation ran'})
        return
    payload = data['payload']
    arguments = {'server_id': job.server_id, 'inbound_id': data['inbound_id']}
    account_path = f"/api/client/{job.server_id}/{data['inbound_id']}"
    if job.action != 'add':
        arguments['email'] = job.email
        account_path += '/' + quote(job.email, safe='')
    lock_key = f'renew:lock:{job.server_id}:{job.email.lower()}'
    db.session.commit()
    token = _acquire_renew_lock(lock_key)
    if token is None:
        job.state = 'queued'
        job.ready_at = _now() + timedelta(seconds=2)
        db.session.commit()
        return
    heartbeat = _start_renew_lock_heartbeat(lock_key, token)
    try:
        membership = payload.pop('membership', None) if job.action == 'edit' else None
        if membership is not None:
            path = f'/api/client/{job.server_id}/{quote(job.email, safe="")}/inbounds'
            status, result = _dispatch(app, job, data, 'set_client_inbounds', path, membership,
                                       server_id=job.server_id, email=job.email)
            if not result or not result.get('success'):
                _complete(job, status, result)
                return
            targets = membership['inbound_ids']
            if arguments['inbound_id'] not in targets:
                arguments['inbound_id'] = targets[0]
                account_path = f"/api/client/{job.server_id}/{targets[0]}/{quote(job.email, safe='')}"
        status, result = _dispatch(app, job, data, _ACTIONS[job.action],
                                   account_path + '/' + job.action, payload, **arguments)
        if job.action == 'edit' and isinstance(result, dict):
            if result.get('success'):
                result['inbound_id'] = arguments['inbound_id']
            elif membership is not None:
                result['membership_updated'] = True
        _complete(job, status, result)
    finally:
        if heartbeat is not None:
            heartbeat.set()
        _release_renew_lock(lock_key, token)


def _complete(job, status, result):
    job.state = 'done'
    result = result if isinstance(result, dict) else {'success': False, 'error': f'HTTP {status}'}
    job.result_data = json.dumps({'status': status, 'body': result})
    job.request_data = None
    job.ready_at = _now()
    db.session.commit()


def _run_job(app, job_id):
    with app.app_context():
        try:
            _execute(app, job_id)
        except Exception as exc:
            db.session.rollback()
            app.logger.error('Client mutation job %s was interrupted (%s)', job_id, type(exc).__name__)
            _mark_interrupted(Job.query.filter_by(id=job_id, state='running'))
            db.session.commit()
        finally:
            _WAKE.set()


def _mark_interrupted(query):
    query.update({Job.state: 'uncertain', Job.request_data: None, Job.ready_at: _now()},
                 synchronize_session=False)


def client_mutation_worker():
    from app import app

    with app.app_context():
        engine = db.engine
    # The scheduler's local singleton covers SQLite deployments. The database
    # lock also elects one queue dispatcher when PostgreSQL spans several hosts.
    while True:
        with resource_lock('client_mutation_dispatcher', engine=engine) as owner:
            if owner:
                _drain_jobs(app)
        _WAKE.wait(2)
        _WAKE.clear()


def _drain_jobs(app):
    active = {}
    recover = True
    next_cleanup = _now()
    with ThreadPoolExecutor(max_workers=_PARALLELISM, thread_name_prefix='client-mutation') as pool:
        while True:
            try:
                with app.app_context():
                    if recover:
                        # A previous process may have reached the panel before it stopped.
                        _mark_interrupted(Job.query.filter_by(state='running'))
                        db.session.commit()
                        recover = False
                    if _now() >= next_cleanup:
                        Job.query.filter(Job.state.in_(('done', 'uncertain')),
                                         Job.ready_at < _now() - timedelta(days=1)).delete(synchronize_session=False)
                        db.session.commit()
                        next_cleanup = _now() + timedelta(hours=1)
                    active = {future: info for future, info in active.items() if not future.done()}
                    while len(active) < _PARALLELISM:
                        servers = {sid for sid, _ in active.values()}
                        blocked = {sid for sid, legacy in active.values() if legacy}
                        claim = _claim_job(blocked, servers)
                        if claim is None:
                            break
                        job_id, server_id, legacy = claim
                        active[pool.submit(_run_job, app, job_id)] = (server_id, legacy)
            except Exception as exc:
                app.logger.error('Client mutation worker could not process its queue (%s)', type(exc).__name__)
            _WAKE.wait(1)
            _WAKE.clear()
