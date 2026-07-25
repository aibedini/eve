"""Notification/account-message/renew templates and package-recommendation API routes (extracted from app.py)."""
from flask import Blueprint, jsonify, request, session

from panel.extensions import db
from panel.models import Admin, NotificationTemplate, RenewTemplate
from panel.routes.common import login_required, user_management_required
from panel.services.billing import (
    _empty_recommendation_template_vars, _recommendation_template_vars,
)


bp = Blueprint('templates_api', __name__)


ACCOUNT_INFO_WHATSAPP_TEMPLATE_TYPE = 'account_info_whatsapp'


ACCOUNT_INFO_SMS_TEMPLATE_TYPE = 'account_info_sms'


ROYALTY_INFO_WHATSAPP_TEMPLATE_TYPE = 'royalty_info_whatsapp'


# Channel-specific variants for Client-Created and Renew notifications
CLIENT_CREATED_WHATSAPP_TEMPLATE_TYPE = 'client_created_whatsapp'


RENEW_WHATSAPP_TEMPLATE_TYPE = 'renew_whatsapp'


DEFAULT_ACCOUNT_INFO_SMS_TEMPLATE = """{email}
Time: {remaining_time}
Volume: {remaining_volume}
Link: {dashboard_link}"""


DEFAULT_ROYALTY_INFO_WHATSAPP_TEMPLATE = """سلام {email} عزیز 👋
اکانت شما آماده‌ست ولی هنوز وصل نشدین!

لینک اتصال: {dashboard_link}

اگه مشکلی دارین بگین کمک کنیم 🙏"""


DEFAULT_CLIENT_CREATED_WHATSAPP_TEMPLATE = """اکانت شما ساخته شد ✅
اسم اکانت: {email}
حجم: {volume} | مدت: {days} روز
لینک اتصال: {dashboard_link}

لطفا از طریق لینک بالا به سرویس خود متصل شین."""


DEFAULT_RENEW_WHATSAPP_TEMPLATE = """تمدید شد ✅
اسم اکانت: {email}
{days_label} | {volume_label}
تاریخ انقضا: {date}
لینک: {dashboard_link}"""


ROYALTY_INFO_SMS_TEMPLATE_TYPE = 'royalty_info_sms'
CLIENT_CREATED_SMS_TEMPLATE_TYPE = 'client_created_sms'
RENEW_SMS_TEMPLATE_TYPE = 'renew_sms'

DEFAULT_ACCOUNT_INFO_WHATSAPP_TEMPLATE = """اطلاعات اکانت شما
اسم اکانت: {email}
مدت زمان باقی مانده: {remaining_time}
حجم باقی مانده: {remaining_volume}
لینک dash sub: {dashboard_link}

لطفا از طریق لینک بالا به سرویس خود متصل شین ."""



DEFAULT_ROYALTY_INFO_SMS_TEMPLATE = """{email}
اکانت آماده‌ست، وصل نشدی!
لینک: {dashboard_link}"""


DEFAULT_CLIENT_CREATED_SMS_TEMPLATE = """{email}
Volume: {volume} | Days: {days}
Link: {dashboard_link}"""


DEFAULT_RENEW_SMS_TEMPLATE = """{email}
{days_label} | {volume_label}
Renewed. Link: {dashboard_link}"""


@bp.route('/api/templates', methods=['GET'])
@user_management_required
def get_templates():
    from app import app  # deferred: app-level helper, avoids circular import
    template_type = (request.args.get('type') or 'client_created').strip().lower()
    query = NotificationTemplate.query
    if template_type != 'all':
        query = query.filter_by(type=template_type)
    templates = query.order_by(NotificationTemplate.created_at.desc()).all()
    return jsonify({'success': True, 'templates': [t.to_dict() for t in templates]})


@bp.route('/api/templates', methods=['POST'])
@user_management_required
def create_template():
    from app import app  # deferred: app-level helper, avoids circular import
    data = request.get_json()
    name = data.get('name')
    content = data.get('content')
    template_type = (data.get('type') or 'client_created').strip().lower()
    if not name or not content:
        return jsonify({'success': False, 'error': 'Name and content are required'}), 400

    template = NotificationTemplate(name=name, content=content, type=template_type)
    db.session.add(template)
    db.session.commit()

    type_count = NotificationTemplate.query.filter_by(type=template_type).count()
    if type_count == 1:
        template.is_active = True
        db.session.commit()

    return jsonify({'success': True, 'template': template.to_dict()})


@bp.route('/api/templates/<int:template_id>', methods=['PUT'])
@user_management_required
def update_template(template_id):
    from app import app  # deferred: app-level helper, avoids circular import
    template = db.session.get(NotificationTemplate, template_id)
    if not template:
        return jsonify({'success': False, 'error': 'Template not found'}), 404
    
    data = request.get_json()
    template.name = data.get('name', template.name)
    template.content = data.get('content', template.content)
    db.session.commit()
    return jsonify({'success': True, 'template': template.to_dict()})


@bp.route('/api/templates/<int:template_id>', methods=['DELETE'])
@user_management_required
def delete_template(template_id):
    from app import app  # deferred: app-level helper, avoids circular import
    template = db.session.get(NotificationTemplate, template_id)
    if not template:
        return jsonify({'success': False, 'error': 'Template not found'}), 404
    if template.is_active:
        return jsonify({'success': False, 'error': 'Cannot delete active template'}), 400
    
    db.session.delete(template)
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/templates/<int:template_id>/activate', methods=['POST'])
@user_management_required
def activate_template(template_id):
    from app import app  # deferred: app-level helper, avoids circular import
    template = db.session.get(NotificationTemplate, template_id)
    if not template:
        return jsonify({'success': False, 'error': 'Template not found'}), 404
    
    # Deactivate all others of the same type
    NotificationTemplate.query.filter_by(type=template.type).update({NotificationTemplate.is_active: False})
    template.is_active = True
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/templates/active', methods=['GET'])
@login_required
def get_active_template():
    from app import app  # deferred: app-level helper, avoids circular import
    template_type = (request.args.get('type') or 'client_created').strip().lower()
    template = NotificationTemplate.query.filter_by(type=template_type, is_active=True).first()
    return jsonify({
        'success': True,
        'template': template.to_dict() if template else None,
        'content': template.content if template else ''
    })


def _account_info_template_vars():
    return [
        '{email}', '{account_name}', '{remaining_time}', '{remaining_volume}',
        '{dashboard_link}', '{sub_link}', '{server_name}',
        '{telegram_channel}', '{whatsapp_channel}',
        '{gift_volume}', '{if_gift}...{/if_gift}',
        '{recommended_package}', '{recommended_volume}', '{recommended_days}',
        '{recommended_price}', '{recommended_daily_usage}', '{recommended_31d_usage}',
        '{if_recommendation}...{/if_recommendation}',
        '{comfort_package}', '{comfort_volume}', '{comfort_days}', '{comfort_price}',
        '{if_comfort_recommendation}...{/if_comfort_recommendation}',
    ]


@bp.route('/api/package-recommendation/template-vars', methods=['GET'])
@login_required
def get_package_recommendation_template_vars():
    """Recommendation variables for manual WhatsApp/SMS template rendering."""
    from app import (  # deferred: app-level helper, avoids circular import
        app, get_accessible_servers,
    )
    try:
        server_id = int(request.args.get('server_id') or 0)
    except (TypeError, ValueError):
        server_id = 0
    sub_id = str(request.args.get('sub_id') or '').strip()
    email = str(request.args.get('email') or '').strip()
    terminal = str(request.args.get('terminal') or '').strip().lower() in ('1', 'true', 'yes')
    if not server_id or not sub_id:
        return jsonify({'success': True, 'variables': _empty_recommendation_template_vars()})

    admin = db.session.get(Admin, session.get('admin_id'))
    allowed_server_ids = {int(item.id) for item in get_accessible_servers(admin)} if admin else set()
    if not admin or server_id not in allowed_server_ids:
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    return jsonify({
        'success': True,
        'variables': _recommendation_template_vars(
            server_id, sub_id, email, terminal=terminal,
        ),
    })


# channel name → (template type, default content)
_CHANNEL_TEMPLATE_MAP = {
    'whatsapp':                (lambda: ACCOUNT_INFO_WHATSAPP_TEMPLATE_TYPE,   lambda: DEFAULT_ACCOUNT_INFO_WHATSAPP_TEMPLATE),
    'sms':                     (lambda: ACCOUNT_INFO_SMS_TEMPLATE_TYPE,        lambda: DEFAULT_ACCOUNT_INFO_SMS_TEMPLATE),
    'royalty_whatsapp':        (lambda: ROYALTY_INFO_WHATSAPP_TEMPLATE_TYPE,   lambda: DEFAULT_ROYALTY_INFO_WHATSAPP_TEMPLATE),
    'royalty_sms':             (lambda: ROYALTY_INFO_SMS_TEMPLATE_TYPE,        lambda: DEFAULT_ROYALTY_INFO_SMS_TEMPLATE),
    'client_created_whatsapp': (lambda: CLIENT_CREATED_WHATSAPP_TEMPLATE_TYPE, lambda: DEFAULT_CLIENT_CREATED_WHATSAPP_TEMPLATE),
    'client_created_sms':      (lambda: CLIENT_CREATED_SMS_TEMPLATE_TYPE,      lambda: DEFAULT_CLIENT_CREATED_SMS_TEMPLATE),
    'renew_whatsapp':          (lambda: RENEW_WHATSAPP_TEMPLATE_TYPE,          lambda: DEFAULT_RENEW_WHATSAPP_TEMPLATE),
    'renew_sms':               (lambda: RENEW_SMS_TEMPLATE_TYPE,               lambda: DEFAULT_RENEW_SMS_TEMPLATE),
}


_TYPE_TO_CHANNEL = {
    ACCOUNT_INFO_WHATSAPP_TEMPLATE_TYPE: 'whatsapp',
    ACCOUNT_INFO_SMS_TEMPLATE_TYPE: 'sms',
    ROYALTY_INFO_WHATSAPP_TEMPLATE_TYPE: 'royalty_whatsapp',
    ROYALTY_INFO_SMS_TEMPLATE_TYPE: 'royalty_sms',
    CLIENT_CREATED_WHATSAPP_TEMPLATE_TYPE: 'client_created_whatsapp',
    CLIENT_CREATED_SMS_TEMPLATE_TYPE: 'client_created_sms',
    RENEW_WHATSAPP_TEMPLATE_TYPE: 'renew_whatsapp',
    RENEW_SMS_TEMPLATE_TYPE: 'renew_sms',
}


def _account_info_template_type(channel='whatsapp'):
    channel = (channel or 'whatsapp').strip().lower()
    entry = _CHANNEL_TEMPLATE_MAP.get(channel)
    return entry[0]() if entry else ACCOUNT_INFO_WHATSAPP_TEMPLATE_TYPE


def _account_info_default_template(channel='whatsapp'):
    from app import DEFAULT_ACCOUNT_INFO_WHATSAPP_TEMPLATE  # deferred: app-level helper, avoids circular import
    channel = (channel or 'whatsapp').strip().lower()
    entry = _CHANNEL_TEMPLATE_MAP.get(channel)
    return entry[1]() if entry else DEFAULT_ACCOUNT_INFO_WHATSAPP_TEMPLATE


def _account_info_channel_from_type(template_type):
    return _TYPE_TO_CHANNEL.get(template_type, 'whatsapp')


_ALL_ACCOUNT_INFO_TYPES = (
    ACCOUNT_INFO_WHATSAPP_TEMPLATE_TYPE, ACCOUNT_INFO_SMS_TEMPLATE_TYPE,
    ROYALTY_INFO_WHATSAPP_TEMPLATE_TYPE, ROYALTY_INFO_SMS_TEMPLATE_TYPE,
    CLIENT_CREATED_WHATSAPP_TEMPLATE_TYPE, CLIENT_CREATED_SMS_TEMPLATE_TYPE,
    RENEW_WHATSAPP_TEMPLATE_TYPE, RENEW_SMS_TEMPLATE_TYPE,
)


def _ensure_default_account_info_template(channel='whatsapp'):
    from app import (  # deferred: app-level helper, avoids circular import
        CLIENT_CREATED_SMS_TEMPLATE_TYPE, RENEW_SMS_TEMPLATE_TYPE,
        ROYALTY_INFO_SMS_TEMPLATE_TYPE,
    )
    template_type = _account_info_template_type(channel)
    # Only ensure a global (owner_id=None) default exists
    existing = NotificationTemplate.query.filter_by(type=template_type, owner_id=None).first()
    if existing:
        return
    name_map = {
        ACCOUNT_INFO_SMS_TEMPLATE_TYPE: 'Default Account Info SMS',
        ROYALTY_INFO_WHATSAPP_TEMPLATE_TYPE: 'Default Royalty Info',
        ROYALTY_INFO_SMS_TEMPLATE_TYPE: 'Default Royalty Info SMS',
        CLIENT_CREATED_WHATSAPP_TEMPLATE_TYPE: 'Default Client Created WhatsApp',
        CLIENT_CREATED_SMS_TEMPLATE_TYPE: 'Default Client Created SMS',
        RENEW_WHATSAPP_TEMPLATE_TYPE: 'Default Renew WhatsApp',
        RENEW_SMS_TEMPLATE_TYPE: 'Default Renew SMS',
    }
    template = NotificationTemplate(
        name=name_map.get(template_type, 'Default Account Info'),
        content=_account_info_default_template(channel),
        type=template_type,
        is_active=True,
        owner_id=None,
    )
    db.session.add(template)
    db.session.commit()


def _resolve_account_info_template(admin: 'Admin', channel: str = 'whatsapp') -> 'NotificationTemplate | None':
    """Return the best-match active template for an admin.

    Priority:
    1. Reseller-specific active template (owner_id = admin.id)
    2. Global active template (owner_id = NULL)
    """
    template_type = _account_info_template_type(channel)
    if admin and admin.role == 'reseller':
        specific = NotificationTemplate.query.filter_by(
            type=template_type, owner_id=admin.id, is_active=True
        ).first()
        if specific:
            return specific
    return NotificationTemplate.query.filter_by(
        type=template_type, owner_id=None, is_active=True
    ).first()


@bp.route('/api/account-message-templates', methods=['GET'])
@user_management_required
def get_account_message_templates():
    from app import (  # deferred: app-level helper, avoids circular import
        DEFAULT_ACCOUNT_INFO_WHATSAPP_TEMPLATE,
        DEFAULT_CLIENT_CREATED_SMS_TEMPLATE, DEFAULT_RENEW_SMS_TEMPLATE,
        DEFAULT_ROYALTY_INFO_SMS_TEMPLATE, app,
    )
    for _ch in ('whatsapp', 'sms', 'royalty_whatsapp', 'royalty_sms',
                'client_created_whatsapp', 'client_created_sms',
                'renew_whatsapp', 'renew_sms'):
        _ensure_default_account_info_template(_ch)

    current_admin = db.session.get(Admin, session.get('admin_id'))

    # Resellers only see their own specific templates + global ones
    # Admins/superadmins see everything
    q = NotificationTemplate.query.filter(
        NotificationTemplate.type.in_(_ALL_ACCOUNT_INFO_TYPES)
    )
    if current_admin and current_admin.role == 'reseller':
        q = q.filter(
            (NotificationTemplate.owner_id == current_admin.id) |
            (NotificationTemplate.owner_id == None)  # noqa: E711
        )
    templates = q.order_by(NotificationTemplate.owner_id.desc().nullslast(),
                           NotificationTemplate.created_at.desc()).all()

    template_dicts = []
    for template in templates:
        data = template.to_dict()
        data['channel'] = _account_info_channel_from_type(template.type)
        template_dicts.append(data)

    # Build reseller list for admin UI (assign template to reseller)
    resellers = []
    if current_admin and current_admin.role in ('admin', 'superadmin'):
        resellers = [
            {'id': r.id, 'username': r.username}
            for r in Admin.query.filter_by(role='reseller').order_by(Admin.username).all()
        ]

    return jsonify({
        'success': True,
        'templates': template_dicts,
        'available_vars': _account_info_template_vars(),
        'default_content': DEFAULT_ACCOUNT_INFO_WHATSAPP_TEMPLATE,
        'default_sms_content': DEFAULT_ACCOUNT_INFO_SMS_TEMPLATE,
        'default_royalty_whatsapp_content': DEFAULT_ROYALTY_INFO_WHATSAPP_TEMPLATE,
        'default_royalty_sms_content': DEFAULT_ROYALTY_INFO_SMS_TEMPLATE,
        'default_client_created_whatsapp_content': DEFAULT_CLIENT_CREATED_WHATSAPP_TEMPLATE,
        'default_client_created_sms_content': DEFAULT_CLIENT_CREATED_SMS_TEMPLATE,
        'default_renew_whatsapp_content': DEFAULT_RENEW_WHATSAPP_TEMPLATE,
        'default_renew_sms_content': DEFAULT_RENEW_SMS_TEMPLATE,
        'resellers': resellers,
    })


@bp.route('/api/account-message-templates/active', methods=['GET'])
@login_required
def get_active_account_message_template():
    from app import (  # deferred: app-level helper, avoids circular import
        _account_info_channel_links, app,
    )
    channel = (request.args.get('channel') or 'whatsapp').strip().lower()
    # Normalise: 'royalty' shorthand → 'royalty_whatsapp'
    if channel == 'royalty':
        channel = 'royalty_whatsapp'
    _ensure_default_account_info_template(channel)
    default_content = _account_info_default_template(channel)

    current_admin = db.session.get(Admin, session.get('admin_id'))

    # Priority: reseller-specific → global
    template = _resolve_account_info_template(current_admin, channel)

    template_data = template.to_dict() if template else None
    if template_data:
        template_data['channel'] = _account_info_channel_from_type(template.type)

    channel_links = _account_info_channel_links(current_admin) if current_admin else {
        'telegram_channel': '', 'whatsapp_channel': ''
    }

    return jsonify({
        'success': True,
        'template': template_data,
        'content': template.content if template else default_content,
        'available_vars': _account_info_template_vars(),
        'scope': template_data.get('scope') if template_data else 'global',
        **channel_links,
    })


@bp.route('/api/account-message-templates', methods=['POST'])
@user_management_required
def create_account_message_template():
    from app import app  # deferred: app-level helper, avoids circular import
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    content = (data.get('content') or '').strip()
    template_type = _account_info_template_type(data.get('channel') or 'whatsapp')
    if not name or not content:
        return jsonify({'success': False, 'error': 'Name and content are required'}), 400

    # owner_id: None = global, reseller admin.id = reseller-specific
    owner_id = None
    raw_owner = data.get('owner_id')
    if raw_owner:
        try:
            owner_id = int(raw_owner)
            # Validate: must be an existing reseller
            owner = db.session.get(Admin, owner_id)
            if not owner or owner.role != 'reseller':
                return jsonify({'success': False, 'error': 'owner_id must refer to a reseller account'}), 400
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Invalid owner_id'}), 400

    template = NotificationTemplate(
        name=name,
        content=content,
        type=template_type,
        is_active=False,
        owner_id=owner_id,
    )
    db.session.add(template)
    db.session.commit()

    # Auto-activate if it's the only one of its scope
    scope_count = NotificationTemplate.query.filter_by(type=template_type, owner_id=owner_id).count()
    if scope_count == 1:
        template.is_active = True
        db.session.commit()

    template_data = template.to_dict()
    template_data['channel'] = _account_info_channel_from_type(template.type)
    return jsonify({'success': True, 'template': template_data})


@bp.route('/api/account-message-templates/<int:template_id>', methods=['PUT'])
@user_management_required
def update_account_message_template(template_id):
    from app import app  # deferred: app-level helper, avoids circular import
    template = db.session.get(NotificationTemplate, template_id)
    if not template or template.type not in _ALL_ACCOUNT_INFO_TYPES:
        return jsonify({'success': False, 'error': 'Template not found'}), 404
    data = request.get_json() or {}
    if 'name' in data:
        template.name = (data.get('name') or template.name).strip()
    if 'content' in data:
        template.content = (data.get('content') or template.content).strip()
    if not template.is_active and 'channel' in data:
        template.type = _account_info_template_type(data.get('channel') or 'whatsapp')
    if not template.name or not template.content:
        return jsonify({'success': False, 'error': 'Name and content are required'}), 400
    db.session.commit()
    template_data = template.to_dict()
    template_data['channel'] = _account_info_channel_from_type(template.type)
    return jsonify({'success': True, 'template': template_data})


@bp.route('/api/account-message-templates/<int:template_id>', methods=['DELETE'])
@user_management_required
def delete_account_message_template(template_id):
    from app import app  # deferred: app-level helper, avoids circular import
    template = db.session.get(NotificationTemplate, template_id)
    if not template or template.type not in _ALL_ACCOUNT_INFO_TYPES:
        return jsonify({'success': False, 'error': 'Template not found'}), 404
    if template.is_active:
        return jsonify({'success': False, 'error': 'Disable this template before deleting it'}), 400
    db.session.delete(template)
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/account-message-templates/<int:template_id>/activate', methods=['POST'])
@user_management_required
def activate_account_message_template(template_id):
    from app import app  # deferred: app-level helper, avoids circular import
    template = db.session.get(NotificationTemplate, template_id)
    if not template or template.type not in _ALL_ACCOUNT_INFO_TYPES:
        return jsonify({'success': False, 'error': 'Template not found'}), 404
    # Only deactivate templates in the same scope (same owner_id)
    NotificationTemplate.query.filter_by(
        type=template.type, owner_id=template.owner_id
    ).update({NotificationTemplate.is_active: False})
    template.is_active = True
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/account-message-templates/<int:template_id>/disable', methods=['POST'])
@user_management_required
def disable_account_message_template(template_id):
    from app import app  # deferred: app-level helper, avoids circular import
    template = db.session.get(NotificationTemplate, template_id)
    if not template or template.type not in _ALL_ACCOUNT_INFO_TYPES:
        return jsonify({'success': False, 'error': 'Template not found'}), 404
    template.is_active = False
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/renew-templates', methods=['GET'])
@user_management_required
def get_renew_templates():
    from app import app  # deferred: app-level helper, avoids circular import
    templates = RenewTemplate.query.order_by(RenewTemplate.created_at.desc()).all()
    return jsonify({
        'success': True, 
        'templates': [t.to_dict() for t in templates],
        'available_vars': [
            '{email}', '{days}', '{days_label}', '{volume}', '{volume_label}', '{date}', '{server_name}', '{mode}', '{dashboard_link}',
            '{gift_volume}', '{if_gift}...{/if_gift}',
            '{recommended_package}', '{recommended_volume}', '{recommended_days}',
            '{recommended_price}', '{recommended_daily_usage}', '{recommended_31d_usage}',
            '{if_recommendation}...{/if_recommendation}',
            '{comfort_package}', '{comfort_volume}', '{comfort_days}', '{comfort_price}',
            '{if_comfort_recommendation}...{/if_comfort_recommendation}'
        ]
    })


@bp.route('/api/renew-templates', methods=['POST'])
@user_management_required
def create_renew_template():
    from app import app  # deferred: app-level helper, avoids circular import
    data = request.get_json()
    name = data.get('name')
    content = data.get('content')
    if not name or not content:
        return jsonify({'success': False, 'error': 'Name and content are required'}), 400

    template = RenewTemplate(name=name, content=content)
    db.session.add(template)
    db.session.commit()

    if RenewTemplate.query.count() == 1:
        template.is_active = True
        db.session.commit()

    return jsonify({'success': True, 'template': template.to_dict()})


@bp.route('/api/renew-templates/<int:template_id>', methods=['PUT'])
@user_management_required
def update_renew_template(template_id):
    from app import app  # deferred: app-level helper, avoids circular import
    template = db.session.get(RenewTemplate, template_id)
    if not template:
        return jsonify({'success': False, 'error': 'Template not found'}), 404
    
    data = request.get_json()
    template.name = data.get('name', template.name)
    template.content = data.get('content', template.content)
    db.session.commit()
    return jsonify({'success': True, 'template': template.to_dict()})


@bp.route('/api/renew-templates/<int:template_id>', methods=['DELETE'])
@user_management_required
def delete_renew_template(template_id):
    from app import app  # deferred: app-level helper, avoids circular import
    template = db.session.get(RenewTemplate, template_id)
    if not template:
        return jsonify({'success': False, 'error': 'Template not found'}), 404
    if template.is_active:
        return jsonify({'success': False, 'error': 'Cannot delete active template'}), 400
    
    db.session.delete(template)
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/renew-templates/<int:template_id>/activate', methods=['POST'])
@user_management_required
def activate_renew_template(template_id):
    from app import app  # deferred: app-level helper, avoids circular import
    template = db.session.get(RenewTemplate, template_id)
    if not template:
        return jsonify({'success': False, 'error': 'Template not found'}), 404
    
    # Deactivate all others
    RenewTemplate.query.update({RenewTemplate.is_active: False})
    template.is_active = True
    db.session.commit()
    return jsonify({'success': True})
