"""The Subscription page owns its own geometry, and no new global rule may reach it.

Two failure modes are guarded here:

* the page had 18 bare ``<svg>`` elements (no class, no width/height). A bare svg with no rule
  falls back to the replaced element's intrinsic size - 300x150 in browsers - which is exactly
  the "Support and QR icons became huge" report. Every icon container the page actually uses now
  has an explicit, scoped size;
* ``static/style.css`` and ``static/tailwind.generated.css`` are global. A rule of the shape
  ``.action-btn svg`` (the earlier icon regression) can reach this page from anywhere, so the
  current at-risk set is frozen in ``tests/design_global_selector_baseline.json`` and any
  addition fails until it is either scoped or deliberately accepted.
"""
import io
import json
import os
import re
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE = os.path.join(_REPO_ROOT, 'templates', 'subscription.html')
_BASELINE = os.path.join(_REPO_ROOT, 'tests', 'design_global_selector_baseline.json')
_SHEETS = (os.path.join(_REPO_ROOT, 'static', 'style.css'),
           os.path.join(_REPO_ROOT, 'static', 'tailwind.generated.css'))

GENERIC = re.compile(r'(^|[\s,>+~])(svg|img|picture|canvas|video)\b|\.(icon|card|container|'
                     r'tile|panel|box|widget|grid|flex|row|column|col)\b', re.IGNORECASE)
GEOMETRY = re.compile(r'\b(width|height|min-width|min-height|max-width|max-height|display|'
                      r'grid|flex|fill|stroke|padding|margin)\s*:', re.IGNORECASE)
COMMENT = re.compile(r'/\*.*?\*/', re.DOTALL)

# Containers whose bare svgs the page sizes itself (the inventory that produced these).
EXPECTED_SCOPED_CONTAINERS = (
    '.pwa-nav-item svg',
    '.fab-circle svg',
    '.renew-ch-btn svg',
    '.pwa-sheet-btn svg',
    '#support-sheet svg',
    '#toast > svg',
    '#qr-modal svg',
)


def _read(path):
    with io.open(path, encoding='utf-8') as handle:
        return handle.read()


def _rules(css):
    for match in re.finditer(r'([^{}]+)\{([^{}]*)\}', css):
        selector = ' '.join(match.group(1).split())
        if selector and not selector.startswith('@'):
            yield selector, match.group(2)


def _at_risk_selectors():
    found = []
    for path in _SHEETS:
        css = COMMENT.sub(' ', _read(path))
        for selector, body in _rules(css):
            if GENERIC.search(selector) and GEOMETRY.search(body):
                found.append({'file': os.path.relpath(path, _REPO_ROOT).replace(os.sep, '/'),
                              'selector': selector})
    return sorted(found, key=lambda row: (row['file'], row['selector']))


def _page_style_blocks():
    template = _read(_TEMPLATE)
    blocks = re.findall(r'<style[^>]*>(.*?)</style>', template, re.DOTALL)
    # Comments may mention svg (this file's own explanation does); never treat them as rules.
    return [COMMENT.sub(' ', block) for block in blocks], template


def _markup_only(template):
    """The template without <style>/<script> bodies, so CSS/JS text is not scanned as markup."""
    stripped = re.sub(r'<style[^>]*>.*?</style>', ' ', template, flags=re.DOTALL)
    return re.sub(r'<script[^>]*>.*?</script>', ' ', stripped, flags=re.DOTALL)


class SubscriptionScopeTests(unittest.TestCase):
    def test_the_page_is_namespaced(self):
        _blocks, template = _page_style_blocks()
        body = re.search(r'<body[^>]*>', template).group(0)
        self.assertIn('subscription-page', body)

    def test_every_svg_rule_the_page_ships_is_scoped(self):
        blocks, _template = _page_style_blocks()
        offenders = []
        for block in blocks:
            for selector, body in _rules(block):
                if 'svg' not in selector and 'img' not in selector:
                    continue
                if GEOMETRY.search(body) and not selector.startswith('.subscription-page'):
                    offenders.append(selector)
        self.assertEqual(offenders, [], 'unscoped icon rules in the subscription page: %s'
                         % offenders)

    def test_the_known_icon_containers_have_explicit_sizes(self):
        blocks, _template = _page_style_blocks()
        css = '\n'.join(blocks)
        for container in EXPECTED_SCOPED_CONTAINERS:
            self.assertIn('.subscription-page ' + container, css, container)
            # The container may be part of a grouped selector list.
            pattern = (r'[^{}]*' + re.escape(container.strip()) + r'[^{}]*\{([^}]*)\}')
            match = re.search(pattern, css)
            self.assertIsNotNone(match, container)
            body = match.group(1)
            self.assertRegex(body, r'width:\s*\d+px', container)
            self.assertRegex(body, r'height:\s*\d+px', container)

    def test_no_new_global_selector_can_reach_the_page(self):
        with io.open(_BASELINE, encoding='utf-8') as handle:
            baseline = json.load(handle)
        current = _at_risk_selectors()
        baseline_keys = {(row['file'], row['selector']) for row in baseline}
        current_keys = {(row['file'], row['selector']) for row in current}
        added = sorted(current_keys - baseline_keys)
        self.assertEqual(
            added, [],
            'new global selectors that can resize icons or layout: %s - scope them under the '
            'page that owns them, or update tests/design_global_selector_baseline.json on '
            'purpose' % added)
        # A removed rule is allowed (it is a shrinking risk), and the baseline must not drift
        # silently in the other direction either.
        self.assertTrue(current_keys.issubset(baseline_keys))

    def test_the_page_does_not_lean_on_an_unscoped_bare_svg(self):
        """Bare svgs in the markup must sit inside a container the page sizes itself."""
        _blocks, template = _page_style_blocks()
        markup = _markup_only(template)
        scoped = '\n'.join(_blocks)
        uncovered = []
        for match in re.finditer(r'<svg[^>]*>', markup):
            tag = match.group(0)
            if 'class=' in tag or 'width=' in tag or 'style=' in tag:
                continue
            before = markup[max(0, match.start() - 400):match.start()]
            containers = re.findall(r'class="([^"]{0,120})"', before)
            container = containers[-1] if containers else ''
            if 'subscription-page' in container:
                continue
            # A bare svg is only safe when its container has a scoped size rule.
            covered = False
            for token in re.findall(r'[.#][A-Za-z0-9_-]+', container):
                if token.startswith('.'):
                    token = token[1:]
                if token and re.search(r'\.subscription-page[^{]*' + re.escape(token) + r'[^{]*svg',
                                       scoped):
                    covered = True
                    break
            self.assertTrue(re.search(r'\.subscription-page [^{]*svg', scoped))
            if not covered:
                uncovered.append(container[:60] or 'unknown')
        self.assertEqual(uncovered, [], 'bare svgs with no scoped size: %s' % uncovered)


if __name__ == '__main__':
    unittest.main()
