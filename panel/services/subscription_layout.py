"""Template choice and block order of the public subscription page.

Stored as one JSON document in SystemConfig so the admin can pick one of the
page templates and, per template, the order and visibility of the page blocks.
"""
import json

LAYOUT_KEY = 'subscription_page_layout'

TEMPLATES = ('classic', 'minimal', 'dashboard', 'glass', 'compact')
DEFAULT_TEMPLATE = 'classic'

BLOCKS = ('status', 'renew', 'backup', 'usage', 'link', 'history', 'faq', 'config')
# The page script binds to these blocks (status banner, live usage, app guide), so they
# can be moved but never hidden.
REQUIRED_BLOCKS = frozenset({'status', 'usage', 'link'})


def default_blocks():
    return [{'id': block, 'visible': True} for block in BLOCKS]


def normalize_blocks(raw):
    """Known blocks in the given order, each once; missing blocks are appended visible."""
    seen = set()
    blocks = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            block_id, visible = item, True
        elif isinstance(item, dict):
            block_id, visible = item.get('id'), item.get('visible', True)
        else:
            continue
        if block_id not in BLOCKS or block_id in seen:
            continue
        seen.add(block_id)
        blocks.append({
            'id': block_id,
            'visible': True if block_id in REQUIRED_BLOCKS else visible is not False,
        })
    blocks.extend({'id': block, 'visible': True} for block in BLOCKS if block not in seen)
    return blocks


def normalize_layout(raw):
    data = raw if isinstance(raw, dict) else {}
    template = data.get('template')
    if template not in TEMPLATES:
        template = DEFAULT_TEMPLATE
    raw_layouts = data.get('layouts') if isinstance(data.get('layouts'), dict) else {}
    layouts = {name: normalize_blocks(raw_layouts.get(name)) for name in TEMPLATES}
    return {'template': template, 'layouts': layouts}


def parse_layout(value):
    try:
        return normalize_layout(json.loads(value) if value else None)
    except (TypeError, ValueError):
        return normalize_layout(None)


def dump_layout(layout):
    return json.dumps(normalize_layout(layout), separators=(',', ':'))


def page_layout(layout, template=None):
    """What the subscription page renders: the template name and its block list."""
    name = template if template in TEMPLATES else layout['template']
    return {'template': name, 'blocks': layout['layouts'][name]}


def load_layout():
    from panel.extensions import db
    from panel.models import SystemConfig
    row = db.session.get(SystemConfig, LAYOUT_KEY)
    return parse_layout(row.value if row else None)
