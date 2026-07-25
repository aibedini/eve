"""Sub-apps, FAQs, announcements, and online-chat scripts API routes (extracted from app.py)."""
import json
import uuid

from flask import Blueprint, jsonify, request, session

from panel.extensions import db
from panel.models import (
    Admin, Announcement, FAQ, OnlineChatScript, Server, SubAppConfig,
)
from panel.routes.common import user_management_required


bp = Blueprint('content', __name__)


# Allowed HTML tags and attributes for FAQ content (XSS Prevention)
ALLOWED_FAQ_TAGS = [
    'a', 'abbr', 'acronym', 'b', 'blockquote', 'code', 'em', 'i', 'li', 'ol', 'strong', 'ul',
    'p', 'br', 'span', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'img', 'hr'
]


ALLOWED_FAQ_ATTRIBUTES = {
    'a': ['href', 'title', 'target'],
    'abbr': ['title'],
    'acronym': ['title'],
    'img': ['src', 'alt', 'title', 'width', 'height', 'class'],
    'span': ['class', 'style'],
    'div': ['class', 'style'],
    'p': ['class', 'style'],
}


ALLOWED_FAQ_STYLES = ['color', 'background-color', 'font-size', 'text-align', 'direction']


@bp.route('/api/sub-apps', methods=['GET'])
def get_sub_apps():
    from app import app  # deferred: app-level helper, avoids circular import
    apps = SubAppConfig.query.order_by(SubAppConfig.display_order, SubAppConfig.id).all()
    return jsonify([a.to_dict() for a in apps])


@bp.route('/api/sub-apps', methods=['POST'])
@user_management_required
def create_sub_app():
    from app import (  # deferred: app-level helper, avoids circular import
        app, sanitize_html,
    )
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    # Auto-generate app_code if not provided
    app_code = data.get('app_code')
    if not app_code:
        app_code = str(uuid.uuid4())[:8]
        
    if SubAppConfig.query.filter_by(app_code=app_code).first():
        return jsonify({'success': False, 'error': 'App code already exists'}), 400
        
    # Sanitize descriptions to prevent XSS
    desc_fa = sanitize_html(data.get('description_fa', ''))
    desc_en = sanitize_html(data.get('description_en', ''))

    new_app = SubAppConfig(
        app_code=app_code,
        name=sanitize_html(data.get('name')),
        os_type=data.get('os_type', 'android'),
        is_enabled=data.get('is_enabled', True),
        title_fa=sanitize_html(data.get('title_fa')),
        description_fa=desc_fa,
        title_en=sanitize_html(data.get('title_en')),
        description_en=desc_en,
        download_link=data.get('download_link'),
        store_link=data.get('store_link'),
        tutorial_link=data.get('tutorial_link'),
        icon_url=data.get('icon_url'),
        is_recommended=data.get('is_recommended', False)
    )
    
    try:
        db.session.add(new_app)
        db.session.commit()
        return jsonify({'success': True, 'app': new_app.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/sub-apps/<int:app_id>', methods=['PUT'])
@user_management_required
def update_sub_app(app_id):
    from app import (  # deferred: app-level helper, avoids circular import
        app, sanitize_html,
    )
    app_config = db.session.get(SubAppConfig, app_id)
    if not app_config:
        return jsonify({'success': False, 'error': 'App not found'}), 404
        
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
        
    # Check if app_code is being changed and if it conflicts
    new_app_code = data.get('app_code')
    if new_app_code and new_app_code != app_config.app_code:
        if SubAppConfig.query.filter_by(app_code=new_app_code).first():
            return jsonify({'success': False, 'error': 'App code already exists'}), 400
        app_config.app_code = new_app_code
        
    if 'name' in data: app_config.name = sanitize_html(data['name'])
    if 'os_type' in data: app_config.os_type = data['os_type']
    if 'is_enabled' in data: app_config.is_enabled = data['is_enabled']
    if 'title_fa' in data: app_config.title_fa = sanitize_html(data['title_fa'])
    if 'description_fa' in data:
        app_config.description_fa = sanitize_html(data['description_fa'])
    if 'title_en' in data: app_config.title_en = sanitize_html(data['title_en'])
    if 'description_en' in data:
        app_config.description_en = sanitize_html(data['description_en'])
    if 'download_link' in data: app_config.download_link = data['download_link']
    if 'store_link' in data: app_config.store_link = data['store_link']
    if 'tutorial_link' in data: app_config.tutorial_link = data['tutorial_link']
    if 'icon_url' in data: app_config.icon_url = data['icon_url']
    if 'is_recommended' in data: app_config.is_recommended = data['is_recommended']
    if 'display_order' in data:
        try:
            app_config.display_order = int(data['display_order'])
        except (TypeError, ValueError):
            pass

    try:
        db.session.commit()
        return jsonify({'success': True, 'app': app_config.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/sub-apps/reorder', methods=['POST'])
@user_management_required
def reorder_sub_apps():
    """Accept [{id, display_order}, ...] and bulk-update ordering."""
    from app import app  # deferred: app-level helper, avoids circular import
    items = request.get_json() or []
    if not isinstance(items, list):
        return jsonify({'success': False, 'error': 'Expected a list'}), 400
    try:
        for item in items:
            app_id = int(item.get('id') or 0)
            order = int(item.get('display_order') or 0)
            if app_id:
                SubAppConfig.query.filter_by(id=app_id).update({'display_order': order})
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/sub-apps/<int:app_id>', methods=['DELETE'])
@user_management_required
def delete_sub_app(app_id):
    from app import app  # deferred: app-level helper, avoids circular import
    app_config = db.session.get(SubAppConfig, app_id)
    if not app_config:
        return jsonify({'success': False, 'error': 'App not found'}), 404
        
    try:
        db.session.delete(app_config)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# FAQ APIs
@bp.route('/api/faqs', methods=['GET'])
@user_management_required
def get_faqs():
    from app import app  # deferred: app-level helper, avoids circular import
    faqs = FAQ.query.order_by(FAQ.created_at.desc()).all()
    resp = jsonify([f.to_dict() for f in faqs])
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@bp.route('/api/faqs', methods=['POST'])
@user_management_required
def create_faq():
    from app import (  # deferred: app-level helper, avoids circular import
        app, sanitize_html,
    )
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
    
    if not data.get('title'):
        return jsonify({'success': False, 'error': 'Title is required'}), 400
        
    # Sanitize HTML content to prevent XSS
    content = sanitize_html(
        data.get('content', ''),
        tags=ALLOWED_FAQ_TAGS,
        attributes=ALLOWED_FAQ_ATTRIBUTES,
        styles=ALLOWED_FAQ_STYLES
    )

    new_faq = FAQ(
        title=sanitize_html(data.get('title')),
        content=content,
        image_url=data.get('image_url'),
        video_url=data.get('video_url'),
        platform=data.get('platform', 'android'),
        is_enabled=data.get('is_enabled', True)
    )
    
    try:
        db.session.add(new_faq)
        db.session.commit()
        return jsonify({'success': True, 'faq': new_faq.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/faqs/<int:faq_id>', methods=['PUT'])
@user_management_required
def update_faq(faq_id):
    from app import (  # deferred: app-level helper, avoids circular import
        app, sanitize_html,
    )
    faq = db.session.get(FAQ, faq_id)
    if not faq:
        return jsonify({'success': False, 'error': 'FAQ not found'}), 404
        
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400
        
    if 'title' in data: faq.title = sanitize_html(data['title'])
    if 'content' in data:
        # Sanitize HTML content to prevent XSS
        faq.content = sanitize_html(
            data['content'],
            tags=ALLOWED_FAQ_TAGS,
            attributes=ALLOWED_FAQ_ATTRIBUTES,
            styles=ALLOWED_FAQ_STYLES
        )
    if 'image_url' in data: faq.image_url = data['image_url']
    if 'video_url' in data: faq.video_url = data['video_url']
    if 'platform' in data: faq.platform = data['platform']
    if 'is_enabled' in data: faq.is_enabled = data['is_enabled']
    
    try:
        db.session.commit()
        return jsonify({'success': True, 'faq': faq.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/faqs/<int:faq_id>', methods=['DELETE'])
@user_management_required
def delete_faq(faq_id):
    from app import app  # deferred: app-level helper, avoids circular import
    faq = db.session.get(FAQ, faq_id)
    if not faq:
        return jsonify({'success': False, 'error': 'FAQ not found'}), 404
        
    try:
        db.session.delete(faq)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# Announcement APIs (Sub Manager)
@bp.route('/api/announcements', methods=['GET'])
@user_management_required
def get_announcements():
    from app import app  # deferred: app-level helper, avoids circular import
    items = Announcement.query.order_by(Announcement.created_at.desc()).all()
    resp = jsonify([a.to_dict() for a in items])
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


def _parse_announcement_payload(data: dict) -> tuple[dict | None, str | None]:
    from app import (  # deferred: app-level helper, avoids circular import
        parse_iso_datetime, parse_jalali_date,
    )
    if not data:
        return None, 'No data provided'

    message = (data.get('message') or '').strip()
    if not message:
        return None, 'Message is required'

    start_at_iso_raw = (data.get('start_at') or '').strip()
    end_at_iso_raw = (data.get('end_at') or '').strip()
    start_at_jalali_raw = (data.get('start_at_jalali') or '').strip()
    end_at_jalali_raw = (data.get('end_at_jalali') or '').strip()

    start_at = parse_iso_datetime(start_at_iso_raw) if start_at_iso_raw else None
    end_at = parse_iso_datetime(end_at_iso_raw) if end_at_iso_raw else None
    if not start_at and start_at_jalali_raw:
        start_at = parse_jalali_date(start_at_jalali_raw)
    if not end_at and end_at_jalali_raw:
        end_at = parse_jalali_date(end_at_jalali_raw)

    if not start_at or not end_at:
        return None, 'Start and End datetime are required'
    if start_at > end_at:
        return None, 'Start datetime must be before End datetime'

    def _parse_int_or_none(val):
        if val is None:
            return None
        try:
            return int(val)
        except Exception:
            try:
                return int(float(str(val).strip()))
            except Exception:
                return None

    raw_targets = data.get('targets')
    all_servers_raw = data.get('all_servers', None)
    server_ids_raw = data.get('server_ids') or []
    if not isinstance(server_ids_raw, list):
        server_ids_raw = []

    normalized_server_ids: list[int] = []
    for sid in server_ids_raw:
        parsed = _parse_int_or_none(sid)
        if parsed is not None:
            normalized_server_ids.append(parsed)
    normalized_server_ids = list(dict.fromkeys(normalized_server_ids))

    def _normalize_targets(raw):
        if raw is None:
            return None
        if raw == '*':
            return '*'
        if isinstance(raw, str):
            trimmed = raw.strip()
            if trimmed == '*':
                return '*'
            if not trimmed:
                return []
            try:
                parsed = json.loads(trimmed)
                return _normalize_targets(parsed)
            except Exception:
                # Back-compat: comma-separated server ids
                ids = []
                for part in trimmed.split(','):
                    parsed_id = _parse_int_or_none(part)
                    if parsed_id is not None:
                        ids.append(parsed_id)
                return [{'server_id': sid, 'inbounds': '*'} for sid in ids]

        entries = raw if isinstance(raw, list) else [raw]
        merged: dict[int, str | set[int]] = {}
        for item in entries:
            server_id = None
            inbounds: str | list[int] = '*'
            if isinstance(item, (int, float, str)):
                server_id = _parse_int_or_none(item)
                inbounds = '*'
            elif isinstance(item, dict):
                server_id = _parse_int_or_none(item.get('server_id') or item.get('server') or item.get('id'))
                raw_inb = item.get('inbounds')
                if raw_inb == '*' or (isinstance(raw_inb, str) and raw_inb.strip() == '*') or raw_inb is None:
                    inbounds = '*'
                elif isinstance(raw_inb, list):
                    inbounds = [v for v in (_parse_int_or_none(x) for x in raw_inb) if v is not None]
                else:
                    one = _parse_int_or_none(raw_inb)
                    inbounds = [] if one is None else [one]

            if not server_id:
                continue

            if server_id not in merged:
                merged[server_id] = '*' if inbounds == '*' else set(inbounds)
            else:
                if merged[server_id] == '*' or inbounds == '*':
                    merged[server_id] = '*'
                else:
                    for v in inbounds:
                        merged[server_id].add(int(v))

        return [
            {'server_id': sid, 'inbounds': '*' if inb == '*' else sorted(list(inb))}
            for sid, inb in merged.items()
        ]

    normalized_targets = _normalize_targets(raw_targets)

    # Back-compat: if targets not provided, derive from all_servers/server_ids
    if normalized_targets is None:
        all_servers = bool(all_servers_raw) if all_servers_raw is not None else True
        if all_servers:
            normalized_targets = '*'
        else:
            normalized_targets = [{'server_id': sid, 'inbounds': '*'} for sid in normalized_server_ids]

    if normalized_targets == '*':
        targets_str = '*'
        all_servers = True
        derived_server_ids: list[int] = []
    else:
        try:
            targets_str = json.dumps(normalized_targets, ensure_ascii=False)
        except Exception:
            targets_str = '[]'
        all_servers = False
        derived_server_ids = []
        for rule in (normalized_targets or []):
            sid = _parse_int_or_none((rule or {}).get('server_id'))
            if sid is not None:
                derived_server_ids.append(sid)
        derived_server_ids = list(dict.fromkeys(derived_server_ids))

    if not all_servers and not derived_server_ids:
        return None, 'Select at least one server (or choose all)'

    payload = {
        'message': message,
        'targets': targets_str,
        'all_servers': all_servers,
        'start_at': start_at,
        'end_at': end_at,
        'server_ids': derived_server_ids,
    }
    return payload, None


@bp.route('/api/announcements', methods=['POST'])
@user_management_required
def create_announcement():
    from app import (  # deferred: app-level helper, avoids circular import
        app, sanitize_html,
    )
    data = request.get_json() or {}
    payload, err = _parse_announcement_payload(data)
    if err:
        return jsonify({'success': False, 'error': err}), 400

    user = db.session.get(Admin, session.get('admin_id')) if session.get('admin_id') else None
    created_by = (getattr(user, 'username', None) or session.get('admin_username') or '').strip() or None

    _ANN_TAGS = ['b','strong','i','em','u','br','p','div','span','ul','ol','li',
                 'a','img','video','source']
    _ANN_ATTRS = {'a': ['href','class','target'], 'span': ['style'], 'div': ['style','class'],
                  'img': ['src','alt','style','width','height'],
                  'video': ['src','controls','style','width','height','preload'],
                  'source': ['src','type'], '*': []}
    _ANN_STYLES = ['color','background-color','font-size','text-align','direction',
                   'max-width','width','height','border-radius','padding','margin',
                   'display','text-decoration','font-weight']
    ann = Announcement(
        message=sanitize_html(payload['message'], tags=_ANN_TAGS, attributes=_ANN_ATTRS, styles=_ANN_STYLES),
        all_servers=payload['all_servers'],
        targets=payload['targets'],
        start_at=payload['start_at'],
        end_at=payload['end_at'],
        created_by=created_by,
        hide_from_resellers=bool(data.get('hide_from_resellers', False)),
        is_popup=bool(data.get('is_popup', False)),
        button_text=(str(data.get('button_text') or '').strip()[:120] or None),
    )

    if not payload['all_servers']:
        servers = Server.query.filter(Server.id.in_(payload['server_ids'])).all() if payload['server_ids'] else []
        ann.servers = servers

    try:
        db.session.add(ann)
        db.session.commit()
        return jsonify({'success': True, 'announcement': ann.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/announcements/<int:announcement_id>', methods=['PUT'])
@user_management_required
def update_announcement(announcement_id):
    from app import (  # deferred: app-level helper, avoids circular import
        app, sanitize_html,
    )
    ann = db.session.get(Announcement, announcement_id)
    if not ann:
        return jsonify({'success': False, 'error': 'Announcement not found'}), 404

    data = request.get_json() or {}
    payload, err = _parse_announcement_payload(data)
    if err:
        return jsonify({'success': False, 'error': err}), 400

    _ANN_TAGS = ['b','strong','i','em','u','br','p','div','span','ul','ol','li',
                 'a','img','video','source']
    _ANN_ATTRS = {'a': ['href','class','target'], 'span': ['style'], 'div': ['style','class'],
                  'img': ['src','alt','style','width','height'],
                  'video': ['src','controls','style','width','height','preload'],
                  'source': ['src','type'], '*': []}
    _ANN_STYLES = ['color','background-color','font-size','text-align','direction',
                   'max-width','width','height','border-radius','padding','margin',
                   'display','text-decoration','font-weight']
    ann.message = sanitize_html(payload['message'], tags=_ANN_TAGS, attributes=_ANN_ATTRS, styles=_ANN_STYLES)
    ann.all_servers = payload['all_servers']
    ann.targets = payload['targets']
    ann.start_at = payload['start_at']
    ann.end_at = payload['end_at']
    if 'hide_from_resellers' in data:
        ann.hide_from_resellers = bool(data['hide_from_resellers'])
    if 'is_popup' in data:
        ann.is_popup = bool(data['is_popup'])
    if 'button_text' in data:
        ann.button_text = (str(data.get('button_text') or '').strip()[:120] or None)

    if ann.all_servers:
        ann.servers = []
    else:
        servers = Server.query.filter(Server.id.in_(payload['server_ids'])).all() if payload['server_ids'] else []
        ann.servers = servers

    try:
        db.session.commit()
        return jsonify({'success': True, 'announcement': ann.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/announcements/<int:announcement_id>', methods=['DELETE'])
@user_management_required
def delete_announcement(announcement_id):
    from app import app  # deferred: app-level helper, avoids circular import
    ann = db.session.get(Announcement, announcement_id)
    if not ann:
        return jsonify({'success': False, 'error': 'Announcement not found'}), 404

    try:
        db.session.delete(ann)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# Online Chat Scripts APIs (Sub Manager)
def _parse_online_chat_payload(data: dict) -> tuple[dict | None, str | None]:
    if not data:
        return None, 'No data provided'

    name = (data.get('name') or '').strip()
    script_code = (data.get('script_code') or '').strip()

    if not name:
        return None, 'Name is required'
    if not script_code:
        return None, 'Script code is required'
    if len(name) > 120:
        return None, 'Name is too long'
    if len(script_code) > 50000:
        return None, 'Script code is too long'

    return {
        'name': name,
        'script_code': script_code,
    }, None


@bp.route('/api/online-chat-scripts', methods=['GET'])
@user_management_required
def get_online_chat_scripts():
    from app import app  # deferred: app-level helper, avoids circular import
    items = OnlineChatScript.query.order_by(OnlineChatScript.created_at.desc()).all()
    return jsonify([item.to_dict() for item in items])


@bp.route('/api/online-chat-scripts', methods=['POST'])
@user_management_required
def create_online_chat_script():
    from app import (  # deferred: app-level helper, avoids circular import
        app, sanitize_html,
    )
    data = request.get_json() or {}
    payload, err = _parse_online_chat_payload(data)
    if err:
        return jsonify({'success': False, 'error': err}), 400

    user = db.session.get(Admin, session.get('admin_id')) if session.get('admin_id') else None
    created_by = (getattr(user, 'username', None) or session.get('admin_username') or '').strip() or None

    item = OnlineChatScript(
        name=sanitize_html(payload['name']),
        script_code=payload['script_code'],
        is_active=False,
        created_by=created_by,
    )

    try:
        db.session.add(item)
        db.session.commit()
        return jsonify({'success': True, 'item': item.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/online-chat-scripts/<int:item_id>', methods=['PUT'])
@user_management_required
def update_online_chat_script(item_id):
    from app import (  # deferred: app-level helper, avoids circular import
        app, sanitize_html,
    )
    item = db.session.get(OnlineChatScript, item_id)
    if not item:
        return jsonify({'success': False, 'error': 'Script not found'}), 404

    data = request.get_json() or {}
    payload, err = _parse_online_chat_payload(data)
    if err:
        return jsonify({'success': False, 'error': err}), 400

    item.name = sanitize_html(payload['name'])
    item.script_code = payload['script_code']

    try:
        db.session.commit()
        return jsonify({'success': True, 'item': item.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/online-chat-scripts/<int:item_id>', methods=['DELETE'])
@user_management_required
def delete_online_chat_script(item_id):
    from app import app  # deferred: app-level helper, avoids circular import
    item = db.session.get(OnlineChatScript, item_id)
    if not item:
        return jsonify({'success': False, 'error': 'Script not found'}), 404

    try:
        db.session.delete(item)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/online-chat-scripts/<int:item_id>/activate', methods=['POST'])
@user_management_required
def activate_online_chat_script(item_id):
    from app import app  # deferred: app-level helper, avoids circular import
    item = db.session.get(OnlineChatScript, item_id)
    if not item:
        return jsonify({'success': False, 'error': 'Script not found'}), 404

    try:
        OnlineChatScript.query.update({OnlineChatScript.is_active: False})
        item.is_active = True
        db.session.commit()
        return jsonify({'success': True, 'item': item.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
