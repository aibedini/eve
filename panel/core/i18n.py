"""Panel-wide translation: English text is the key, Persian is looked up by panel language.

Templates write ``{{ _('Add Server') }}`` and inline scripts write ``_t('Saved')``. In an
English panel the call returns its argument unchanged, so English output cannot drift;
in a Persian panel it returns the catalog entry. Catalogs live in ``panel/i18n/<page>.py``
(one per page, plus ``common`` for shared words) and a lookup searches all of them, so a
word is translated the same way on every page. ``{name}`` placeholders are filled by
keyword arguments here and by the ``vars`` object in the browser.

``tests/test_i18n_pages.py`` fails when a converted page uses a string its catalog does
not translate, when a catalog entry is unused, or when hardcoded text is left in markup.
"""
import re

_PLACEHOLDER = re.compile(r'\{(\w+)\}')
_CATALOGS = {}


def register(module, fa):
    """Register the Persian catalog (English text -> Persian text) of one module."""
    _CATALOGS[module] = dict(fa)


def modules():
    return dict(_CATALOGS)


def catalog(*names):
    """Merged catalog of ``common`` plus the named modules (what the browser receives)."""
    merged = dict(_CATALOGS.get('common', {}))
    for name in names:
        merged.update(_CATALOGS.get(name, {}))
    return merged


def _fill(text, values):
    if not values:
        return text
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def translate(text, lang, **values):
    if lang == 'fa':
        for module in _CATALOGS.values():
            if text in module:
                return _fill(module[text], values)
    return _fill(text, values)


def inject_i18n():
    """Jinja context: ``_()`` for markup and ``i18n_catalog(page)`` for the page script."""
    from app import _get_panel_ui_lang  # deferred: app-level helper, avoids circular import
    try:
        lang = _get_panel_ui_lang()
    except Exception:
        lang = 'en'
    return {
        '_': lambda text, **values: translate(text, lang, **values),
        'i18n_catalog': lambda *pages: catalog(*pages) if lang == 'fa' else {},
    }
