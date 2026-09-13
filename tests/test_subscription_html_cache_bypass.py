"""Regression coverage for browser HTML vs subscription response caching.

The human-facing /s/... page carries per-request CSP nonces. Reusing rendered HTML
across requests makes the cached body nonce disagree with the fresh CSP header and
causes the browser to reject the page-local CSS and JS.
"""
from flask import Flask

from panel.core import subscription_cache


def _app():
    app = Flask(__name__)
    app.config['TESTING'] = True
    return app


def test_browser_html_request_bypasses_subscription_cache(monkeypatch):
    monkeypatch.setenv('EVE_SUBSCRIPTION_CACHE_ENABLED', '1')
    app = _app()

    with app.test_request_context(
        '/s/9/example',
        headers={
            'User-Agent': 'Mozilla/5.0 Chrome/152.0',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        },
    ):
        assert subscription_cache.enabled() is False
        # Diagnostics must still report the configured feature state rather than
        # pretending the machine-client cache is globally disabled.
        assert subscription_cache.metrics()['enabled'] is True


def test_explicit_html_view_bypasses_cache_even_without_browser_ua(monkeypatch):
    monkeypatch.setenv('EVE_SUBSCRIPTION_CACHE_ENABLED', '1')
    app = _app()

    with app.test_request_context(
        '/s/9/example?view=1',
        headers={'User-Agent': 'curl/8.10', 'Accept': '*/*'},
    ):
        assert subscription_cache.enabled() is False


def test_vpn_client_response_cache_remains_enabled(monkeypatch):
    monkeypatch.setenv('EVE_SUBSCRIPTION_CACHE_ENABLED', '1')
    app = _app()

    with app.test_request_context(
        '/s/9/example',
        headers={'User-Agent': 'v2rayNG/1.10', 'Accept': '*/*'},
    ):
        assert subscription_cache.enabled() is True


def test_non_html_mozilla_request_keeps_machine_client_classification(monkeypatch):
    monkeypatch.setenv('EVE_SUBSCRIPTION_CACHE_ENABLED', '1')
    app = _app()

    with app.test_request_context(
        '/s/9/example',
        headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'},
    ):
        assert subscription_cache.enabled() is True


def test_cache_still_works_outside_request_context(monkeypatch):
    monkeypatch.setenv('EVE_SUBSCRIPTION_CACHE_ENABLED', '1')
    subscription_cache.reset()

    assert subscription_cache.enabled() is True
    assert subscription_cache.set('9:example:full', (b'payload', 200, {}), ttl=5)
    assert subscription_cache.get('9:example:full')[0] == b'payload'
