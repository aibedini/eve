"""Client management API routes (extracted from app.py)."""
import base64
import copy
import io
import json
import math
import re
import secrets
import string
import threading
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse

import qrcode
import requests
from flask import Blueprint, jsonify, request, session
from sqlalchemy import func, or_

from panel.extensions import db, limiter
from panel.models import (
    Admin, ClientOwnership, NotificationTemplate, Package, RenewTemplate,
    Server, ServiceOwnership, Transaction, VolumeRulePreset,
)
from panel.routes.common import login_required

bp = Blueprint('clients', __name__)


@bp.route('/api/clients/search')
@login_required
@limiter.limit("60 per minute")
def global_client_search():
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_SERVER_DATA, get_accessible_servers, get_reseller_access_maps,
        is_inbound_accessible,
    )
    user = db.session.get(Admin, session['admin_id'])
    query = (request.args.get('email') or '').strip().lower()
    if not query:
        return jsonify({"success": False, "error": "Query parameter 'email' is required"}), 400

    try:
        limit = int(request.args.get('limit', 500))
    except ValueError:
        limit = 500
    limit = max(1, min(limit, 5000))

    # --- اصلاح حرفه‌ای: جستجو در کش (RAM) به جای درخواست مجدد ---
    
    # اگر کش خالی است (برنامه تازه اجرا شده)، پیام مناسب بدهد
    if not GLOBAL_SERVER_DATA.get('inbounds'):
        return jsonify({"success": True, "results": [], "errors": ["System is starting up, please wait..."]})

    matches = []
    
    # دریافت دسترسی‌های کاربر برای فیلتر کردن نتایج
    accessible_servers = get_accessible_servers(user)
    accessible_server_ids = {s.id for s in accessible_servers}
    
    # تنظیمات دسترسی ریسلر
    allowed_map = '*'
    assignments = {}
    if user.role == 'reseller':
        allowed_map, assignments = get_reseller_access_maps(user)

    # جستجو در داده‌های موجود در رم
    for inbound in GLOBAL_SERVER_DATA['inbounds']:
        sid = inbound.get('server_id')
        iid = inbound.get('id')

        # 1. بررسی دسترسی به سرور
        if sid not in accessible_server_ids:
            continue

        # 2. بررسی دسترسی به اینباند (مخصوص ریسلرها)
        if user.role == 'reseller':
            if not is_inbound_accessible(sid, iid, allowed_map, assignments):
                continue

        # 3. جستجو در کلاینت‌های این اینباند
        clients = inbound.get('clients', [])
        for client in clients:
            c_email = (client.get('email') or '').lower()
            c_comment = (client.get('comment') or '').lower()
            if query not in c_email and query not in c_comment:
                continue
            # کلاینت پیدا شد
            matches.append({
                "server_id": sid,
                "server_name": inbound.get('server_name'),
                "panel_type": next((s.panel_type for s in accessible_servers if s.id == sid), 'auto'),
                "inbound_id": iid,
                "inbound": {
                    "id": iid,
                    "remark": inbound.get('remark', ''),
                    "port": inbound.get('port', ''),
                    "protocol": inbound.get('protocol', ''),
                    "enable": inbound.get('enable', False)
                },
                "client": client
            })

            if len(matches) >= limit:
                break
        
        if len(matches) >= limit:
            break

    return jsonify({"success": True, "results": matches, "errors": []})


@bp.route('/api/client/<int:server_id>/<int:inbound_id>/toggle', methods=['POST'])
@login_required
def toggle_client(server_id, inbound_id):
    from app import _toggle_client_core  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 401

    server = Server.query.get_or_404(server_id)

    try:
        data = request.get_json() or {}
        email = data.get('email')
        enable = data.get('enable', True)
        if not email:
            return jsonify({"success": False, "error": "Email required"}), 400
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON"}), 400

    ok, error_message, status_code = _toggle_client_core(user, server, inbound_id, email, enable)
    if ok:
        response = {"success": True}
        if user.role == 'reseller':
            response["remaining_credit"] = user.credit
        return jsonify(response)
    return jsonify({"success": False, "error": error_message}), status_code


@bp.route('/api/client/<int:server_id>/<int:inbound_id>/reset', methods=['POST'])
@login_required
def reset_client_traffic(server_id, inbound_id):
    from app import (  # deferred: app-level helper, avoids circular import
        CLIENT_RESET_FALLBACKS, CLIENT_UPDATE_FALLBACKS, _has_client_access, _json_field,
        _push_full_inbound, _reseller_can_create_free, _user_can_afford, app, build_panel_url,
        calculate_reseller_price, collect_endpoint_templates, fetch_inbounds, find_client,
        get_config, get_xui_session, log_transaction, patch_cached_client, server_is_v3,
        v3_reset_client, v3_update_client,
    )
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 401
    
    server = Server.query.get_or_404(server_id)
    
    try:
        data = request.get_json() or {}
    except:
        return jsonify({"success": False, "error": "Invalid JSON"}), 400

    email = data.get('email')
    if not email:
        return jsonify({"success": False, "error": "Email required"}), 400
    try:
        volume_gb = int(data.get('volume_gb', 0) or 0)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Invalid volume value"}), 400
    if volume_gb < 0:
        volume_gb = 0
    
    base_cost_gb = get_config('cost_per_gb', 0)
    user_cost_gb = calculate_reseller_price(user, base_price=base_cost_gb, cost_type='gb')
    
    is_free = bool(data.get('is_free', False))
    if is_free and not _reseller_can_create_free(user):
        return jsonify({"success": False, "error": "Free creation is not permitted for your account"}), 403
    if is_free:
        charge_amount = 0
    else:
        charge_amount = volume_gb * user_cost_gb if volume_gb > 0 else 0

    if user.role == 'reseller':
        if not _has_client_access(user, server_id, email, inbound_id=inbound_id):
            return jsonify({"success": False, "error": "Access denied"}), 403
        if not is_free and user_cost_gb > 0 and volume_gb <= 0:
            return jsonify({"success": False, "error": "Billable volume required"}), 400
        ok, err = _user_can_afford(user, charge_amount)
        if not ok:
            return jsonify({"success": False, "error": err}), 402

    session_obj, error = get_xui_session(server)
    if error: return jsonify({"success": False, "error": error}), 400

    def _apply_volume_cap_after_reset(target_client):
        """After a successful traffic reset, set totalGB if caller specified a volume cap."""
        if volume_gb <= 0:
            return
        target_client['totalGB'] = volume_gb * 1024 * 1024 * 1024
        try:
            if server_is_v3(server):
                v3_update_client(server, session_obj, email, target_client)
            elif 'id' not in target_client:
                # Shadowsocks: need full inbound update
                _ibs_r, _fe_r, _ = fetch_inbounds(session_obj, server.host, server.panel_type)
                _full_ib_r = None
                if not _fe_r:
                    for _ib_r in (_ibs_r or []):
                        if _ib_r.get('id') == inbound_id:
                            _full_ib_r = _ib_r
                            break
                if _full_ib_r:
                    _fs_r = _json_field(_full_ib_r.get('settings'), {})
                    _fs_r['clients'] = [
                        target_client if c.get('email') == email else c
                        for c in _fs_r.get('clients', [])
                    ]
                    _push_full_inbound(server, session_obj, _full_ib_r, _fs_r)
            else:
                client_id = target_client.get('id', target_client.get('password', email))
                up = {'id': inbound_id, 'settings': json.dumps({'clients': [target_client]})}
                rpl = {'id': inbound_id, 'inbound_id': inbound_id, 'inboundId': inbound_id,
                       'clientId': client_id, 'client_id': client_id, 'email': email}
                for tpl in collect_endpoint_templates(server.panel_type, 'client_update', CLIENT_UPDATE_FALLBACKS):
                    url2 = build_panel_url(server.host, tpl, rpl)
                    if not url2:
                        continue
                    r2 = session_obj.post(url2, json=up, verify=False, timeout=10)
                    if r2.status_code == 200:
                        break
        except Exception as exc:
            app.logger.warning("apply_volume_cap_after_reset failed for %s: %s", email, exc)

    try:
        # v3: reset the first-class client by email (legacy resetClientTraffic is 404).
        if server_is_v3(server):
            ok, _vr, verr = v3_reset_client(server, session_obj, email)
            if not ok:
                return jsonify({"success": False, "error": f"v3 reset failed: {verr}"}), 502

            if volume_gb > 0:
                inbounds_r, _, _ = fetch_inbounds(session_obj, server.host, server.panel_type)
                target_r, _ = find_client(inbounds_r, inbound_id, email)
                if target_r:
                    _apply_volume_cap_after_reset(target_r)

            if charge_amount > 0:
                sender_card = data.get('sender_card', '') or ''
                card_id = data.get('card_id')
                if user.role == 'reseller':
                    user.credit -= charge_amount
                    log_transaction(user.id, -charge_amount, 'reset_traffic', "Reset traffic (Credit Usage)", server_id=server.id, sender_card=sender_card, card_id=card_id, category='usage', client_email=email)
                else:
                    log_transaction(user.id, charge_amount, 'reset_traffic', "Reset traffic (Income)", server_id=server.id, sender_card=sender_card, card_id=card_id, category='income', client_email=email)
                db.session.commit()
            patch_cached_client(server.id, email, up=0, down=0,
                                total_gb_bytes=(volume_gb * 1024 * 1024 * 1024 if volume_gb > 0 else None))
            response = {"success": True}
            if user.role == 'reseller':
                response["remaining_credit"] = user.credit
            return jsonify(response)

        templates = collect_endpoint_templates(server.panel_type, 'client_reset_traffic', CLIENT_RESET_FALLBACKS)
        replacements = {
            'id': inbound_id,
            'inbound_id': inbound_id,
            'inboundId': inbound_id,
            'email': email
        }
        errors = []
        for template in templates:
            full_url = build_panel_url(server.host, template, replacements)
            if not full_url:
                continue
            requires_path_email = (':email' in template) or ('{email}' in template)
            payload = None if requires_path_email else {"email": email}
            try:
                if payload is None:
                    resp = session_obj.post(full_url, verify=False, timeout=10)
                else:
                    resp = session_obj.post(full_url, json=payload, verify=False, timeout=10)
            except Exception as exc:
                errors.append(f"{template}: {exc}")
                continue

            if resp.status_code == 200:
                try:
                    resp_json = resp.json()
                    if isinstance(resp_json, dict) and resp_json.get('success') is False:
                        errors.append(f"{template}: success false")
                        continue
                except ValueError:
                    pass

                if volume_gb > 0:
                    inbounds_r, _, _ = fetch_inbounds(session_obj, server.host, server.panel_type)
                    target_r, _ = find_client(inbounds_r, inbound_id, email)
                    if target_r:
                        _apply_volume_cap_after_reset(target_r)

                if charge_amount > 0:
                    sender_card = data.get('sender_card', '') or ''
                    card_id = data.get('card_id')
                    if user.role == 'reseller':
                        user.credit -= charge_amount
                        log_transaction(user.id, -charge_amount, 'reset_traffic', "Reset traffic (Credit Usage)", server_id=server.id, sender_card=sender_card, card_id=card_id, category='usage', client_email=email)
                    else:
                        log_transaction(user.id, charge_amount, 'reset_traffic', "Reset traffic (Income)", server_id=server.id, sender_card=sender_card, card_id=card_id, category='income', client_email=email)
                    db.session.commit()

                patch_cached_client(server.id, email, up=0, down=0,
                                    total_gb_bytes=(volume_gb * 1024 * 1024 * 1024 if volume_gb > 0 else None))
                response = {"success": True}
                if user.role == 'reseller':
                    response["remaining_credit"] = user.credit
                return jsonify(response)

            errors.append(f"{template}: {resp.status_code}")
            if resp.status_code != 404:
                break

        app.logger.warning("Reset traffic failed for %s: %s", email, '; '.join(errors))
        return jsonify({"success": False, "error": "Reset endpoint returned error"}), 400
    except Exception as e:
        app.logger.error("Reset error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route('/api/client/<int:server_id>/<int:inbound_id>/<email>/edit', methods=['POST'])
@login_required
def edit_client(server_id, inbound_id, email):
    from app import (  # deferred: app-level helper, avoids circular import
        CLIENT_UPDATE_FALLBACKS, GLOBAL_REFRESH_LOCK, GLOBAL_SERVER_DATA, _get_panel_ui_lang,
        _has_client_access, _json_field, _push_full_inbound, _v3_sanitize_email, app,
        build_panel_url, collect_endpoint_templates, fetch_inbounds, find_client,
        get_xui_session, patch_cached_client, persist_detected_panel_type, server_is_v3,
        v3_update_client,
    )
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 401
    
    server = Server.query.get_or_404(server_id)
    
    # Check ownership for resellers
    if user.role == 'reseller':
        if not _has_client_access(user, server_id, email, inbound_id=inbound_id):
            return jsonify({"success": False, "error": "Access denied"}), 403
    
    try:
        data = request.get_json() or {}
        new_email = data.get('new_email', '').strip()
        new_total_gb = data.get('totalGB')
        new_expiry_time = data.get('expiryTime')
        new_comment = data.get('comment')
    except:
        return jsonify({"success": False, "error": "Invalid data"}), 400

    if not new_email:
        return jsonify({"success": False, "error": "New email is required"}), 400
        
    session_obj, error = get_xui_session(server)
    if error:
        return jsonify({"success": False, "error": error}), 400
        
    try:
        # The dashboard cache already carries the panel's raw client object. For
        # the common v3 field-edit case (no rename), reuse it and avoid a full,
        # slow inbounds read before the actual update. Renames still fetch the
        # authoritative server list so duplicate-email validation stays strict.
        inbounds = None
        target_client = None
        fetched_inbound_row_edit = None
        if (server_is_v3(server) and new_email == email
                and _v3_sanitize_email(email) == email):
            try:
                with GLOBAL_REFRESH_LOCK:
                    for ib in (GLOBAL_SERVER_DATA.get('inbounds') or []):
                        if (int(ib.get('server_id', -1)) != int(server_id)
                                or int(ib.get('id', -1)) != int(inbound_id)):
                            continue
                        for cached_client in (ib.get('clients') or []):
                            if ((cached_client.get('email') or '').strip().lower()
                                    == (email or '').strip().lower()):
                                raw = cached_client.get('raw_client')
                                if isinstance(raw, dict):
                                    target_client = copy.deepcopy(raw)
                                    break
                        if target_client:
                            break
            except Exception:
                target_client = None

        if not target_client:
            inbounds, fetch_err, detected_type = fetch_inbounds(session_obj, server.host, server.panel_type)
            if fetch_err:
                return jsonify({"success": False, "error": "Failed to fetch inbounds"}), 400
            persist_detected_panel_type(server, detected_type)
            target_client, fetched_inbound_row_edit = find_client(inbounds, inbound_id, email)

        if not target_client and server_is_v3(server):
            # Spaced-name client: panel stores it space-free → retry sanitized.
            _ce = _v3_sanitize_email(email)
            if _ce and _ce != email:
                target_client, fetched_inbound_row_edit = find_client(inbounds, inbound_id, _ce)
                if target_client:
                    email = _ce
        if not target_client:
            return jsonify({"success": False, "error": "Client not found"}), 404

        # v3.4+ rejects any client email containing a space (validateClientEmail),
        # so the edit/rename silently fails. Force the new email space-free first,
        # then apply the rest of the edit — "fix the name, then do whatever".
        if server_is_v3(server):
            _clean_new = _v3_sanitize_email(new_email)
            if _clean_new:
                new_email = _clean_new

        # Check for duplicate email on the same server (excluding current client)
        if new_email != email:
            for ib in (inbounds or []):
                settings = _json_field(ib.get('settings'), {})
                clients = settings.get('clients', [])
                for c in clients:
                    if c.get('email') == new_email:
                        return jsonify({"success": False, "error": f"Client with email '{new_email}' already exists on this server."}), 400

        # Extract ID before modification to ensure we target the correct client
        client_id = target_client.get('id', target_client.get('password', email))

        # Update email
        target_client['email'] = new_email

        # Comment can be edited by anyone
        if new_comment is not None:
            target_client['comment'] = new_comment

        # Only superadmin can edit volume and expiry
        if user.is_superadmin:
            if new_total_gb is not None:
                try:
                    target_client['totalGB'] = int(float(new_total_gb) * 1024 * 1024 * 1024)
                except (ValueError, TypeError):
                    pass
            if new_expiry_time is not None:
                try:
                    target_client['expiryTime'] = int(new_expiry_time)
                except (ValueError, TypeError):
                    pass

        # v3: use first-class client endpoint (legacy updateClient is 404 on v3)
        if server_is_v3(server):
            ok_v3, _vr, verr = v3_update_client(server, session_obj, email, target_client)
            if not ok_v3:
                detail = verr or 'panel rejected update'
                app.logger.warning("v3 edit client failed for %s: %s", email, detail)
                prefix = 'پنل خطا برگرداند' if _get_panel_ui_lang() == 'fa' else 'The panel returned an error'
                return jsonify({"success": False, "error": f"{prefix}: {detail}"}), 502
            success = True
        elif 'id' not in target_client:
            # Shadowsocks clients have no UUID — use full inbound update.
            _full_ib_edit = fetched_inbound_row_edit
            if _full_ib_edit is None:
                return jsonify({"success": False, "error": "shadowsocks: could not get full inbound for update"}), 400
            _full_settings_edit = _json_field(_full_ib_edit.get('settings'), {})
            _full_settings_edit['clients'] = [
                target_client if c.get('email') == email else c
                for c in _full_settings_edit.get('clients', [])
            ]
            _ok_push_edit, _push_err_edit = _push_full_inbound(server, session_obj, _full_ib_edit, _full_settings_edit)
            if not _ok_push_edit:
                detail = _push_err_edit or 'shadowsocks inbound update failed'
                app.logger.warning("Edit client failed for %s: %s", email, detail)
                prefix = 'آپدیت ناموفق بود' if _get_panel_ui_lang() == 'fa' else 'Update failed'
                return jsonify({"success": False, "error": f"{prefix} — {detail}"}), 400
            success = True
        else:
            update_payload = {
                "id": inbound_id,
                "settings": json.dumps({"clients": [target_client]})
            }

            replacements = {
                'id': inbound_id,
                'inbound_id': inbound_id,
                'inboundId': inbound_id,
                'clientId': client_id,
                'client_id': client_id,
                'email': email
            }

            templates = collect_endpoint_templates(server.panel_type, 'client_update', CLIENT_UPDATE_FALLBACKS)
            errors = []
            success = False

            for template in templates:
                full_url = build_panel_url(server.host, template, replacements)
                if not full_url:
                    continue
                try:
                    resp = session_obj.post(full_url, json=update_payload, verify=False, timeout=10)
                except Exception as exc:
                    errors.append(f"{template}: {exc}")
                    continue

                if resp.status_code == 200:
                    try:
                        resp_json = resp.json()
                        if isinstance(resp_json, dict) and resp_json.get('success') is False:
                            panel_msg = resp_json.get('msg') or resp_json.get('message') or 'success=false'
                            errors.append(f"{template}: {panel_msg}")
                            continue
                    except ValueError:
                        pass

                    success = True
                    break

                errors.append(f"{template}: HTTP {resp.status_code}")

            if not success:
                detail = '; '.join(errors) or 'no endpoint succeeded'
                app.logger.warning("Edit client failed for %s: %s", email, detail)
                prefix = 'آپدیت ناموفق بود' if _get_panel_ui_lang() == 'fa' else 'Update failed'
                return jsonify({"success": False, "error": f"{prefix} — {detail}"}), 400

        if success:
            # Update ownership if exists
            email_l = (email or '').strip().lower()
            ownerships = ClientOwnership.query.filter(
                ClientOwnership.server_id == server_id,
                or_(
                    ClientOwnership.client_uuid == str(client_id),
                    func.lower(ClientOwnership.client_email) == email_l,
                )
            ).all()
            for own in ownerships:
                own.client_email = new_email
                if (not (own.client_uuid or '').strip()) and str(client_id):
                    own.client_uuid = str(client_id)
            db.session.commit()

            # Write-through cache: reflect the edit instantly (no panel re-fetch).
            try:
                _tg = None
                _ex = None
                if user.is_superadmin:
                    if new_total_gb is not None:
                        _tg = int(float(new_total_gb) * 1024 * 1024 * 1024)
                    if new_expiry_time is not None:
                        _ex = int(new_expiry_time)
                patch_cached_client(
                    server_id, email, client_uuid=str(client_id) if client_id else None,
                    new_email=(new_email if new_email != email else None),
                    comment=(new_comment if new_comment is not None else None),
                    total_gb_bytes=_tg, expiry_ts=_ex)
            except Exception:
                pass

            return jsonify({"success": True})

    except Exception as e:
        app.logger.error("Edit client error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route('/api/client/<int:server_id>/<email>/inbounds', methods=['POST'])
@login_required
def set_client_inbounds(server_id, email):
    """Change which inbounds a v3 client is assigned to (add/replace/remove)."""
    from app import (  # deferred: app-level helper, avoids circular import
        _reconcile_client_inbounds,
    )
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 401

    server = Server.query.get_or_404(server_id)

    data = request.get_json(silent=True) or {}
    mode = (data.get('mode') or 'set').lower()
    inbound_ids = data.get('inbound_ids') or []
    client_uuid = (data.get('client_uuid') or '').strip()

    ok, err, status, info = _reconcile_client_inbounds(
        user, server, email, client_uuid, inbound_ids, mode)
    if ok:
        return jsonify({"success": True, "info": info or {}})
    code = status if (isinstance(status, int) and status >= 400) else 400
    return jsonify({"success": False, "error": err or "Failed"}), code


@bp.route('/api/client/<int:server_id>/<int:inbound_id>/<email>/delete', methods=['POST'])
@login_required
def delete_client(server_id, inbound_id, email):
    from app import _delete_client_core, app  # deferred: app-level helper, avoids circular import
    try:
        user = db.session.get(Admin, session['admin_id'])
        if not user:
            return jsonify({"success": False, "error": "User not found"}), 401

        server = Server.query.get_or_404(server_id)

        ok, error_message, status_code = _delete_client_core(user, server, inbound_id, email)
        if ok:
            return jsonify({"success": True})
        return jsonify({"success": False, "error": error_message}), status_code
    except Exception as exc:
        app.logger.error("Unhandled delete_client error: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": f"Server error: {exc}"}), 500


@bp.route('/api/volume-rule-presets', methods=['GET'])
@login_required
def list_volume_rule_presets():
    """Return volume-filter presets visible to the current user (own + global)."""
    user = db.session.get(Admin, session['admin_id'])
    q = VolumeRulePreset.query
    if user and user.role == 'reseller':
        q = q.filter((VolumeRulePreset.owner_id == user.id) | (VolumeRulePreset.owner_id == None))  # noqa: E711
    presets = q.order_by(VolumeRulePreset.created_at.desc()).all()
    return jsonify({'success': True, 'presets': [p.to_dict() for p in presets]})


@bp.route('/api/volume-rule-presets', methods=['POST'])
@login_required
def save_volume_rule_preset():
    """Create or overwrite (by same name + owner) a volume-filter preset."""
    user = db.session.get(Admin, session['admin_id'])
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    rules = data.get('rules')
    if not name:
        return jsonify({'success': False, 'error': 'Name is required'}), 400
    if not isinstance(rules, list) or not rules:
        return jsonify({'success': False, 'error': 'At least one rule is required'}), 400

    rules_json = json.dumps(rules, ensure_ascii=False)
    # Overwrite an existing preset with the same name for this owner
    existing = VolumeRulePreset.query.filter_by(name=name, owner_id=user.id).first()
    if existing:
        existing.rules = rules_json
        preset = existing
    else:
        preset = VolumeRulePreset(name=name, rules=rules_json, owner_id=user.id)
        db.session.add(preset)
    db.session.commit()
    return jsonify({'success': True, 'preset': preset.to_dict()})


@bp.route('/api/volume-rule-presets/<int:preset_id>', methods=['DELETE'])
@login_required
def delete_volume_rule_preset(preset_id):
    user = db.session.get(Admin, session['admin_id'])
    preset = db.session.get(VolumeRulePreset, preset_id)
    if not preset:
        return jsonify({'success': False, 'error': 'Preset not found'}), 404
    # Only the owner (or superadmin) can delete
    if preset.owner_id != user.id and not session.get('is_superadmin', False):
        return jsonify({'success': False, 'error': 'Access denied'}), 403
    db.session.delete(preset)
    db.session.commit()
    return jsonify({'success': True})


@bp.route('/api/client/bulk', methods=['POST'])
@login_required
def bulk_client_action():
    from app import (  # deferred: app-level helper, avoids circular import
        BULK_JOBS, BULK_JOBS_CLIENTS, BULK_JOBS_LOCK, _load_bulk_jobs_locked, _parse_bool,
        _prune_bulk_jobs_locked, _run_bulk_job, _save_bulk_jobs_locked, _summarize_bulk_job,
        _utc_iso_now,
    )
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 401

    try:
        payload = request.get_json() or {}
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON"}), 400

    wait_for_completion = _parse_bool(request.args.get('wait')) or _parse_bool(payload.get('wait'))

    action = payload.get('action')
    clients = payload.get('clients')
    data = payload.get('data') or {}
    conditions = payload.get('conditions') or {}

    allowed_actions = {'enable', 'disable', 'delete', 'assign_owner', 'unassign_owner', 'add_days', 'add_volume', 'volume_policy', 'volume_multiplier', 'set_start_after_use', 'set_inbounds'}
    if action not in allowed_actions:
        return jsonify({"success": False, "error": "Invalid action"}), 400
    if not isinstance(clients, list) or len(clients) == 0:
        return jsonify({"success": False, "error": "Clients list required"}), 400

    if action == 'set_inbounds':
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Invalid data"}), 400
        _mode = str(data.get('inbound_mode') or 'set').strip().lower()
        if _mode not in ('set', 'add', 'remove'):
            return jsonify({"success": False, "error": "inbound_mode must be set, add or remove"}), 400
        if not isinstance(data.get('inbound_ids'), list) or len(data.get('inbound_ids') or []) == 0:
            return jsonify({"success": False, "error": "inbound_ids required"}), 400

    reseller_id = None
    if action in ('assign_owner', 'unassign_owner'):
        if session.get('role') == 'reseller':
            return jsonify({"success": False, "error": "Access denied"}), 403

    if action in ('add_days', 'add_volume', 'volume_policy', 'volume_multiplier'):
        # Basic payload validation here; deep validation happens in the worker.
        if not isinstance(data, dict):
            return jsonify({"success": False, "error": "Invalid data"}), 400
        if action == 'add_days':
            if 'days_delta' not in data:
                return jsonify({"success": False, "error": "days_delta required"}), 400
        if action == 'add_volume':
            if 'volume_gb_delta' not in data:
                return jsonify({"success": False, "error": "volume_gb_delta required"}), 400
        if action == 'volume_policy':
            if not isinstance(data.get('volume_rules'), list) or len(data.get('volume_rules') or []) == 0:
                return jsonify({"success": False, "error": "volume_rules required"}), 400
        if action == 'volume_multiplier':
            try:
                factor = float(data.get('factor', 0) or 0)
            except (TypeError, ValueError):
                factor = 0
            if factor <= 0:
                return jsonify({"success": False, "error": "factor must be > 0"}), 400
            mode = str(data.get('mode') or 'set_remaining').strip().lower()
            if mode not in ('set_remaining', 'reset_and_set'):
                return jsonify({"success": False, "error": "mode must be set_remaining or reset_and_set"}), 400
            # Optional skip_min_gb / skip_max_gb — must be non-negative if provided
            for _skk in ('skip_min_gb', 'skip_max_gb'):
                _skv = data.get(_skk)
                if _skv is not None:
                    try:
                        _skf = float(_skv)
                    except (TypeError, ValueError):
                        return jsonify({"success": False, "error": f"{_skk} must be a number"}), 400
                    if _skf < 0:
                        return jsonify({"success": False, "error": f"{_skk} must be >= 0"}), 400

    if conditions is not None and not isinstance(conditions, dict):
        return jsonify({"success": False, "error": "Invalid conditions"}), 400

    if action == 'assign_owner':
        reseller_id = data.get('reseller_id')
        try:
            reseller_id = int(reseller_id)
        except (TypeError, ValueError):
            reseller_id = None
        if not reseller_id:
            return jsonify({"success": False, "error": "reseller_id required"}), 400
        reseller = db.session.get(Admin, reseller_id)
        if not reseller or reseller.role != 'reseller':
            return jsonify({"success": False, "error": "Invalid reseller"}), 400

    # Enqueue as an async job so the UI can show progress.
    # The (potentially huge) client list is kept in memory only — never written
    # to the shared JSON file — so disk writes stay tiny and fast at any scale.
    job_id = secrets.token_hex(8)
    job = {
        'id': job_id,
        'state': 'queued',
        'action': action,
        'data': data,
        'conditions': conditions,
        'user_id': user.id,
        'created_at': _utc_iso_now(),
        'created_at_ts': time.time(),
        'started_at': None,
        'finished_at': None,
        'progress': {
            'total': len(clients),
            'processed': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0,
        },
        'errors': [],
        'report_rows': [],
        'report_rules': data.get('volume_rules') if action == 'volume_policy' else None,
        'error': None,
    }
    with BULK_JOBS_LOCK:
        _load_bulk_jobs_locked()
        BULK_JOBS[job_id] = job
        BULK_JOBS_CLIENTS[job_id] = clients
        _save_bulk_jobs_locked()
        _prune_bulk_jobs_locked()

    if wait_for_completion:
        _run_bulk_job(job_id)
        with BULK_JOBS_LOCK:
            _load_bulk_jobs_locked()
            done_job = BULK_JOBS.get(job_id)
            summary = _summarize_bulk_job(done_job) if done_job else None
        return jsonify({'success': True, 'job_id': job_id, 'done': True, 'job': summary})

    t = threading.Thread(target=_run_bulk_job, args=(job_id,), daemon=True)
    t.start()
    return jsonify({'success': True, 'job_id': job_id})


@bp.route('/api/client/bulk/job/<job_id>', methods=['GET'])
@login_required
@limiter.exempt
def bulk_client_job(job_id):
    from app import (  # deferred: app-level helper, avoids circular import
        BULK_JOBS, BULK_JOBS_LOCK, _load_bulk_jobs_locked, _summarize_bulk_job,
    )
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 401

    with BULK_JOBS_LOCK:
        _load_bulk_jobs_locked()
        job = BULK_JOBS.get(job_id)
        if not job:
            return jsonify({'success': False, 'error': 'Job not found'}), 404

        # Simple access control: only the job owner or superadmin can view
        try:
            if int(job.get('user_id') or 0) != int(user.id) and not session.get('is_superadmin', False):
                return jsonify({'success': False, 'error': 'Access denied'}), 403
        except Exception:
            if not session.get('is_superadmin', False):
                return jsonify({'success': False, 'error': 'Access denied'}), 403

        resp = jsonify({'success': True, 'job': _summarize_bulk_job(job)})
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        return resp


@bp.route('/api/client/<email>/last-renewal', methods=['GET'])
@login_required
def client_last_renewal(email):
    """Return the most recent renewal transaction(s) for a client email so the
    operator can avoid charging the same account twice."""
    from app import format_jalali  # deferred: app-level helper, avoids circular import
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    email_l = (email or '').strip()
    if not email_l:
        return jsonify({'success': True, 'renewals': []})

    # Match space-insensitively: v3 renames spaced emails on the panel, so old
    # transactions may be stored under the spaced email while the modal now
    # queries with the clean one (or vice versa). Normalize both sides.
    email_norm = email_l.replace(' ', '').lower()

    q = Transaction.query.filter(
        func.replace(func.lower(Transaction.client_email), ' ', '') == email_norm,
        Transaction.type == 'renew',
    )
    # Resellers only see their own transactions
    if user.role == 'reseller':
        q = q.filter(Transaction.admin_id == user.id)

    rows = q.order_by(Transaction.created_at.desc()).limit(3).all()

    renewals = []
    now = datetime.utcnow()
    for t in rows:
        created = t.created_at
        days_ago = None
        hours_ago = None
        if created:
            delta = now - created
            days_ago = delta.days
            hours_ago = int(delta.total_seconds() // 3600)
        card_label = ''
        try:
            if t.card:
                card_label = t.card.label or t.card.masked_card() or ''
        except Exception:
            card_label = ''
        renewals.append({
            'id': t.id,
            'amount': t.amount,
            'date_jalali': format_jalali(created) if created else None,
            'date_iso': created.isoformat() if created else None,
            'days_ago': days_ago,
            'hours_ago': hours_ago,
            'sender_card': t.sender_card or '',
            'dest_card': card_label,
            'description': t.description or '',
            'admin_username': (t.admin.username if getattr(t, 'admin', None) else ''),
        })

    # Gift history: gift renewals carry the Royalty marker in their description.
    # (gifts are conventionally given once, so the operator should be warned.)
    last_gift = None
    gift_count = 0
    try:
        gq = Transaction.query.filter(
            func.replace(func.lower(Transaction.client_email), ' ', '') == email_norm,
            or_(
                Transaction.description.like('%هدیه رویالتی%'),
                Transaction.description.like('%for Royalty%'),
            ),
        )
        if user.role == 'reseller':
            gq = gq.filter(Transaction.admin_id == user.id)
        gift_rows = gq.order_by(Transaction.created_at.desc()).all()
        gift_count = len(gift_rows)
        if gift_rows:
            g = gift_rows[0]
            gb = None
            m = re.search(r'\+\s*(\d+)\s*(?:GB|گیگ)', g.description or '')
            if m:
                try:
                    gb = int(m.group(1))
                except Exception:
                    gb = None
            gcreated = g.created_at
            last_gift = {
                'date_jalali': format_jalali(gcreated) if gcreated else None,
                'date_iso': gcreated.isoformat() if gcreated else None,
                'days_ago': (now - gcreated).days if gcreated else None,
                'gift_gb': gb,
                'admin_username': (g.admin.username if getattr(g, 'admin', None) else ''),
            }
    except Exception:
        last_gift = None
        gift_count = 0

    return jsonify({'success': True, 'renewals': renewals, 'last_gift': last_gift, 'gift_count': gift_count})


def _acquire_renew_lock(key: str, ttl: int = 45) -> bool:
    """Best-effort cross-worker lock so a slow v3.4 renew (panel takes 10-18s to
    push to the node) can't be charged / SMS'd twice when the operator retries.
    Returns True if acquired (or when Redis is unavailable — don't block renews)."""
    from app import get_redis  # deferred: app-level helper, avoids circular import
    r = get_redis()
    if r is None:
        return True
    try:
        return bool(r.set(key, '1', nx=True, ex=ttl))
    except Exception:
        return True


def _release_renew_lock(key: str) -> None:
    from app import get_redis  # deferred: app-level helper, avoids circular import
    r = get_redis()
    if r is None:
        return
    try:
        r.delete(key)
    except Exception:
        pass


def _renew_result_key(lock_key: str) -> str:
    return lock_key.replace('renew:lock:', 'renew:result:', 1)


def _clear_renew_result(lock_key: str) -> None:
    """Discard the previous completed result when a genuinely new renew starts."""
    from app import get_redis  # deferred: app-level helper, avoids circular import
    r = get_redis()
    if r is None:
        return
    try:
        r.delete(_renew_result_key(lock_key))
    except Exception:
        pass


def _store_renew_result(lock_key: str, payload: dict, ttl: int = 180) -> None:
    """Keep the exact successful response for duplicate-request Re-checks.

    The duplicate request cannot reconstruct the original package/message from a
    panel read-back alone. Redis is already required for the cross-worker renew
    lock, so keeping this short-lived result beside that lock also works when the
    retry and the original request land on different gunicorn workers.
    """
    from app import get_redis  # deferred: app-level helper, avoids circular import
    r = get_redis()
    if r is None:
        return
    try:
        r.set(_renew_result_key(lock_key), json.dumps(payload, ensure_ascii=False), ex=ttl)
    except Exception:
        pass


def _load_renew_result(lock_key: str) -> dict | None:
    from app import get_redis  # deferred: app-level helper, avoids circular import
    r = get_redis()
    if r is None:
        return None
    try:
        raw = r.get(_renew_result_key(lock_key))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _fire_renew_postcheck(server_id: int, inbound_id: int, email: str,
                          client_snapshot: dict) -> None:
    """Reconcile a completed renew without holding the browser request open.

    v3 node panels may take several seconds to make an update visible.  The
    renew call has already received a successful response from the panel at
    this point, so the read-back and the defensive re-enable are post-commit
    maintenance rather than part of the operator-facing critical path.
    """
    from app import (  # deferred: app-level helper, avoids circular import
        _v3_sanitize_email, app, fetch_inbounds, find_client, get_xui_session,
        patch_cached_client, server_is_v3, v3_enable_client,
    )
    snapshot = copy.deepcopy(client_snapshot or {})

    def _worker():
        with app.app_context():
            try:
                server = db.session.get(Server, server_id)
                if not server:
                    return
                session_obj, error = get_xui_session(server)
                if error:
                    return
                lookup_email = email
                observed = None
                expected_expiry = int(snapshot.get('expiryTime') or 0)
                expected_total = int(snapshot.get('totalGB') or 0)
                # A successful v3 response can precede read-after-write
                # visibility on every node. Retry only in this background task;
                # never put this settling delay back on the browser request.
                for attempt in range(3):
                    inbounds, fetch_err, _ = fetch_inbounds(
                        session_obj, server.host, server.panel_type,
                    )
                    if not fetch_err and inbounds:
                        observed, _ = find_client(inbounds, inbound_id, lookup_email)
                        if not observed and server_is_v3(server):
                            clean = _v3_sanitize_email(lookup_email)
                            if clean and clean != lookup_email:
                                observed, _ = find_client(inbounds, inbound_id, clean)
                                if observed:
                                    lookup_email = clean
                        if observed:
                            observed_expiry = int(observed.get('expiryTime') or 0)
                            observed_total = int(observed.get('totalGB') or 0)
                            if observed_expiry == expected_expiry and observed_total == expected_total:
                                break
                    if attempt < 2:
                        time.sleep(1)
                if not observed:
                    return

                observed_expiry = int(observed.get('expiryTime') or 0)
                observed_total = int(observed.get('totalGB') or 0)
                if observed_expiry != expected_expiry or observed_total != expected_total:
                    app.logger.warning(
                        f"Renew post-check still stale for {lookup_email}; "
                        "leaving optimistic cache intact"
                    )
                    return

                # The primary update always sends enable=True.  Keep the safety
                # net, but re-assert AND re-check until the panel actually
                # reports the client enabled — v3 nodes can lag, and a renewed
                # account must never be left suspended (manual disable or the
                # panel's own expiry/volume auto-disable alike).
                for _reenable_attempt in range(3):
                    if observed.get('enable') is not False:
                        break
                    snapshot['enable'] = True
                    if not server_is_v3(server):
                        break
                    reenabled, _response, reenable_error = v3_enable_client(
                        server, session_obj, lookup_email, snapshot,
                    )
                    if not reenabled:
                        app.logger.error(
                            f"Renew post-check could not re-enable {lookup_email}: "
                            f"{reenable_error}"
                        )
                        break
                    app.logger.warning(
                        f"Renew post-check re-asserted enable for {lookup_email} "
                        f"(panel had it disabled)"
                    )
                    time.sleep(1)
                    _inbounds_re, _fetch_err_re, _ = fetch_inbounds(
                        session_obj, server.host, server.panel_type,
                    )
                    if _fetch_err_re or not _inbounds_re:
                        continue
                    _recheck, _ = find_client(_inbounds_re, inbound_id, lookup_email)
                    if _recheck:
                        observed = _recheck
                if observed.get('enable') is False:
                    app.logger.error(
                        f"Renew post-check: {lookup_email} still disabled after "
                        f"re-assert attempts"
                    )

                patch_cached_client(
                    server_id, lookup_email,
                    client_uuid=str(observed.get('id')) if observed.get('id') else None,
                    total_gb_bytes=observed_total,
                    expiry_ts=observed_expiry,
                    enable=bool(observed.get('enable', True)),
                    comment=observed.get('comment'),
                )
            except Exception:
                app.logger.exception('[renew-postcheck] failed')

    threading.Thread(target=_worker, daemon=True).start()


def _fire_renew_whatsapp(server_id: int, email: str, text: str,
                         recipient_comment: str = '') -> None:
    """Send the transactional renew WhatsApp message off the request path."""
    from app import (  # deferred: app-level helper, avoids circular import
        _send_whatsapp_message, _whatsapp_automation_allowed_for_account, app,
    )
    def _worker():
        with app.app_context():
            try:
                if _whatsapp_automation_allowed_for_account(server_id, email):
                    _send_whatsapp_message(
                        'renew_success', email, text,
                        recipient_comment=recipient_comment,
                    )
            except Exception:
                app.logger.exception('[renew-whatsapp] send failed')

    threading.Thread(target=_worker, daemon=True).start()


@bp.route('/api/client/<int:server_id>/<int:inbound_id>/<email>/renew', methods=['POST'])
@login_required
def renew_client(server_id, inbound_id, email):
    """Renew client expiry and/or volume"""
    from app import (  # deferred: app-level helper, avoids circular import
        CLIENT_RESET_FALLBACKS, CLIENT_UPDATE_FALLBACKS, DEFAULT_RENEW_SMS_TEMPLATE,
        DEFAULT_RENEW_TEMPLATE, DEFAULT_TG_TPL_RENEW, GLOBAL_SERVER_DATA,
        RENEW_SMS_TEMPLATE_TYPE, _account_info_channel_links, _calculate_minimum_price,
        _cancel_stale_account_sms, _clear_message_cooldown, _compute_client_service_state,
        _fire_automation_sms, _get_dashboard_status_thresholds, _get_panel_ui_lang,
        _get_telegram_depletion_settings, _get_whatsapp_runtime_settings, _has_client_access,
        _json_field, _notification_bot_for_account, _notify_customer_telegram,
        _push_full_inbound, _recommendation_template_vars, _render_text_template,
        _reseller_can_create_free, _toggle_optout_tags, _user_can_afford, _v3_sanitize_email,
        _whatsapp_automation_allowed_for_account, _whatsapp_render_bot_template, app,
        build_panel_url, calculate_reseller_price, collect_endpoint_templates, fetch_inbounds,
        find_client, format_jalali, format_remaining_days, get_xui_session, log_transaction,
        patch_cached_client, persist_detected_panel_type, server_is_v3, v3_enable_client,
        v3_reset_client, v3_update_client,
    )
    t0 = time.perf_counter()
    renewal_trace_id = secrets.token_hex(4)
    panel_is_fa = _get_panel_ui_lang() == 'fa'
    timing = {
        "total_ms": None,
        "used_cache_client": False,
        "login_ms": None,
        "fetch_inbounds_ms": None,
        "update_post_ms": None,
        "reset_traffic_ms": None,
        "verify_fetch_ms": None,
        "update_endpoint": None,
        "update_status": None,
    }

    def _finish(payload: dict, status_code: int = 200):
        try:
            timing["total_ms"] = int((time.perf_counter() - t0) * 1000)
        except Exception:
            timing["total_ms"] = None
        if isinstance(payload, dict):
            payload.setdefault("trace_id", renewal_trace_id)
            payload.setdefault("timing", timing)
        # Log only slow renews (keeps logs clean)
        try:
            if timing.get("total_ms") is not None and timing["total_ms"] >= 2000:
                app.logger.info(
                    f"Renew timing: trace={renewal_trace_id}, server={server_id}, inbound={inbound_id}, email={email}, timing={timing}"
                )
        except Exception:
            pass
        return jsonify(payload), status_code

    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return _finish({"success": False, "error": "User not found"}, 401)

    server = db.session.get(Server, server_id)
    if not server:
        return _finish({"success": False, "error": "Server not found"}, 404)

    try:
        data = request.get_json() or {}
    except Exception:
        return _finish({"success": False, "error": "Invalid request data"}, 400)

    start_after_first_use = bool(data.get('start_after_first_use', False))
    reset_traffic = bool(data.get('reset_traffic', False))
    is_free = bool(data.get('free', False))
    if is_free and not _reseller_can_create_free(user):
        return _finish({"success": False, "error": "Free renewal is not permitted for your account"}, 403)
    mode = (data.get('mode') or 'custom').lower()
    if mode not in ('package', 'custom'):
        mode = 'custom'

    price = 0
    days_to_add = 0
    volume_gb_to_add = 0
    volume_provided = False
    description = ""
    pkg_name = None

    def _fractional_renew_value(value, field_name):
        number = float(value or 0)
        if not math.isfinite(number):
            raise ValueError(f'{field_name} must be a finite number')
        # Keep the existing clean labels/audit values for whole numbers while
        # preserving actual fractions such as 0.5.
        return int(number) if number.is_integer() else number

    try:
        if mode == 'package':
            pkg_id = data.get('package_id')
            package = db.session.get(Package, pkg_id) if pkg_id else None
            if not package or not getattr(package, 'enabled', True):
                return _finish({"success": False, "error": "Invalid package selected"}, 400)
            days_to_add = int(package.days or 0)
            volume_gb_to_add = int(package.volume or 0)
            volume_provided = True
            price = calculate_reseller_price(user, package=package)
            pkg_name = package.name
            description = f"Renew Package: {package.name} - {email}"
            if days_to_add < 0:
                return _finish({"success": False, "error": "Package is misconfigured (negative days)"}, 400)
        else:
            days_to_add = _fractional_renew_value(data.get('days', 0), 'days')
            raw_volume = data.get('volume', None)
            if raw_volume is None:
                volume_provided = False
                volume_gb_to_add = 0
            elif isinstance(raw_volume, str) and raw_volume.strip() == '':
                volume_provided = False
                volume_gb_to_add = 0
            else:
                volume_provided = True
                volume_gb_to_add = _fractional_renew_value(raw_volume, 'volume')
            if volume_gb_to_add < 0:
                volume_gb_to_add = 0
            if days_to_add < 0:
                days_to_add = 0
            # 0 days or 0 volume means unlimited; allowed for unlimited users
            
            reseller_context_id = user.id if user.role == 'reseller' else None
            price, _cpg, _cpd, _tier = _calculate_minimum_price(
                volume_gb_to_add,
                days_to_add,
                reseller_id=reseller_context_id,
                server_id=server_id,
                user=user,
            )
            days_label = f"{days_to_add} Days" if days_to_add > 0 else "Unlimited Days"
            if not volume_provided:
                vol_label = "Keep Volume"
            else:
                vol_label = f"{volume_gb_to_add} GB" if volume_gb_to_add > 0 else "Unlimited Volume"
            pkg_name = 'Custom'
            description = f"Renew Custom: {days_label}, {vol_label} - {email}"
    except (ValueError, TypeError) as e:
        return _finish({"success": False, "error": f"Invalid data: {e}"}, 400)
    except Exception as e:
        app.logger.error("Renew price-calc error (trace=%s): %s", renewal_trace_id, e, exc_info=True)
        return _finish({"success": False, "error": f"Server error during price calculation: {e}"}, 500)

    if is_free:
        price = 0

    # Gift volume: added on top of the renewal volume, free of charge.
    try:
        gift_volume_gb = int(data.get('gift_volume_gb') or 0)
        if gift_volume_gb < 0:
            gift_volume_gb = 0
    except (TypeError, ValueError):
        gift_volume_gb = 0

    if gift_volume_gb > 0:
        volume_gb_to_add += gift_volume_gb
        volume_provided = True
        gift_note = f"+{gift_volume_gb} گیگ هدیه رویالتی" if panel_is_fa else f"+{gift_volume_gb} GB for Royalty"
        description = f"{description} ({gift_note})"

    try:
        if user.role == 'reseller':
            if not _has_client_access(user, server_id, email, inbound_id=inbound_id):
                return _finish({"success": False, "error": "Access denied"}, 403)
            ok, err = _user_can_afford(user, price)
            if not ok:
                return _finish({"success": False, "error": err}, 402)
    except Exception as e:
        app.logger.error("Renew access-check error (trace=%s): %s", renewal_trace_id, e, exc_info=True)
        return _finish({"success": False, "error": f"Server error during access check: {e}"}, 500)

    # Optimization: Try to find client in global cache first to avoid slow fetch_inbounds
    # NOTE: cached display rows include usage stats while `raw_client` often does not.
    target_client = None
    cached_client_row = None
    fetched_inbound_row = None
    stats_up = 0
    stats_down = 0
    cached_inbounds = GLOBAL_SERVER_DATA.get('inbounds') or []
    for ib in cached_inbounds:
        try:
            if int(ib.get('server_id', -1)) == int(server.id) and int(ib.get('id', -1)) == int(inbound_id):
                for c in ib.get('clients', []):
                    if c.get('email') == email and 'raw_client' in c:
                        target_client = copy.deepcopy(c['raw_client'])
                        cached_client_row = c
                        timing["used_cache_client"] = True
                        break
        except (ValueError, TypeError):
            continue
        if target_client: break

    t_login0 = time.perf_counter()
    session_obj, error = get_xui_session(server)
    timing["login_ms"] = int((time.perf_counter() - t_login0) * 1000)
    if error:
        return _finish({"success": False, "error": error}, 400)

    # One renew per (server, account) at a time. The v3.4 panel update is slow,
    # so a timed-out request gets retried while the first is still running — which
    # would double-charge + double-SMS. Held for the TTL on SUCCESS (so a retry
    # right after completion is also blocked) and released immediately on failure.
    _renew_lock_key = f"renew:lock:{server_id}:{(email or '').strip().lower()}"
    if not _acquire_renew_lock(_renew_lock_key, ttl=45):
        _lang = _get_panel_ui_lang()
        _msg = ("این اکانت همین الان در حال تمدید است — چند لحظه صبر کنید و سپس Re-check بزنید."
                if _lang == 'fa' else
                "This account is already being renewed — please wait a moment, then press Re-check.")
        # Not an error to throw at the user: the renew IS happening. Signal the UI
        # to open the result modal in an "in progress" state (with Re-check) rather
        # than a red error toast, and never double-charge.
        return _finish({"success": False, "code": "renew_in_progress", "error": _msg,
                        "server_id": server_id, "inbound_id": inbound_id, "email": email}, 409)

    # This is a new renewal, not a duplicate retry. Do not let a short-lived
    # result from the previous renewal satisfy this one's Re-check.
    _clear_renew_result(_renew_lock_key)

    try:
        if not target_client:
            # Fallback to fetching from panel if not in cache
            t_fetch0 = time.perf_counter()
            inbounds, fetch_err, detected_type = fetch_inbounds(session_obj, server.host, server.panel_type)
            timing["fetch_inbounds_ms"] = int((time.perf_counter() - t_fetch0) * 1000)
            if fetch_err:
                return _finish({"success": False, "error": "Failed to fetch inbounds"}, 400)

            persist_detected_panel_type(server, detected_type)
            target_client, fetched_inbound_row = find_client(inbounds, inbound_id, email)
            if not target_client:
                return _finish({"success": False, "error": "Client not found"}, 404)

            # Try to capture traffic usage from the inbound's clientStats.
            # Many panels do NOT include up/down in the client settings list.
            try:
                for st in (fetched_inbound_row or {}).get('clientStats', []) or []:
                    if (st.get('email') or '') == email:
                        stats_up = int(st.get('up') or 0)
                        stats_down = int(st.get('down') or 0)
                        break
            except Exception:
                stats_up = 0
                stats_down = 0

        # If we used cached raw_client, merge in traffic/cap fields from the cached row.
        # This avoids undercounting usage when `raw_client` is missing one direction
        # OR contains a stale smaller number.
        # NOTE: cached_client_row.expiryTime is a human string; use expiryTimestamp instead.
        if cached_client_row and isinstance(target_client, dict):
            try:
                cached_up = int(cached_client_row.get('up') or 0)
            except Exception:
                cached_up = 0
            try:
                cur_up = int(target_client.get('up') or 0)
            except Exception:
                cur_up = 0
            if cached_up > cur_up:
                target_client['up'] = cached_up

            try:
                cached_down = int(cached_client_row.get('down') or 0)
            except Exception:
                cached_down = 0
            try:
                cur_down = int(target_client.get('down') or 0)
            except Exception:
                cur_down = 0
            if cached_down > cur_down:
                target_client['down'] = cached_down

            try:
                if target_client.get('totalGB') in (None, '', 0) and cached_client_row.get('totalGB') not in (None, ''):
                    target_client['totalGB'] = cached_client_row.get('totalGB')
            except Exception:
                pass
            try:
                if target_client.get('expiryTime') in (None, '', 0) and cached_client_row.get('expiryTimestamp') not in (None, ''):
                    target_client['expiryTime'] = cached_client_row.get('expiryTimestamp')
            except Exception:
                pass

        try:
            current_expiry_ms = int(target_client.get('expiryTime') or 0)
        except (TypeError, ValueError):
            current_expiry_ms = 0

        # Snapshot current remaining values for message rendering.
        # IMPORTANT: keep rounding consistent with UI (format_remaining_days uses floor .days).
        remaining_days_before = 0
        try:
            expiry_info_before = format_remaining_days(current_expiry_ms)
            raw_days = int(expiry_info_before.get('days') or 0)
            if raw_days > 0:
                remaining_days_before = raw_days
            elif expiry_info_before.get('type') == 'start_after_use' and raw_days >= 0:
                remaining_days_before = raw_days
            else:
                remaining_days_before = 0
        except Exception:
            remaining_days_before = 0

        try:
            current_total_bytes = int(target_client.get('totalGB') or 0)
        except (TypeError, ValueError):
            current_total_bytes = 0

        try:
            used_up = int(target_client.get('up') or 0)
        except (TypeError, ValueError):
            used_up = 0
        try:
            used_down = int(target_client.get('down') or 0)
        except (TypeError, ValueError):
            used_down = 0

        # If the raw client doesn't carry traffic fields (common), fall back to clientStats.
        # Some panels only include one direction (up/down) in the client settings list.
        # Prefer clientStats when it provides missing OR higher values to avoid undercounting usage.
        try:
            if stats_up or stats_down:
                try:
                    su = int(stats_up or 0)
                except Exception:
                    su = 0
                try:
                    sd = int(stats_down or 0)
                except Exception:
                    sd = 0

                if su:
                    used_up = max(int(used_up or 0), su)
                if sd:
                    used_down = max(int(used_down or 0), sd)
        except Exception:
            pass
        used_bytes = max(0, used_up + used_down)

        remaining_gb_before = 0
        remaining_gb_before_exact = 0.0
        has_limited_volume = current_total_bytes > 0
        if has_limited_volume:
            remaining_bytes = current_total_bytes - used_bytes
            if remaining_bytes < 0:
                remaining_bytes = 0

            # Keep an exact value for renewal message (e.g. 45.11GB).
            remaining_gb_before_exact = (
                remaining_bytes / float(1024 * 1024 * 1024) if remaining_bytes > 0 else 0.0
            )

            # Keep a coarse integer variant for legacy template placeholders.
            gb_float = remaining_gb_before_exact
            rounded_gb = int(gb_float + 0.5) if gb_float > 0 else 0
            remaining_gb_before = max(1, rounded_gb) if remaining_bytes > 0 else 0
        
        # Panels store expiry in integer milliseconds. Convert only after applying
        # the fractional-day value so e.g. 0.5 days remains exactly 12 hours.
        duration_ms = int(round(days_to_add * 86400000))

        # Calculate new expiry
        if days_to_add == 0:
            # 0 days = unlimited expiry
            new_expiry = 0
        elif start_after_first_use:
            new_expiry = -duration_ms
        else:
            current_expiry = target_client.get('expiryTime', 0)
            # If the client is not started yet (negative expiry), keep it not-started
            # and add days to the pending duration.
            try:
                current_expiry_int = int(current_expiry or 0)
            except (TypeError, ValueError):
                current_expiry_int = 0

            if current_expiry_int < 0:
                new_expiry = current_expiry_int - duration_ms
            elif current_expiry_int > 0:
                # Add days in milliseconds (avoids DST/timezone edge cases).
                # An already-expired timestamp must NOT be extended in the past:
                # the panel's own watchdog would disable the client again within
                # a minute. Base the extension on "now" so the renewed account
                # actually goes (and stays) active.
                now_ms = int(time.time() * 1000)
                base_expiry = current_expiry_int if current_expiry_int > now_ms else now_ms
                new_expiry = base_expiry + duration_ms
            else:
                new_expiry = int(time.time() * 1000) + duration_ms
        
        # Update volume
        current_volume = current_total_bytes
        
        if reset_traffic:
            target_client['up'] = 0
            target_client['down'] = 0
            # If resetting, keep cap unless user explicitly provided a new cap.
            if not volume_provided:
                new_volume = current_volume
            else:
                # 0 = unlimited, >0 = set exact cap
                if volume_gb_to_add > 0:
                    new_volume = int(round(volume_gb_to_add * 1024 * 1024 * 1024))
                else:
                    new_volume = 0  # unlimited
        else:
            # When adding volume:
            # - If volume not provided: keep existing cap
            # - If provided: 0 means set to unlimited, >0 means add to current
            if not volume_provided:
                new_volume = current_volume
            elif volume_gb_to_add == 0:
                new_volume = 0  # unlimited
            elif volume_gb_to_add > 0:
                # If current is unlimited (0), keep unlimited.
                if current_volume == 0:
                    new_volume = 0
                else:
                    new_volume = current_volume + int(round(volume_gb_to_add * 1024 * 1024 * 1024))
            else:
                new_volume = current_volume
        
        # Check if client was disabled before we re-enable it (for notification)
        _was_disabled = not target_client.get('enable', True)

        # Update client — always re-enable so disabled-due-to-traffic clients go active immediately
        target_client['expiryTime'] = new_expiry
        target_client['totalGB'] = new_volume
        target_client['enable'] = True
        # Renew = active customer again → strip the #nosms/#nopm opt-out tags that
        # the disable flow added, so automation messaging resumes (mirrors the
        # manual enable toggle). No-op when the comment has no such tags.
        target_client['comment'] = _toggle_optout_tags(target_client.get('comment'), add=False)

        client_id = target_client.get('id', target_client.get('password', email))

        update_payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [target_client]})
        }

        replacements = {
            'id': inbound_id,
            'inbound_id': inbound_id,
            'inboundId': inbound_id,
            'clientId': client_id,
            'client_id': client_id,
            'email': email
        }

        _is_v3 = server_is_v3(server)
        # Shadowsocks clients have no UUID 'id' field — updateClient/:clientId won't work.
        _is_shadowsocks_no_id = (not _is_v3) and ('id' not in target_client)

        class _SyntheticOK:  # lets the v3 path reuse the legacy post-success block
            status_code = 200
            @staticmethod
            def json():
                return {"success": True}

        templates = collect_endpoint_templates(server.panel_type, 'client_update', CLIENT_UPDATE_FALLBACKS)
        errors = []
        t_update0 = time.perf_counter()
        for template in templates:
            full_url = build_panel_url(server.host, template, replacements)
            if not full_url:
                continue
            if _is_v3:
                # v3: first-class client update by email (legacy updateClient is 404)
                ok, _vr, verr = v3_update_client(server, session_obj, email, target_client)
                if not ok:
                    errors.append(f"v3 update: {verr}")
                    break
                # v3 renames the client to the space-free email on the panel during
                # the update, so every later lookup (reset, verify) and the success
                # message must use the clean email — otherwise find_client returns
                # client_not_found and the message shows the old spaced email.
                email = _v3_sanitize_email(email)
                enabled, _er, enable_error = v3_enable_client(
                    server, session_obj, email, target_client,
                )
                if not enabled:
                    errors.append(f"v3 enable: {enable_error}")
                    break
                resp = _SyntheticOK()
            elif _is_shadowsocks_no_id:
                # Shadowsocks clients have no UUID — use full inbound update instead.
                _full_ib = fetched_inbound_row
                if _full_ib is None:
                    # Came from cache; re-fetch to get the complete inbound object.
                    _ibs_fresh, _fe2, _ = fetch_inbounds(session_obj, server.host, server.panel_type)
                    if not _fe2:
                        for _ib in (_ibs_fresh or []):
                            if _ib.get('id') == inbound_id:
                                _full_ib = _ib
                                break
                if _full_ib is None:
                    errors.append("shadowsocks: could not fetch full inbound for update")
                    break
                _full_settings = _json_field(_full_ib.get('settings'), {})
                _full_settings['clients'] = [
                    target_client if c.get('email') == email else c
                    for c in _full_settings.get('clients', [])
                ]
                _ok_push, _push_err = _push_full_inbound(server, session_obj, _full_ib, _full_settings)
                if not _ok_push:
                    errors.append(f"shadowsocks inbound update: {_push_err}")
                    break
                resp = _SyntheticOK()
            else:
                try:
                    resp = session_obj.post(full_url, json=update_payload, verify=False, timeout=10)
                except Exception as exc:
                    errors.append(f"{template}: {exc}")
                    continue
            if resp.status_code == 200:
                timing["update_post_ms"] = int((time.perf_counter() - t_update0) * 1000)
                timing["update_endpoint"] = template
                timing["update_status"] = resp.status_code
                try:
                    resp_json = resp.json()
                    if isinstance(resp_json, dict) and resp_json.get('success') is False:
                        errors.append(f"{template}: success false")
                        continue
                except ValueError:
                    pass
                
                # If reset_traffic was requested, we must call the specific reset endpoint
                # because updateClient usually ignores 'up'/'down' fields.
                if reset_traffic and _is_v3:
                    t_reset0 = time.perf_counter()
                    v3_reset_client(server, session_obj, email)
                    timing["reset_traffic_ms"] = int((time.perf_counter() - t_reset0) * 1000)
                elif reset_traffic:
                    reset_templates = collect_endpoint_templates(server.panel_type, 'client_reset_traffic', CLIENT_RESET_FALLBACKS)
                    t_reset0 = time.perf_counter()
                    for r_template in reset_templates:
                        r_url = build_panel_url(server.host, r_template, replacements)
                        if not r_url: continue
                        
                        # Some panels need email in body, some in URL. Try both if needed.
                        requires_path_email = (':email' in r_template) or ('{email}' in r_template)
                        r_payload = None if requires_path_email else {"email": email}
                        
                        try:
                            if r_payload is None:
                                session_obj.post(r_url, verify=False, timeout=5)
                            else:
                                session_obj.post(r_url, json=r_payload, verify=False, timeout=5)
                            # We don't strictly check success here as the main update succeeded, 
                            # but we try our best to reset traffic.
                        except:
                            pass
                    timing["reset_traffic_ms"] = int((time.perf_counter() - t_reset0) * 1000)

                # Read the account back from 3x-ui before charging, notifying, or
                # updating EVE's cache.  A successful POST only acknowledges the
                # request; it does not prove the client is active on the panel.
                verify = {
                    "attempted": True,
                    "ok": None,
                    "error": None,
                    "expected": {
                        "expiryTime": new_expiry,
                        "totalGB": new_volume,
                        "enable": True,
                    },
                    "observed": {
                        "expiryTime": None,
                        "totalGB": None,
                    },
                }
                defer_inline_verify = False
                try:
                    if defer_inline_verify:
                        v_inbounds, v_err = [], 'verification_deferred'
                        timing["verify_fetch_ms"] = 0
                    else:
                        t_v0 = time.perf_counter()
                        v_inbounds, v_err, _ = fetch_inbounds(session_obj, server.host, server.panel_type)
                        timing["verify_fetch_ms"] = int((time.perf_counter() - t_v0) * 1000)
                    if v_err or not v_inbounds:
                        verify["ok"] = False
                        verify["error"] = v_err or "verify_fetch_failed"
                    else:
                        v_client, v_inbound = find_client(v_inbounds, inbound_id, email)
                        if not v_client and server_is_v3(server):
                            # v3 stores the email space-free; after a spaced-email
                            # rename the lookup must use the sanitized form, else
                            # verify wrongly reports "not verified".
                            _vc = _v3_sanitize_email(email)
                            if _vc and _vc != email:
                                v_client, v_inbound = find_client(v_inbounds, inbound_id, _vc)
                                if v_client:
                                    email = _vc
                        if not v_client:
                            verify["ok"] = False
                            verify["error"] = "client_not_found_after_update"
                        else:
                            try:
                                verify["observed"]["expiryTime"] = int(v_client.get('expiryTime') or 0)
                            except Exception:
                                verify["observed"]["expiryTime"] = None
                            try:
                                verify["observed"]["totalGB"] = int(v_client.get('totalGB') or 0)
                            except Exception:
                                verify["observed"]["totalGB"] = None

                            # Record enable up-front — before the best-effort
                            # service-state block below — so the re-enable safety
                            # net always sees the panel's actual value even when
                            # the state computation fails.
                            verify["observed"]["enable"] = bool(v_client.get('enable', True))

                            # Compute service state for immediate UI update
                            try:
                                v_up = 0
                                v_down = 0
                                for st in (v_inbound.get('clientStats', []) if v_inbound else []):
                                    if st.get('email') == email:
                                        v_up = st.get('up', 0)
                                        v_down = st.get('down', 0)
                                        break
                                
                                v_total = verify["observed"]["totalGB"] or 0
                                v_remaining = max(v_total - (v_up + v_down), 0) if v_total > 0 else None
                                v_expiry = verify["observed"]["expiryTime"] or 0
                                v_expiry_info = format_remaining_days(v_expiry, lang=_get_panel_ui_lang())
                                
                                v_state = _compute_client_service_state(
                                    enabled=bool(v_client.get('enable', True)),
                                    total_bytes=v_total,
                                    remaining_bytes=v_remaining,
                                    expiry_ts=v_expiry,
                                    expiry_info=v_expiry_info,
                                    thresholds=_get_dashboard_status_thresholds(),
                                    lang=_get_panel_ui_lang()
                                )
                                verify["observed"]["service_state_label"] = v_state.get('label')
                                verify["observed"]["service_state_emoji"] = v_state.get('emoji')
                                verify["observed"]["service_state_tag"] = v_state.get('tag')
                                verify["observed"]["up"] = v_up
                                verify["observed"]["down"] = v_down
                                verify["observed"]["enable"] = bool(v_client.get('enable', True))
                            except Exception as e:
                                app.logger.error("Error computing service state in renew verify: %s", e)

                            ok_exp = (verify["observed"]["expiryTime"] == int(new_expiry or 0))
                            ok_vol = (verify["observed"]["totalGB"] == int(new_volume or 0))
                            ok_enable = (verify["observed"].get("enable") is not False)
                            verify["ok"] = bool(ok_exp and ok_vol and ok_enable)
                except Exception as exc:
                    verify["ok"] = False
                    verify["error"] = str(exc)

                # Safety net: renew always pushes enable=True, but if the panel
                # read-back shows the client STILL disabled (manual disable or
                # the panel's own expiry/volume auto-disable), re-assert AND
                # re-check until it sticks — a renewed account must never be
                # left suspended.
                if verify.get("observed", {}).get("enable") is False:
                    for _reenable_attempt in range(3):
                        try:
                            target_client["enable"] = True
                            if _is_v3:
                                v3_enable_client(server, session_obj, email, target_client)
                            else:
                                session_obj.post(full_url, json=update_payload, verify=False, timeout=10)
                            time.sleep(1)
                            r_inbounds, r_err, _ = fetch_inbounds(session_obj, server.host, server.panel_type)
                            if r_err or not r_inbounds:
                                continue
                            r_client, _r_ib = find_client(r_inbounds, inbound_id, email)
                            if not r_client and _is_v3:
                                _rc = _v3_sanitize_email(email)
                                if _rc and _rc != email:
                                    r_client, _r_ib = find_client(r_inbounds, inbound_id, _rc)
                                    if r_client:
                                        email = _rc
                            if r_client and r_client.get('enable', True):
                                verify["observed"]["enable"] = True
                                verify["re_enabled"] = True
                                ok_exp = (verify["observed"]["expiryTime"] == int(new_expiry or 0))
                                ok_vol = (verify["observed"]["totalGB"] == int(new_volume or 0))
                                verify["ok"] = bool(ok_exp and ok_vol)
                                app.logger.warning(
                                    f"Renew re-asserted enable for {email} (panel had it disabled)")
                                break
                        except Exception:
                            continue
                    if verify["observed"].get("enable") is False:
                        verify["ok"] = False
                        verify["error"] = "enable_reassert_failed"
                        app.logger.error(
                            f"Renew could not re-enable {email} after 3 attempts")

                if not verify.get("ok"):
                    # Keep the lock/result so an operator can use Re-check without
                    # accidentally submitting a second renewal while 3x-ui is
                    # still settling.  Crucially, do not charge, notify, or write
                    # expected values into the cache as if they were observed.
                    _store_renew_result(_renew_lock_key, {"verify": verify})
                    detail = ("تمدید به پنل ارسال شد، اما فعال بودن کاربر در 3x-ui تأیید نشد. "
                              "از Re-check استفاده کنید و قبل از تمدید دوباره وضعیت پنل را بررسی کنید."
                              if panel_is_fa else
                              "The renewal was sent, but the client was not confirmed active in 3x-ui. "
                              "Use Re-check and inspect the panel before renewing again.")
                    return _finish({
                        "success": False,
                        "code": "renew_not_verified",
                        "error": detail,
                        "verify": verify,
                    }, 409)

                # Only a confirmed panel state may create financial records or
                # trigger customer notifications.
                sender_card = data.get('sender_card', '') or ''
                card_id = data.get('card_id')
                if is_free:
                    if user.role == 'reseller':
                        log_transaction(user.id, 0, 'renew', f"User Renewal (Free) - {description}", server_id=server.id, sender_card=sender_card, card_id=card_id, category='usage', client_email=email, package_name=pkg_name, volume_gb=volume_gb_to_add, days=days_to_add)
                    else:
                        log_transaction(user.id, 0, 'renew', f"User Renewal (Free) - {description}", server_id=server.id, sender_card=sender_card, card_id=card_id, category='income', client_email=email, package_name=pkg_name, volume_gb=volume_gb_to_add, days=days_to_add)
                    db.session.commit()
                elif price > 0:
                    if user.role == 'reseller':
                        user.credit -= price
                        log_transaction(user.id, -price, 'renew', f"User Renewal (Credit Usage) - {description}", server_id=server.id, sender_card=sender_card, card_id=card_id, category='usage', client_email=email, package_name=pkg_name, volume_gb=volume_gb_to_add, days=days_to_add)
                    else:
                        log_transaction(user.id, price, 'renew', f"User Renewal (Income) - {description}", server_id=server.id, sender_card=sender_card, card_id=card_id, category='income', client_email=email, package_name=pkg_name, volume_gb=volume_gb_to_add, days=days_to_add)
                    db.session.commit()

                # Build copyable success text (dynamic template)
                now_utc = datetime.utcnow()

                # Message values:
                # - If start_after_first_use: show the package/custom amount (days_to_add)
                # - If reset_traffic: show the package/custom amount (volume_gb_to_add)
                # - Otherwise: show remaining_before + added (days/GB)
                if days_to_add <= 0:
                    msg_days = '♾️'
                    days_label = "♾️"
                elif start_after_first_use:
                    msg_days = days_to_add
                    days_label = f"{msg_days} Days"
                else:
                    msg_days = int(remaining_days_before) + days_to_add
                    days_label = f"{msg_days} Days"

                if not volume_provided:
                    # No volume change: show remaining (or unlimited)
                    if not has_limited_volume:
                        msg_volume = '♾️'
                        volume_label = "♾️"
                    else:
                        msg_volume = int(remaining_gb_before)
                        volume_label = f"{remaining_gb_before_exact:.2f}GB"
                elif volume_gb_to_add == 0:
                    msg_volume = '♾️'
                    volume_label = "♾️"
                elif reset_traffic:
                    msg_volume = volume_gb_to_add
                    volume_label = f"{msg_volume}GB"
                else:
                    if not has_limited_volume:
                        msg_volume = '♾️'
                        volume_label = "♾️"
                    else:
                        msg_volume = int(remaining_gb_before) + volume_gb_to_add
                        volume_label = f"{(remaining_gb_before_exact + float(volume_gb_to_add)):.2f}GB"

                # `{date}` should represent the new expiry, not "now".
                # - Finite expiry (>0): show Jalali Tehran date+time
                # - Unlimited (0): Persian label
                # - Not started (<0): show "N days after first use"
                if new_expiry == 0:
                    date_label = "نامحدود" if panel_is_fa else "Unlimited"
                elif new_expiry < 0:
                    date_label = (f"{msg_days} روز بعد از اولین اتصال" if panel_is_fa
                                  else f"{msg_days} days after first connection")
                else:
                    try:
                        expiry_dt_utc = datetime.utcfromtimestamp(int(new_expiry) / 1000)
                    except Exception:
                        expiry_dt_utc = now_utc
                    date_label = format_jalali(expiry_dt_utc) or ''

                # Dashboard link
                app_base = request.url_root.rstrip('/')
                final_id = target_client.get('subId') or target_client.get('id') or ''
                dashboard_link = f"{app_base}/s/{server.id}/{final_id}" if final_id else ""

                active_tpl = RenewTemplate.query.filter_by(is_active=True).first()
                tpl_content = active_tpl.content if active_tpl else DEFAULT_RENEW_TEMPLATE
                _ch_links = _account_info_channel_links(db.session.get(Admin, session.get('admin_id'))) \
                    if session.get('admin_id') else {'telegram_channel': '', 'whatsapp_channel': ''}
                _renew_tpl_vars = {
                    'email': email,
                    'days': msg_days,
                    'days_label': days_label,
                    'volume': msg_volume,
                    'volume_label': volume_label,
                    'date': date_label,
                    'server_name': getattr(server, 'name', '') or '',
                    'mode': mode,
                    'dashboard_link': dashboard_link,
                    # Account-Info-style aliases so a template written with the
                    # account-info placeholders ({service_name}/{remaining_time}/
                    # {remaining_volume}/{account_name}/{sub_link}) also renders.
                    'service_name': email,
                    'account_name': email,
                    'remaining_time': days_label,
                    'remaining_volume': volume_label,
                    'sub_link': dashboard_link,
                    # Gift: {gift_volume} holds the amount; {gift_given} drives the
                    # {if_gift}...{/if_gift} conditional block in the template.
                    'gift_volume': str(gift_volume_gb) if gift_volume_gb > 0 else '',
                    'gift_given': gift_volume_gb > 0,
                    # Channel links resolved by role (superadmin→global, reseller→own).
                    'telegram_channel': _ch_links.get('telegram_channel', ''),
                    'whatsapp_channel': _ch_links.get('whatsapp_channel', ''),
                    '_sub_id': final_id,
                }
                _renew_tpl_vars.update(_recommendation_template_vars(
                    server.id, final_id, email, terminal=False,
                ))
                copy_text = _render_text_template(tpl_content, _renew_tpl_vars)

                whatsapp_runtime = _get_whatsapp_runtime_settings()
                _client_comment = (target_client.get('comment') or '') if target_client else ''
                # WhatsApp bot uses its own renew template (falls back to the
                # generic renew copy when no bot template is configured).
                _wa_text = _whatsapp_render_bot_template('renew_success', _renew_tpl_vars, whatsapp_runtime) or copy_text
                # Skip the automated send for reseller-owned accounts whose owner
                # has not enabled WhatsApp automation — don't message their
                # clients from the system number.
                whatsapp_allowed = _whatsapp_automation_allowed_for_account(server.id, email)
                whatsapp_scheduled = bool(
                    whatsapp_allowed
                    and whatsapp_runtime.get('enabled', False)
                    and whatsapp_runtime.get('deployment_region') != 'iran'
                    and whatsapp_runtime.get('trigger_renew_success', False)
                )
                whatsapp_delivery = {
                    'sent': False,
                    'scheduled': whatsapp_scheduled,
                    'reason': None if whatsapp_allowed else 'reseller_automation_disabled',
                }

                # Write through before cancelling stale warnings. A depletion worker
                # may already have classified this account and must see the renewed
                # state during its final pre-dispatch validation.
                try:
                    patch_cached_client(
                        server_id, email,
                        client_uuid=str(target_client.get('id')) if target_client and target_client.get('id') else None,
                        total_gb_bytes=int(target_client.get('totalGB') or 0),
                        expiry_ts=int(target_client.get('expiryTime') or 0),
                        enable=True,
                        comment=target_client.get('comment'),
                        up=(0 if reset_traffic else None),
                        down=(0 if reset_traffic else None))
                except Exception:
                    pass

                # SMS automation (GMweb) — non-reseller-owned accounts only; runs
                # in a background thread so it never delays the renew response.
                _cancel_stale_account_sms(server.id, email, reason='renew_success')
                _fire_automation_sms('renew', server.id, email, RENEW_SMS_TEMPLATE_TYPE,
                                     DEFAULT_RENEW_SMS_TEMPLATE, _renew_tpl_vars, _client_comment,
                                     server_name=getattr(server, 'name', '') or '')
                # Telegram confirmation for bot-linked customers — reseller-owned
                # accounts are messaged through the reseller's own bot.
                try:
                    telegram_runtime = _get_telegram_depletion_settings()
                    _tg_bot, _tg_own = _notification_bot_for_account(server.id, email)
                    _tg_text = _render_text_template(
                        telegram_runtime.get('tpl_renew') or DEFAULT_TG_TPL_RENEW,
                        _renew_tpl_vars,
                    )
                    if (telegram_runtime.get('trigger_renew_success', True)
                            and _tg_own and _tg_bot and (_tg_text or '').strip()):
                        _notify_customer_telegram(_tg_own.customer_id, _tg_text, bot=_tg_bot)
                except Exception:
                    pass
                whatsapp_meta = {
                    'enabled': whatsapp_runtime.get('enabled', False),
                    'deployment_region': whatsapp_runtime.get('deployment_region', 'outside'),
                    'provider': whatsapp_runtime.get('provider', 'baileys'),
                    'trigger_renew_success': whatsapp_runtime.get('trigger_renew_success', False),
                    'blocked_reason': whatsapp_runtime.get('blocked_reason') if not whatsapp_runtime.get('enabled', False) else None,
                    'delivery': whatsapp_delivery,
                }

                # Reset send counter + automation cooldown so a renewed account can
                # be messaged again from scratch (both SMS and WhatsApp).
                _clear_message_cooldown(email, server_id)
                if whatsapp_scheduled:
                    _fire_renew_whatsapp(server.id, email, _wa_text, _client_comment)

                _store_renew_result(_renew_lock_key, {
                    "copy_text": copy_text,
                    "tpl_vars": _renew_tpl_vars,
                    "verify": verify,
                    "client_comment": _client_comment,
                    "was_reactivated": _was_disabled,
                })
                return _finish({"success": True, "copy_text": copy_text, "tpl_vars": _renew_tpl_vars, "verify": verify, "whatsapp": whatsapp_meta, "was_reactivated": _was_disabled})

            errors.append(f"{template}: {resp.status_code}")
            timing["update_endpoint"] = template
            timing["update_status"] = resp.status_code
            if resp.status_code != 404:
                break

        app.logger.warning("Renew failed for %s: %s", email, '; '.join(errors))
        # Surface the REAL panel error (was a useless generic string), so the UI
        # shows what actually went wrong instead of "Server error (HTTP 400)".
        detail = '; '.join(str(e) for e in errors) or 'Client update failed on the panel'
        if 'duplicate subid' in detail.lower():
            detail += ((' — این اکانت subId تکراری دارد و پنل اجازه‌ی آپدیت نمی‌دهد. در پنل، subId را یکتا کنید و دوباره تمدید کنید.'
                        if panel_is_fa else
                        ' — This account has a duplicate subId, so the panel rejected the update. Make its subId unique in the panel, then retry the renewal.'))
        _release_renew_lock(_renew_lock_key)  # failed → allow an immediate retry
        return _finish({"success": False, "error": detail}, 400)
    except Exception as e:
        app.logger.error("Renew error: %s", e)
        _release_renew_lock(_renew_lock_key)  # failed → allow an immediate retry
        return _finish({"success": False, "error": str(e)}, 400)


@bp.route('/api/client/<int:server_id>/rotate', methods=['POST'])
@login_required
def rotate_client(server_id):
    """Replace a leaked client: disable the old one (uid/link revoked) and create
    a fresh client (new UUID + new subId) carrying over the remaining traffic
    bytes and remaining time."""
    from app import (  # deferred: app-level helper, avoids circular import
        GLOBAL_REFRESH_LOCK, GLOBAL_SERVER_DATA, _add_client_to_inbound, _has_client_access,
        _json_field, _push_full_inbound, _recompute_cached_client, add_cached_client, app,
        build_public_subscription_url, fetch_inbounds, get_xui_session, invalidate_ownership_cache,
        persist_detected_panel_type, server_is_v3, v3_add_client, v3_update_client,
    )
    user = db.session.get(Admin, session['admin_id'])
    if not user:
        return jsonify({"ok": False, "success": False, "error": "User not found"}), 401

    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"ok": False, "success": False, "error": "Server not found"}), 404

    try:
        data = request.get_json() or {}
    except Exception:
        return jsonify({"ok": False, "success": False, "error": "Invalid request data"}), 400

    email = (data.get('client_email') or '').strip()
    if not email:
        return jsonify({"ok": False, "success": False, "error": "client_email is required"}), 400

    try:
        if user.role == 'reseller' and not _has_client_access(user, server_id, email):
            return jsonify({"ok": False, "success": False, "error": "Access denied"}), 403
    except Exception as e:
        app.logger.error("Rotate access-check error: %s", e, exc_info=True)
        return jsonify({"ok": False, "success": False, "error": "Server error during access check"}), 500

    # Locate the client in the global cache first (any inbound on this server).
    target_client = None
    cached_client_row = None
    inbound_id = None
    known_emails = set()
    for ib in (GLOBAL_SERVER_DATA.get('inbounds') or []):
        try:
            if int(ib.get('server_id', -1)) != int(server.id):
                continue
        except (ValueError, TypeError):
            continue
        for c in ib.get('clients', []):
            c_email = c.get('email')
            if c_email:
                known_emails.add(c_email)
            if c_email == email and 'raw_client' in c and target_client is None:
                target_client = copy.deepcopy(c['raw_client'])
                cached_client_row = c
                try:
                    inbound_id = int(ib.get('id'))
                except (ValueError, TypeError):
                    inbound_id = None

    session_obj, error = get_xui_session(server)
    if error:
        return jsonify({"ok": False, "success": False, "error": error}), 400

    # Same lock family as renew: a rotate must never overlap a renew (or a second
    # rotate) of the same account — both rewrite the client on the panel.
    _rotate_lock_key = f"renew:lock:{server_id}:{email.lower()}"
    if not _acquire_renew_lock(_rotate_lock_key, ttl=45):
        return jsonify({"ok": False, "success": False, "code": "rotate_in_progress",
                        "error": "This account is already being rotated/renewed — please wait a moment."}), 409

    fetched_inbound_row = None
    try:
        if not target_client:
            # Fallback to fetching from the panel if not in cache
            inbounds, fetch_err, detected_type = fetch_inbounds(session_obj, server.host, server.panel_type)
            if fetch_err:
                _release_renew_lock(_rotate_lock_key)
                return jsonify({"ok": False, "success": False, "error": "Failed to fetch inbounds"}), 400
            persist_detected_panel_type(server, detected_type)
            for ib in (inbounds or []):
                settings = _json_field(ib.get('settings'), {})
                for c in settings.get('clients', []):
                    c_email = c.get('email')
                    if c_email:
                        known_emails.add(c_email)
                    if c_email == email and target_client is None:
                        target_client = copy.deepcopy(c)
                        fetched_inbound_row = ib
                        try:
                            inbound_id = int(ib.get('id'))
                        except (ValueError, TypeError):
                            inbound_id = None
            if not target_client:
                _release_renew_lock(_rotate_lock_key)
                return jsonify({"ok": False, "success": False, "error": "Client not found"}), 404

        if inbound_id is None:
            _release_renew_lock(_rotate_lock_key)
            return jsonify({"ok": False, "success": False, "error": "Could not determine client's inbound"}), 400

        # Merge in traffic/cap fields from the cached row (raw_client often lacks usage).
        if cached_client_row and isinstance(target_client, dict):
            for _field in ('up', 'down'):
                try:
                    _cached_v = int(cached_client_row.get(_field) or 0)
                except Exception:
                    _cached_v = 0
                try:
                    _cur_v = int(target_client.get(_field) or 0)
                except Exception:
                    _cur_v = 0
                if _cached_v > _cur_v:
                    target_client[_field] = _cached_v
            try:
                if target_client.get('totalGB') in (None, '', 0) and cached_client_row.get('totalGB') not in (None, ''):
                    target_client['totalGB'] = cached_client_row.get('totalGB')
            except Exception:
                pass
            try:
                if target_client.get('expiryTime') in (None, '', 0) and cached_client_row.get('expiryTimestamp') not in (None, ''):
                    target_client['expiryTime'] = cached_client_row.get('expiryTimestamp')
            except Exception:
                pass

        # ── Snapshot remaining quota ──
        try:
            current_total_bytes = int(target_client.get('totalGB') or 0)
        except (TypeError, ValueError):
            current_total_bytes = 0
        try:
            used_bytes = max(0, int(target_client.get('up') or 0) + int(target_client.get('down') or 0))
        except (TypeError, ValueError):
            used_bytes = 0
        # 0 totalGB = unlimited → stays unlimited (0)
        remaining_bytes = max(0, current_total_bytes - used_bytes) if current_total_bytes > 0 else 0

        try:
            current_expiry_ms = int(target_client.get('expiryTime') or 0)
        except (TypeError, ValueError):
            current_expiry_ms = 0
        now_ms = int(time.time() * 1000)
        if current_expiry_ms > 0:
            remaining_ms = max(0, current_expiry_ms - now_ms)
            new_expiry_ms = now_ms + remaining_ms
            remaining_days = remaining_ms // 86400000
        elif current_expiry_ms < 0:
            # start-after-first-use pending form → preserve as-is
            new_expiry_ms = current_expiry_ms
            remaining_days = (-current_expiry_ms) // 86400000
        else:
            # 0 = unlimited expiry → stays unlimited
            new_expiry_ms = 0
            remaining_days = None

        # ── New email: <base>_vN with N above any existing suffix on this server ──
        base_email = re.sub(r'_v\d+$', '', email)
        _max_v = 1
        for _e in known_emails:
            _m = re.match(r'^(.*)_v(\d+)$', _e or '')
            if _m and _m.group(1) == base_email:
                try:
                    _max_v = max(_max_v, int(_m.group(2)))
                except (TypeError, ValueError):
                    pass
        new_email = f"{base_email}_v{_max_v + 1}"
        while new_email in known_emails:
            _max_v += 1
            new_email = f"{base_email}_v{_max_v + 1}"

        # ── Disable the old client with a documentation comment ──
        old_comment = (target_client.get('comment') or '').strip()
        rotate_note = f"rotated -> {new_email} (uid/link revoked)"
        disabled_client = copy.deepcopy(target_client)
        disabled_client['enable'] = False
        disabled_client['comment'] = f"{old_comment} | {rotate_note}" if old_comment else rotate_note

        _is_v3 = server_is_v3(server)
        if _is_v3:
            ok, _vr, verr = v3_update_client(server, session_obj, email, disabled_client)
            if not ok:
                _release_renew_lock(_rotate_lock_key)
                return jsonify({"ok": False, "success": False, "error": f"v3 disable failed: {verr}"}), 502
        else:
            # Legacy/SS: no per-client disable endpoint — push the full inbound.
            if fetched_inbound_row is None:
                _ibs_fresh, _fe, _ = fetch_inbounds(session_obj, server.host, server.panel_type)
                if not _fe:
                    for _ib in (_ibs_fresh or []):
                        if _ib.get('id') == inbound_id:
                            fetched_inbound_row = _ib
                            break
            if fetched_inbound_row is None:
                _release_renew_lock(_rotate_lock_key)
                return jsonify({"ok": False, "success": False, "error": "Could not fetch full inbound for update"}), 502
            _full_settings = _json_field(fetched_inbound_row.get('settings'), {})
            _full_settings['clients'] = [
                disabled_client if c.get('email') == email else c
                for c in _full_settings.get('clients', [])
            ]
            _ok_push, _push_err = _push_full_inbound(server, session_obj, fetched_inbound_row, _full_settings)
            if not _ok_push:
                _release_renew_lock(_rotate_lock_key)
                return jsonify({"ok": False, "success": False, "error": f"Disable failed: {_push_err}"}), 502

        # ── Create the replacement client ──
        _is_shadowsocks_no_id = 'id' not in target_client
        new_sub_id = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
        new_client = {
            "email": new_email,
            "comment": f"rotated from {email} | uid/link replaced",
            "enable": True,
            "expiryTime": new_expiry_ms,
            "totalGB": remaining_bytes,
            "subId": new_sub_id,
            "limitIp": target_client.get('limitIp', 0),
            "flow": target_client.get('flow', ''),
            "tgId": target_client.get('tgId', ''),
            "reset": target_client.get('reset', 0),
        }
        if not _is_shadowsocks_no_id:
            new_client["id"] = str(uuid.uuid4())
        new_client_uuid = new_client.get('id') or ''

        if _is_v3:
            ok, _vr, verr = v3_add_client(server, session_obj, new_client, [inbound_id])
            if not ok:
                _release_renew_lock(_rotate_lock_key)
                return jsonify({"ok": False, "success": False, "error": f"v3 add failed: {verr}"}), 502
        else:
            _ok_add, _add_err = _add_client_to_inbound(server, session_obj, fetched_inbound_row, new_client)
            if not _ok_add:
                _release_renew_lock(_rotate_lock_key)
                return jsonify({"ok": False, "success": False, "error": f"Add failed: {_add_err}"}), 502

        # ── Ownership rows ──
        old_uuid = str(target_client.get('id') or '')
        now_utc = datetime.utcnow()
        try:
            old_so = None
            if old_uuid:
                old_so = ServiceOwnership.query.filter_by(
                    server_id=server.id, client_uuid=old_uuid).first()
            if old_so is None:
                old_so = ServiceOwnership.query.filter_by(
                    server_id=server.id, client_email_snapshot=email, revoked_at=None).first()
            if old_so is not None:
                old_so.revoked_at = now_utc
                db.session.add(ServiceOwnership(
                    customer_id=old_so.customer_id,
                    server_id=server.id,
                    client_uuid=new_client_uuid or new_email,
                    client_email_snapshot=new_email,
                    reseller_id=old_so.reseller_id,
                    verification_method=old_so.verification_method,
                    verified_by_admin_id=old_so.verified_by_admin_id,
                    verified_at=old_so.verified_at,
                ))
            # ClientOwnership has no revoke field — point it at the new identity.
            co_query = ClientOwnership.query.filter_by(server_id=server.id, client_email=email)
            for co in co_query.all():
                co.client_email = new_email
                co.client_uuid = new_client_uuid or co.client_uuid
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.error("Rotate ownership update failed for %s: %s", email, e, exc_info=True)
            _release_renew_lock(_rotate_lock_key)
            return jsonify({"ok": False, "success": False, "error": "Rotated on panel, but ownership update failed"}), 500

        invalidate_ownership_cache()
        add_cached_client(server.id, [inbound_id], new_client)
        # Mark the old client disabled in the cache too.
        try:
            if cached_client_row is not None:
                with GLOBAL_REFRESH_LOCK:
                    cached_client_row.setdefault('raw_client', {})
                    cached_client_row['raw_client']['enable'] = False
                    cached_client_row['raw_client']['comment'] = disabled_client['comment']
                    _recompute_cached_client(cached_client_row)
        except Exception:
            pass

        # ── New subscription links ──
        parsed_host = urlparse(server.host)
        final_port = server.sub_port if server.sub_port else parsed_host.port
        port_str = f":{final_port}" if final_port else ""
        base_sub = f"{parsed_host.scheme}://{parsed_host.hostname}{port_str}"
        final_id = new_sub_id or new_client_uuid
        sub_url = f"{base_sub}/{(server.sub_path or '').strip('/')}/{final_id}"
        dash_sub_url = build_public_subscription_url(
            server.id, final_id, request.url_root,
        )

        remaining_gb = None
        if current_total_bytes > 0:
            remaining_gb = round(remaining_bytes / float(1024 * 1024 * 1024), 2)

        return jsonify({
            "ok": True,
            "success": True,
            "new_email": new_email,
            "sub_url": sub_url,
            "dash_sub_url": dash_sub_url,
            "remaining_days": remaining_days,
            "remaining_gb": remaining_gb,
        })
    except Exception as e:
        app.logger.error("Rotate error for %s: %s", email, e, exc_info=True)
        _release_renew_lock(_rotate_lock_key)  # failed → allow an immediate retry
        return jsonify({"ok": False, "success": False, "error": str(e)}), 400


@bp.route('/api/client/<int:server_id>/<int:inbound_id>/<email>/renew/verify', methods=['POST'])
@login_required
def verify_renew_client(server_id, inbound_id, email):
    """Re-check a client's expiry, volume, and active state after a renew.

    Expected values are optional:
      {"expected_expiryTime": <ms>, "expected_totalGB": <bytes>,
       "expected_enable": true}
    """
    from app import (  # deferred: app-level helper, avoids circular import
        _has_client_access, _v3_sanitize_email, fetch_inbounds, find_client, get_xui_session,
        persist_detected_panel_type, server_is_v3,
    )
    trace_id = secrets.token_hex(4)
    t0 = time.perf_counter()

    def _finish(payload: dict, status_code: int = 200):
        try:
            payload.setdefault('timing', {})
            payload['timing']['total_ms'] = int((time.perf_counter() - t0) * 1000)
        except Exception:
            payload.setdefault('timing', {})
            payload['timing']['total_ms'] = None
        payload.setdefault('trace_id', trace_id)
        return jsonify(payload), status_code

    user = db.session.get(Admin, session.get('admin_id'))
    if not user:
        return _finish({'success': False, 'error': 'User not found'}, 401)

    server = Server.query.get_or_404(server_id)

    try:
        data = request.get_json() or {}
    except Exception:
        data = {}

    expected_expiry = data.get('expected_expiryTime', None)
    expected_total = data.get('expected_totalGB', None)
    expected_enable = data.get('expected_enable', True)
    awaiting_result = bool(data.get('awaiting_result', False))
    try:
        expected_expiry = None if expected_expiry is None else int(expected_expiry)
    except Exception:
        expected_expiry = None
    try:
        expected_total = None if expected_total is None else int(expected_total)
    except Exception:
        expected_total = None
    if isinstance(expected_enable, str):
        expected_enable = expected_enable.strip().lower() not in {'0', 'false', 'no', 'off'}
    else:
        expected_enable = bool(expected_enable)

    # Access control for resellers
    if user.role == 'reseller':
        if not _has_client_access(user, server_id, email, inbound_id=inbound_id):
            return _finish({'success': False, 'error': 'Access denied'}, 403)

    t_login0 = time.perf_counter()
    session_obj, error = get_xui_session(server)
    login_ms = int((time.perf_counter() - t_login0) * 1000)
    if error:
        return _finish({'success': False, 'error': error, 'timing': {'login_ms': login_ms}}, 400)

    verify = {
        'attempted': True,
        'ok': None,
        'error': None,
        'expected': {
            'expiryTime': expected_expiry,
            'totalGB': expected_total,
            'enable': expected_enable,
        },
        'observed': {'expiryTime': None, 'totalGB': None, 'enable': None},
    }

    renew_lock_key = f"renew:lock:{server_id}:{(email or '').strip().lower()}"

    try:
        t_v0 = time.perf_counter()
        inbounds, fetch_err, detected_type = fetch_inbounds(session_obj, server.host, server.panel_type)
        verify_fetch_ms = int((time.perf_counter() - t_v0) * 1000)
        persist_detected_panel_type(server, detected_type)
        if fetch_err or not inbounds:
            verify['ok'] = False
            verify['error'] = fetch_err or 'verify_fetch_failed'
            return _finish({'success': True, 'verify': verify, 'timing': {'login_ms': login_ms, 'verify_fetch_ms': verify_fetch_ms}})

        v_client, _ = find_client(inbounds, inbound_id, email)
        if not v_client and server_is_v3(server):
            # v3 stores the client email without spaces; retry the lookup with
            # the sanitized form so Re-check works after a spaced-email rename.
            _clean = _v3_sanitize_email(email)
            if _clean and _clean != email:
                v_client, _ = find_client(inbounds, inbound_id, _clean)
                if v_client:
                    email = _clean
        if not v_client:
            verify['ok'] = False
            verify['error'] = 'client_not_found'
            return _finish({'success': True, 'verify': verify, 'timing': {'login_ms': login_ms, 'verify_fetch_ms': verify_fetch_ms}})

        try:
            verify['observed']['expiryTime'] = int(v_client.get('expiryTime') or 0)
        except Exception:
            verify['observed']['expiryTime'] = None
        try:
            verify['observed']['totalGB'] = int(v_client.get('totalGB') or 0)
        except Exception:
            verify['observed']['totalGB'] = None
        verify['observed']['enable'] = bool(v_client.get('enable', True))

        completed_result = _load_renew_result(renew_lock_key)
        completed_verify = (completed_result or {}).get('verify') or {}
        completed_expected = completed_verify.get('expected') or {}

        # A duplicate/while-running request has no expected values of its own.
        # It is verified only when the original request has stored its exact
        # successful result and the panel read-back matches that result.
        if awaiting_result:
            cached_expiry = completed_expected.get('expiryTime')
            cached_total = completed_expected.get('totalGB')
            cached_enable = bool(completed_expected.get('enable', True))
            if not completed_result or (cached_expiry is None and cached_total is None):
                verify['ok'] = False
                verify['error'] = 'renew_still_in_progress'
            else:
                ok_exp = cached_expiry is None or verify['observed']['expiryTime'] == int(cached_expiry)
                ok_vol = cached_total is None or verify['observed']['totalGB'] == int(cached_total)
                ok_enable = verify['observed']['enable'] is cached_enable
                verify['expected'] = {
                    'expiryTime': cached_expiry,
                    'totalGB': cached_total,
                    'enable': cached_enable,
                }
                verify['ok'] = bool(ok_exp and ok_vol and ok_enable)
                if not verify['ok']:
                    verify['error'] = 'renew_result_not_applied_yet'
        elif expected_expiry is None and expected_total is None:
            verify['ok'] = verify['observed']['enable'] is expected_enable
        else:
            ok_exp = True if expected_expiry is None else (verify['observed']['expiryTime'] == expected_expiry)
            ok_vol = True if expected_total is None else (verify['observed']['totalGB'] == expected_total)
            ok_enable = verify['observed']['enable'] is expected_enable
            verify['ok'] = bool(ok_exp and ok_vol and ok_enable)

        payload = {'success': True, 'verify': verify,
                   'timing': {'login_ms': login_ms, 'verify_fetch_ms': verify_fetch_ms}}
        if verify.get('ok') and completed_result:
            payload.update({
                'copy_text': completed_result.get('copy_text') or '',
                'tpl_vars': completed_result.get('tpl_vars') or {},
                'client_comment': completed_result.get('client_comment') or '',
                'was_reactivated': bool(completed_result.get('was_reactivated', False)),
            })
        return _finish(payload)
    except Exception as exc:
        verify['ok'] = False
        verify['error'] = str(exc)
        return _finish({'success': True, 'verify': verify, 'timing': {'login_ms': login_ms}})


@bp.route('/api/client/qrcode', methods=['GET'])
def generate_qrcode():
    """Generate QR code from URL query parameter (GET request)"""
    from app import app  # deferred: app-level helper, avoids circular import
    link = request.args.get('link', '')
    if not link:
        return jsonify({"success": False, "error": "Link required"}), 400
    
    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()
        return jsonify({"success": True, "qrcode": f"data:image/png;base64,{qr_base64}"})
    except Exception as e:
        app.logger.error("QR Code error: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route('/api/client/<int:server_id>/qrcode', methods=['POST'])
@login_required
def client_qrcode():
    data = request.json
    url = data.get('url')
    if not url: return jsonify({"success": False}), 400
    
    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()
        return jsonify({"success": True, "qrcode": f"data:image/png;base64,{qr_base64}"})
    except:
        return jsonify({"success": False}), 400


@bp.route('/api/client/<int:server_id>/<int:inbound_id>/add', methods=['POST'])
@login_required
def add_client(server_id, inbound_id):
    from app import (  # deferred: app-level helper, avoids circular import
        CLIENT_CREATED_SMS_TEMPLATE_TYPE, DEFAULT_CLIENT_CREATED_SMS_TEMPLATE,
        GLOBAL_SERVER_DATA, INBOUND_GET_FALLBACKS, INBOUND_UPDATE_FALLBACKS,
        _account_info_channel_links, _calculate_minimum_price, _cancel_stale_account_sms,
        _fire_automation_sms, _json_field, _render_text_template, _reseller_can_create_free,
        _safe_response_json, _ss_password, _user_can_afford, add_cached_client, app,
        build_panel_url, build_public_subscription_url, calculate_reseller_price, collect_endpoint_templates,
        ensure_reseller_allowed_for_assignment, generate_client_link, get_reseller_access_maps,
        get_xui_session, invalidate_ownership_cache, is_inbound_accessible,
        is_server_accessible, log_transaction, server_is_v3, v3_add_client,
    )
    user = db.session.get(Admin, session['admin_id'])
    server = Server.query.get_or_404(server_id)
    allowed_map, assignments = get_reseller_access_maps(user) if user.role == 'reseller' else ('*', {})
    
    data = request.json or {}
    email = data.get('email', '').strip()
    
    if not email: return jsonify({"success": False, "error": "Email is required"})

    mode = data.get('mode', 'custom')
    start_after_first_use = bool(data.get('start_after_first_use', False))
    is_free = bool(data.get('free', False))
    if is_free and not _reseller_can_create_free(user):
        return jsonify({"success": False, "error": "Free creation is not permitted for your account"}), 403

    if not email: return jsonify({"success": False, "error": "Email is required"})

    price = 0
    days = 0
    volume_gb = 0
    description = ""
    pkg_name = None

    if mode == 'package':
        pkg_id = data.get('package_id')
        package = db.session.get(Package, pkg_id)
        if not package: return jsonify({"success": False, "error": "Invalid Package"}), 400

        price = calculate_reseller_price(user, package=package)
        days = package.days
        volume_gb = package.volume
        pkg_name = package.name
        description = f"Purchase Package: {package.name} - {email}"

    else:
        days = int(data.get('days', 30))
        volume_gb = int(data.get('volume', 0))
        pkg_name = 'Custom'

        reseller_context_id = user.id if user.role == 'reseller' else None
        price, _cpg, _cpd, _tier = _calculate_minimum_price(
            volume_gb,
            days,
            reseller_id=reseller_context_id,
            server_id=server_id,
            user=user,
        )
        days_label = 'Unlimited' if days == 0 else str(days)
        vol_label  = 'Unlimited' if volume_gb == 0 else str(volume_gb)
        description = f"Custom Plan: {days_label} Days, {vol_label} GB - {email}"

    if is_free:
        price = 0

    if user.role == 'reseller':
        if not is_server_accessible(server_id, allowed_map, assignments):
            return jsonify({"success": False, "error": "Access to this server is denied"}), 403
        if not is_inbound_accessible(server_id, inbound_id, allowed_map, assignments):
            return jsonify({"success": False, "error": "Access to this inbound is denied"}), 403
        
        ok, err = _user_can_afford(user, price)
        if not ok:
            return jsonify({"success": False, "error": err}), 402

    session_obj, error = get_xui_session(server)
    if error: return jsonify({"success": False, "error": error})

    try:
        client_uuid = str(uuid.uuid4())
        client_sub_id = ''.join(secrets.choice(string.ascii_letters + string.digits) for i in range(16))
        
        expiry_time = 0
        if start_after_first_use:
            expiry_time = -1 * (days * 86400000)
        elif days > 0:
            expiry_time = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
            
        new_client = {
            "id": client_uuid,
            "email": email,
            "comment": (data.get('comment') or '').strip(),
            "enable": True,
            "expiryTime": expiry_time,
            "totalGB": volume_gb * 1024 * 1024 * 1024 if volume_gb > 0 else 0,
            "subId": client_sub_id,
            "limitIp": 0,
            "flow": "",
            "tgId": "",
            "reset": 0
        }

        # ── 3x-ui v3+ : assign one client to one OR MANY inbounds in a single call.
        # Only taken when the client sent inbound_ids (v3 multi-assign UI); otherwise
        # the universal single-inbound flow below runs (works on every panel version).
        _req_inbound_ids = data.get('inbound_ids')
        if server_is_v3(server) and isinstance(_req_inbound_ids, list) and _req_inbound_ids:
            try:
                assign_ids = sorted({int(x) for x in _req_inbound_ids if x is not None})
            except Exception:
                assign_ids = []
            if not assign_ids:
                assign_ids = [inbound_id]

            ok, _vr, verr = v3_add_client(server, session_obj, new_client, assign_ids)
            if not ok:
                return jsonify({"success": False, "error": f"v3 add failed: {verr}"}), 502

            # Billing (charged once for the whole client)
            sender_card = data.get('sender_card', '') or ''
            card_id = data.get('card_id')
            if is_free:
                log_transaction(user.id, 0, 'purchase', f"Add User (Free) - {description}", server_id=server.id, sender_card=sender_card, card_id=card_id, category=('usage' if user.role == 'reseller' else 'income'), client_email=email, package_name=pkg_name, volume_gb=volume_gb, days=days)
            elif price > 0:
                if user.role == 'reseller':
                    user.credit -= price
                    log_transaction(user.id, -price, 'purchase', "Add User (Credit Usage)", server_id=server.id, sender_card=sender_card, card_id=card_id, category='usage', client_email=email, package_name=pkg_name, volume_gb=volume_gb, days=days)
                else:
                    log_transaction(user.id, price, 'purchase', "Add User (Income)", server_id=server.id, sender_card=sender_card, card_id=card_id, category='income', client_email=email, package_name=pkg_name, volume_gb=volume_gb, days=days)

            # Ownership row per assigned inbound (price recorded once on the first)
            for _idx, _iid in enumerate(assign_ids):
                db.session.add(ClientOwnership(
                    reseller_id=user.id, server_id=server.id, inbound_id=_iid,
                    client_email=email, client_uuid=client_uuid,
                    price=(price if _idx == 0 else 0)))
                try:
                    ensure_reseller_allowed_for_assignment(user, server.id, _iid)
                except Exception:
                    pass
            db.session.commit()
            invalidate_ownership_cache()
            add_cached_client(server.id, assign_ids, new_client)

            # Links from subId (one subscription aggregates all assigned inbounds)
            parsed_host = urlparse(server.host)
            final_port = server.sub_port if server.sub_port else parsed_host.port
            port_str = f":{final_port}" if final_port else ""
            base_sub = f"{parsed_host.scheme}://{parsed_host.hostname}{port_str}"
            final_id = client_sub_id or client_uuid
            sub_url = f"{base_sub}/{(server.sub_path or '').strip('/')}/{final_id}"
            dash_sub_url = build_public_subscription_url(
                server.id, final_id, request.url_root,
            )

            # Protocol label = distinct protocols across the assigned inbounds
            _protos = []
            for ib in (GLOBAL_SERVER_DATA.get('inbounds') or []):
                try:
                    if int(ib.get('server_id', -1)) == int(server.id) and int(ib.get('id', -1)) in assign_ids:
                        _protos.append(ib.get('protocol') or '')
                except Exception:
                    continue
            proto_label = ', '.join(sorted({p for p in _protos if p})) or 'vless'

            copy_text = ''
            try:
                active_tpl = NotificationTemplate.query.filter_by(type='client_created', is_active=True).first()
                if active_tpl and active_tpl.content:
                    vol_label = '♾️' if volume_gb == 0 else f'{volume_gb} GB'
                    days_label_cc = '♾️' if days == 0 else f'{days}'
                    copy_text = _render_text_template(active_tpl.content, {
                        'service_name': email, 'email': email, 'protocol': proto_label,
                        'volume': vol_label, 'days': days_label_cc, 'sub_link': sub_url,
                        'dashboard_link': dash_sub_url, 'server_name': getattr(server, 'name', '') or '',
                        'comment': data.get('comment', '') or '',
                    })
            except Exception:
                copy_text = ''

            # SMS automation (GMweb) on create — non-reseller-owned accounts only.
            _cc_days_label = ('♾️' if days == 0 else f'{days}')
            _cc_volume_label = ('♾️' if volume_gb == 0 else f'{volume_gb} GB')
            _cc_volume_num = ('♾️' if volume_gb == 0 else str(volume_gb))
            _cancel_stale_account_sms(server.id, email, reason='client_created')
            _fire_automation_sms('created', server.id, email, CLIENT_CREATED_SMS_TEMPLATE_TYPE,
                                 DEFAULT_CLIENT_CREATED_SMS_TEMPLATE, {
                                     'service_name': email, 'account_name': email, 'email': email, 'protocol': proto_label,
                                     'volume': _cc_volume_label, 'volume_label': _cc_volume_label,
                                     'days': _cc_days_label, 'days_label': _cc_days_label,
                                     # Account-info-style aliases used by many SMS templates
                                     # ({remaining_volume}GB{remaining_time}روز). Number-only so a
                                     # template that appends its own GB/روز reads right.
                                     'remaining_volume': _cc_volume_num, 'remaining_time': _cc_days_label,
                                     'sub_link': sub_url,
                                     'dashboard_link': dash_sub_url, 'server_name': getattr(server, 'name', '') or '',
                                     'comment': data.get('comment', '') or '',
                                     # No gift on creation: keep {if_gift}…{/if_gift} blocks empty.
                                     'gift_volume': '', 'gift_given': False,
                                 }, data.get('comment', '') or '',
                                 server_name=getattr(server, 'name', '') or '')

            return jsonify({
                "success": True,
                "copy_text": copy_text,
                "client": {
                    "email": email, "comment": data.get('comment', '') or '',
                    "protocol": proto_label, "volume": volume_gb, "days": days,
                    "sub_link": sub_url, "direct_link": None, "dashboard_link": dash_sub_url,
                    "inbound_ids": assign_ids,
                }
            })

        inbound_data = None
        last_fetch_error = None
        last_fetch_url = None

        for tpl in collect_endpoint_templates(server.panel_type, 'inbounds_get', INBOUND_GET_FALLBACKS):
            get_url = build_panel_url(server.host, tpl, {'id': inbound_id})
            if not get_url:
                continue
            last_fetch_url = get_url
            try:
                # Use a short connect timeout and a longer read timeout to reduce false failures on slow panels.
                get_resp = session_obj.get(get_url, verify=False, timeout=(3, 20))
            except requests.exceptions.ConnectTimeout:
                app.logger.warning("Panel connect timeout while fetching inbound (server_id=%s, host=%s, url=%s)", server.id, server.host, get_url)
                return jsonify({"success": False, "error": f"Connection timeout to panel for server '{server.name}'. Check port/firewall and panel availability."}), 504
            except requests.exceptions.ReadTimeout:
                app.logger.warning("Panel read timeout while fetching inbound (server_id=%s, host=%s, url=%s)", server.id, server.host, get_url)
                return jsonify({"success": False, "error": f"Panel response timeout for server '{server.name}'. The panel may be slow or overloaded."}), 504
            except requests.exceptions.ConnectionError as exc:
                app.logger.warning("Panel connection error while fetching inbound (server_id=%s, host=%s, url=%s): %s", server.id, server.host, get_url, exc)
                return jsonify({"success": False, "error": f"Unable to connect to panel for server '{server.name}'. Check host/port and network connectivity."}), 502

            if get_resp.status_code != 200:
                last_fetch_error = f"Unexpected status {get_resp.status_code}"
                continue

            get_json, get_err = _safe_response_json(get_resp)
            if get_err:
                last_fetch_error = get_err
                continue

            if not isinstance(get_json, dict):
                last_fetch_error = "Unexpected response shape"
                continue

            obj = get_json.get('obj')
            if obj is None:
                obj = get_json.get('data')

            if isinstance(obj, dict) and obj:
                inbound_data = obj
                break

            # Some panels wrap as {success:true, data:{...}} or return empty on wrong endpoint.
            last_fetch_error = "Empty inbound data"

        if not inbound_data:
            details = last_fetch_error or 'Failed to fetch inbound data from panel'
            if last_fetch_url:
                details = f"{details} (last url: {last_fetch_url})"
            return jsonify({
                "success": False,
                "error": f"{details}. If this is an Alireza panel, ensure endpoints like /xui/API/... are reachable and server Panel URL/webpath is correct."
            }), 502

        settings = _json_field(inbound_data.get('settings'), {})
        settings.setdefault('clients', [])

        for c in settings['clients']:
            if c['email'] == email: return jsonify({"success": False, "error": f"Email '{email}' already exists on server"})

        # Protocol-specific credentials. VLESS/VMess use `id` (already set); but
        # Shadowsocks needs a per-client method+password and Trojan needs a
        # password — without them x-ui fails: "Shadowsocks password is not specified".
        _proto = (inbound_data.get('protocol') or '').lower()
        if _proto == 'shadowsocks':
            _ss_method = settings.get('method') or 'chacha20-ietf-poly1305'
            new_client['method'] = _ss_method
            new_client['password'] = _ss_password(_ss_method)
        elif _proto == 'trojan':
            new_client['password'] = new_client.get('password') or secrets.token_urlsafe(16)

        settings['clients'].append(new_client)
        
        update_data = inbound_data.copy()
        update_data['settings'] = json.dumps(settings)

        update_ok = False
        update_error = None

        for tpl in collect_endpoint_templates(server.panel_type, 'inbounds_update', INBOUND_UPDATE_FALLBACKS):
            up_url = build_panel_url(server.host, tpl, {'id': inbound_id})
            if not up_url:
                continue
            try:
                up_resp = session_obj.post(up_url, json=update_data, verify=False, timeout=(3, 20))
            except requests.exceptions.ConnectTimeout:
                app.logger.warning("Panel connect timeout while updating inbound (server_id=%s, host=%s, url=%s)", server.id, server.host, up_url)
                return jsonify({"success": False, "error": f"Connection timeout to panel for server '{server.name}'. Check port/firewall and panel availability."}), 504
            except requests.exceptions.ReadTimeout:
                app.logger.warning("Panel read timeout while updating inbound (server_id=%s, host=%s, url=%s)", server.id, server.host, up_url)
                return jsonify({"success": False, "error": f"Panel response timeout for server '{server.name}'. The panel may be slow or overloaded."}), 504
            except requests.exceptions.ConnectionError as exc:
                app.logger.warning("Panel connection error while updating inbound (server_id=%s, host=%s, url=%s): %s", server.id, server.host, up_url, exc)
                return jsonify({"success": False, "error": f"Unable to connect to panel for server '{server.name}'. Check host/port and network connectivity."}), 502

            if up_resp.status_code != 200:
                update_error = f"Unexpected status {up_resp.status_code}"
                continue

            up_json, up_err = _safe_response_json(up_resp)
            if up_err:
                update_error = up_err
                continue
            if isinstance(up_json, dict) and up_json.get('success'):
                update_ok = True
                break

            if isinstance(up_json, dict):
                update_error = up_json.get('msg') or up_json.get('message') or 'Panel update failed'
            else:
                update_error = 'Panel update failed'

        if update_ok:

            sender_card = data.get('sender_card', '') or ''
            card_id = data.get('card_id')
            if is_free:
                if user.role == 'reseller':
                    log_transaction(user.id, 0, 'purchase', f"Add User (Free) - {description}", server_id=server.id, sender_card=sender_card, card_id=card_id, category='usage', client_email=email, package_name=pkg_name, volume_gb=volume_gb, days=days)
                else:
                    log_transaction(user.id, 0, 'purchase', f"Add User (Free) - {description}", server_id=server.id, sender_card=sender_card, card_id=card_id, category='income', client_email=email, package_name=pkg_name, volume_gb=volume_gb, days=days)
            elif price > 0:
                if user.role == 'reseller':
                    user.credit -= price
                    log_transaction(user.id, -price, 'purchase', "Add User (Credit Usage)", server_id=server.id, sender_card=sender_card, card_id=card_id, category='usage', client_email=email, package_name=pkg_name, volume_gb=volume_gb, days=days)
                else:
                    log_transaction(user.id, price, 'purchase', "Add User (Income)", server_id=server.id, sender_card=sender_card, card_id=card_id, category='income', client_email=email, package_name=pkg_name, volume_gb=volume_gb, days=days)
            
            ownership = ClientOwnership(
                reseller_id=user.id,
                server_id=server.id,
                inbound_id=inbound_id,
                client_email=email,
                client_uuid=client_uuid,
                price=price
            )
            db.session.add(ownership)

            # Keep reseller "Allowed Servers" UI in sync with ownership creation
            try:
                ensure_reseller_allowed_for_assignment(user, server.id, inbound_id)
            except Exception:
                pass

            db.session.commit()
            invalidate_ownership_cache()
            add_cached_client(server.id, [inbound_id], new_client)

            # Generate Links for Response
            parsed_host = urlparse(server.host)
            hostname = parsed_host.hostname
            scheme = parsed_host.scheme
            final_port = server.sub_port if server.sub_port else parsed_host.port
            port_str = f":{final_port}" if final_port else ""
            
            base_sub = f"{scheme}://{hostname}{port_str}"
            s_path = server.sub_path.strip('/')
            final_id = client_sub_id if client_sub_id else client_uuid
            
            sub_url = f"{base_sub}/{s_path}/{final_id}"
            app_base = request.url_root.rstrip('/')
            dash_sub_url = f"{app_base}/s/{server.id}/{final_id}"
            
            # Fetch direct link from upstream subscription instead of generating manually.
            # Short timeout so a slow/overloaded panel doesn't add seconds to every
            # "add client" call — we fall back to local generation immediately.
            direct_link = None
            try:
                sub_resp = requests.get(
                    sub_url,
                    headers={'User-Agent': 'v2rayng'},
                    timeout=(2, 3),
                    verify=False,
                    allow_redirects=False
                )
                if sub_resp.status_code == 200:
                    raw_content = sub_resp.content or b''
                    try:
                        decoded = base64.b64decode(raw_content).decode('utf-8')
                    except Exception:
                        decoded = raw_content.decode('utf-8', errors='ignore')
                    configs = [line.strip() for line in decoded.splitlines() if line.strip()]
                    if configs:
                        direct_link = configs[0]  # First config is usually the main one
            except Exception as e:
                app.logger.debug("Failed to fetch direct link from sub: %s", e)
            
            # Fallback to manual generation if upstream failed
            if not direct_link:
                direct_link = generate_client_link(new_client, inbound_data, server.host)

            # Render the active Client Created Notification template (if any).
            vol_label = '♾️' if volume_gb == 0 else f'{volume_gb} GB'
            days_label_cc = '♾️' if days == 0 else f'{days}'
            _cc_ch_links = _account_info_channel_links(db.session.get(Admin, session.get('admin_id'))) \
                if session.get('admin_id') else {'telegram_channel': '', 'whatsapp_channel': ''}
            _cc_tpl_vars = {
                'service_name': email,
                'email': email,
                'protocol': inbound_data.get('protocol', 'vless'),
                'volume': vol_label,
                'volume_label': vol_label,
                'days': days_label_cc,
                'days_label': days_label_cc,
                'sub_link': sub_url,
                'dashboard_link': dash_sub_url,
                'server_name': getattr(server, 'name', '') or '',
                'comment': data.get('comment', '') or '',
                # No gift on creation: keep {if_gift}…{/if_gift} blocks empty.
                'gift_volume': '', 'gift_given': False,
                # Account-Info-style aliases so account-info placeholders render too.
                # Number-only volume/time so a template that appends its own GB/روز
                # (e.g. "{remaining_volume}GB{remaining_time}روز") doesn't double the unit.
                'account_name': email,
                'remaining_time': days_label_cc,
                'remaining_volume': ('♾️' if volume_gb == 0 else str(volume_gb)),
                # Channel links resolved by role (superadmin→global, reseller→own).
                'telegram_channel': _cc_ch_links.get('telegram_channel', ''),
                'whatsapp_channel': _cc_ch_links.get('whatsapp_channel', ''),
            }
            copy_text = ''
            try:
                active_tpl = NotificationTemplate.query.filter_by(
                    type='client_created', is_active=True
                ).first()
                if active_tpl and active_tpl.content:
                    copy_text = _render_text_template(active_tpl.content, _cc_tpl_vars)
            except Exception:
                copy_text = ''

            # SMS automation (GMweb) on create — non-reseller-owned accounts only.
            _cancel_stale_account_sms(server.id, email, reason='client_created')
            _fire_automation_sms('created', server.id, email, CLIENT_CREATED_SMS_TEMPLATE_TYPE,
                                 DEFAULT_CLIENT_CREATED_SMS_TEMPLATE, _cc_tpl_vars, data.get('comment', '') or '',
                                 server_name=getattr(server, 'name', '') or '')

            return jsonify({
                "success": True,
                "copy_text": copy_text,
                "tpl_vars": _cc_tpl_vars,
                "client": {
                    "email": email,
                    "comment": data.get('comment', '') or '',
                    "protocol": inbound_data.get('protocol', 'vless'),
                    "volume": volume_gb,
                    "days": days,
                    "sub_link": sub_url,
                    "direct_link": direct_link,
                    "dashboard_link": dash_sub_url
                }
            })
        else:
            return jsonify({"success": False, "error": f"Panel Error: {update_error or 'Panel update failed'}"})

    except Exception as e:
        app.logger.error("Add client error (server_id=%s, inbound_id=%s): %s", server_id, inbound_id, e)
        return jsonify({"success": False, "error": str(e)})
