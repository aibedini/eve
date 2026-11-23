"""God AI's v1 read-only context API and proposal intake."""
import json
import logging
import re
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

from flask import Blueprint, abort, g, jsonify, request
from sqlalchemy import and_, func, or_
from werkzeug.exceptions import HTTPException

from panel.extensions import db
from panel.models import CustomerAccount, ServiceOwnership
from panel.models.machine import MachinePhoneLookup, MachineProposal
from panel.models.core import RenewalEvent
from panel.services import machine_context as context
from panel.services.machine_security import (
    authenticate, check_network, configured_secret, phone_lookup_hash, timestamp, utcnow,
)

bp = Blueprint('machine', __name__, url_prefix='/machine/v1')
audit = logging.getLogger('eve.machine.audit')


def envelope(**data):
    return jsonify(**data, schema_version=1, source='eve', fetched_at=timestamp(utcnow()))


@bp.before_request
def authenticate_machine():
    request_id = request.headers.get('X-Request-ID', '')
    g.request_id = request_id if re.fullmatch(
        r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', request_id
    ) else str(uuid4())
    check_network()
    authenticate()
    if request.method not in ('GET', 'POST'):
        abort(405)


@bp.after_request
def response_headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Request-ID'] = g.request_id
    if getattr(g, 'machine_key_id', None):
        audit.info('machine_access key_id=%s endpoint=%s status=%s request_id=%s',
                   g.machine_key_id, request.endpoint, response.status_code, g.request_id)
    return response


@bp.errorhandler(404)
@bp.errorhandler(405)
@bp.errorhandler(413)
@bp.errorhandler(429)
@bp.errorhandler(HTTPException)
def machine_error(error):
    response = envelope(error=error.name)
    response.status_code = error.code
    if error.code == 401:
        response.headers['WWW-Authenticate'] = 'Bearer'
    if error.code == 429:
        response.headers['Retry-After'] = str(getattr(g, 'machine_retry_after', 60))
    return response


@bp.errorhandler(Exception)
def unavailable(error):
    db.session.rollback()
    audit.error('machine_api_failed endpoint=%s type=%s', request.endpoint, type(error).__name__)
    return envelope(error='Service Unavailable'), 503


def scope(name):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if name not in g.machine_scopes:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def customer(customer_id):
    if not 0 < customer_id <= 2147483647:
        abort(400)
    return db.get_or_404(CustomerAccount, customer_id)


def service(key):
    owner = context.find_service(key)
    if owner is None:
        abort(404)
    return owner


def limit(default, maximum=100):
    value = request.args.get('limit', str(default))
    if not re.fullmatch(r'[0-9]{1,3}', value) or not 1 <= int(value) <= maximum:
        abort(400)
    return int(value)


def parse_time(value):
    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)', value):
        abort(400)
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        abort(400)


@bp.get('/health', provide_automatic_options=False)
def health():
    from app import APP_VERSION
    return envelope(status='ok', version=APP_VERSION)


@bp.get('/customers/<int:customer_id>', provide_automatic_options=False)
@scope('context.customer.read')
def get_customer(customer_id):
    return envelope(**context.customer_views([customer(customer_id)])[0])


@bp.get('/customers:lookup', provide_automatic_options=False)
@scope('context.customer.read')
def lookup_customer():
    value = request.args.get('phone_hash', '')
    if (set(request.args) != {'phone_hash'} or len(request.args.getlist('phone_hash')) != 1
            or not re.fullmatch(r'[0-9a-f]{64}', value)):
        abort(400)
    pepper = configured_secret('EVE_MACHINE_PHONE_PEPPER')
    if not pepper:
        abort(503)
    rows = CustomerAccount.query.join(MachinePhoneLookup).filter(
        MachinePhoneLookup.phone_hash == value).limit(100).all()
    # Recompute against the current contact and pepper before exposing any match.
    rows = [row for row in rows if phone_lookup_hash(row.primary_phone, pepper) == value]
    return envelope(items=[dict(item, match_basis='phone_hash') for item in context.customer_views(rows)])


@bp.get('/customers', provide_automatic_options=False)
@scope('context.customer.read')
def customers():
    count = limit(50)
    query = CustomerAccount.query
    since = request.args.get('updated_since')
    if since:
        query = query.filter(CustomerAccount.updated_at >= parse_time(since))
    cursor = request.args.get('cursor')
    if cursor:
        stamp, separator, row_id = cursor.rpartition('~')
        if not separator or not re.fullmatch(r'[1-9][0-9]{0,9}', row_id) or int(row_id) > 2147483647:
            abort(400)
        value = parse_time(stamp)
        query = query.filter(or_(CustomerAccount.updated_at > value,
                                 and_(CustomerAccount.updated_at == value, CustomerAccount.id > int(row_id))))
    rows = query.order_by(CustomerAccount.updated_at, CustomerAccount.id).limit(count + 1).all()
    next_cursor = f'{timestamp(rows[count - 1].updated_at)}~{rows[count - 1].id}' if len(rows) > count else None
    return envelope(items=context.customer_views(rows[:count]), next_cursor=next_cursor)


@bp.get('/customers/<int:customer_id>/services', provide_automatic_options=False)
@scope('context.service.read')
def customer_services(customer_id):
    customer(customer_id)
    return envelope(items=context.service_views(
        context.active_services().filter_by(customer_id=customer_id).order_by(ServiceOwnership.id).limit(100).all()))


@bp.get('/services/<service_key>', provide_automatic_options=False)
@scope('context.service.read')
def get_service(service_key):
    return envelope(**context.service_views([service(service_key)])[0])


@bp.get('/services:lookup', provide_automatic_options=False)
@scope('context.service.read')
def lookup_service():
    email = request.args.get('client_email', '').strip()
    if not email or len(email) > 255 or any(ord(char) < 32 for char in email):
        abort(400)
    rows = context.active_services().filter(func.lower(ServiceOwnership.client_email_snapshot) == email.lower()).limit(2).all()
    if len(rows) != 1:
        abort(404)
    return envelope(**context.service_views(rows)[0])


@bp.get('/services/<service_key>/renewals', provide_automatic_options=False)
@scope('context.renewal.read')
def renewals(service_key):
    rows = context.renewal_rows(service(service_key)).order_by(RenewalEvent.renewed_at.desc(), RenewalEvent.id.desc())
    return envelope(items=[context.renewal_view(row) for row in rows.limit(limit(20))])


@bp.get('/customers/<int:customer_id>/payments', provide_automatic_options=False)
@scope('context.payment.read_masked')
def payments(customer_id):
    customer(customer_id)
    since = request.args.get('since')
    return envelope(items=context.payment_views(customer_id, parse_time(since) if since else None))


@bp.get('/customers/<int:customer_id>/support-requests', provide_automatic_options=False)
@scope('context.support.read')
def support_requests(customer_id):
    customer(customer_id)
    return envelope(items=context.support_views(customer_id))


@bp.get('/ownership-claims', provide_automatic_options=False)
@scope('context.customer.read')
def claims():
    status = request.args.get('status', 'pending')
    if status not in ('pending', 'approved', 'rejected', 'needs_attention', 'partially_approved'):
        abort(400)
    return envelope(items=context.claim_views(status))


@bp.get('/knowledge/faqs', provide_automatic_options=False)
@scope('context.knowledge.read')
def knowledge():
    return envelope(**context.knowledge_view())


@bp.get('/sms/notifications/<service_key>', provide_automatic_options=False)
@scope('context.service.read')
def notifications(service_key):
    return envelope(items=context.notification_views(service(service_key)))


@bp.post('/proposals', provide_automatic_options=False)
@scope('proposals.write')
def proposals():
    if request.content_length is not None and request.content_length > 65536:
        abort(400)
    if not request.is_json:
        abort(400)
    try:
        raw = request.stream.read(65537)
        if len(raw) > 65536:
            abort(400)
        data = json.loads(raw)
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
        size = len(payload.encode('utf-8'))
    except (ValueError, RecursionError):
        abort(400)
    if not isinstance(data, dict) or not data or size > 65536:
        abort(400)
    row = MachineProposal(id=str(uuid4()), key_id=g.machine_key_id, payload=payload,
                          status='pending', created_at=utcnow())
    db.session.add(row)
    db.session.commit()
    return envelope(proposal_id=row.id, status=row.status), 202
