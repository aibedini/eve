"""Find and wrap hardcoded English in Jinja templates (see panel/core/i18n.py).

    python scripts/i18n_scan.py templates/monitor.html            # report only
    python scripts/i18n_scan.py --write templates/monitor.html    # wrap simple text

Only plain markup text and the placeholder/title/aria-label/alt attributes are wrapped,
as ``{{ _('English text') }}``. Text mixed with ``{{ expression }}`` on the same line of
prose, and everything inside <script>, is reported for a manual ``_('..', name=value)``
or ``_t('..', {name: value})`` instead, because a sentence must be translated whole.
"""
import html
import re
import sys
from pathlib import Path

TOKEN = re.compile(
    r'(\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}|<script\b.*?</script>|<style\b.*?</style>'
    r'|<!--.*?-->|<[^>]+>)', re.S)
ATTRIBUTE = re.compile(r'''\b(placeholder|title|aria-label|alt)=(")([^"]*)"''')
LETTERS = re.compile(r'[A-Za-z]{2,}')
# Text that is a name, a unit or a code rather than a sentence to translate.
KEEP = {
    'Eve', 'EVE', 'GMweb', 'SMS', 'X-UI', '3X-UI', 'Telegram', 'WhatsApp', 'WebAuthn', 'MFA',
    'TOTP', 'JSON', 'CSV', 'API', 'URL', 'UUID', 'IP', 'GB', 'MB', 'TB', 'Xray', 'xray', 'HWID',
    'Pulse', 'BNQO', 'Redis', 'PostgreSQL', 'OK', 'N/A', 'Clash', 'QR', 'VLESS', 'VMess',
}
USED = re.compile(r'''(?<![\w.])_t?\(\s*(?:'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)")''')


def normalize(text):
    return ' '.join(html.unescape(text).split())


def is_translatable(text):
    # A list of {variables} is code, not a sentence.
    if not LETTERS.search(re.sub(r'\{[^}]*\}', '', text)) or text in KEEP:
        return False
    return not all(word in KEEP for word in re.findall(r'[\w\-]+', text))


def pieces(source):
    """Yield ('tag'|'jinja'|'text'|'skip', start, end) covering the whole source."""
    cursor = 0
    for match in TOKEN.finditer(source):
        if match.start() > cursor:
            yield 'text', cursor, match.start()
        text = match.group(0)
        kind = 'jinja' if text[:2] in ('{#', '{%', '{{') else (
            'skip' if text[:7].lower() in ('<script', '<style>') or text.startswith('<!--')
            or text.lower().startswith('<style') else 'tag')
        yield kind, match.start(), match.end()
        cursor = match.end()
    if cursor < len(source):
        yield 'text', cursor, len(source)


def candidates(source):
    """Every untranslated string in the markup: [(kind, key, start, end, mixed)]."""
    found = []
    parts = list(pieces(source))
    for index, (kind, start, end) in enumerate(parts):
        chunk = source[start:end]
        if kind == 'tag':
            for match in ATTRIBUTE.finditer(chunk):
                value = match.group(3)
                if '{{' in value or '{%' in value or not is_translatable(normalize(value)):
                    continue
                found.append(('attr', normalize(value), start + match.start(3), start + match.end(3), False))
        elif kind == 'text':
            key = normalize(chunk)
            if not key or not is_translatable(key):
                continue
            # A neighbouring {{ expression }} (ignoring blank text) means the sentence
            # is built around a value, so it needs a hand-written placeholder.
            def adjacent(step):
                j = index + step
                while 0 <= j < len(parts):
                    k, s, e = parts[j]
                    if k == 'text' and not source[s:e].strip():
                        j += step
                        continue
                    return k == 'jinja' and source[s:s + 2] == '{{'
                return False
            found.append(('text', key, start, end, adjacent(-1) or adjacent(1)))
    return found


def quote(key):
    return "'" + key.replace('\\', '\\\\').replace("'", "\\'") + "'"


def wrap(source):
    """Return (new_source, wrapped_keys, mixed) with simple text and attributes wrapped."""
    out = []
    cursor = 0
    keys = []
    mixed = []
    for kind, key, start, end, is_mixed in candidates(source):
        if is_mixed:
            mixed.append(key)
            continue
        out.append(source[cursor:start])
        if kind == 'attr':
            out.append("{{ _(%s) }}" % quote(key))
        else:
            chunk = source[start:end]
            lead = chunk[:len(chunk) - len(chunk.lstrip())]
            trail = chunk[len(chunk.rstrip()):]
            out.append("%s{{ _(%s) }}%s" % (lead, quote(key), trail))
        keys.append(key)
        cursor = end
    out.append(source[cursor:])
    return ''.join(out), keys, mixed


def used_keys(source):
    """Literal keys passed to _() / _t() anywhere in a file (markup or inline script)."""
    keys = set()
    for match in USED.finditer(source):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        keys.add(re.sub(r'\\(.)', r'\1', raw))
    return keys


def main(argv):
    write = '--write' in argv
    status = 0
    for name in [a for a in argv if not a.startswith('--')]:
        path = Path(name)
        source = path.read_bytes().decode('utf-8')
        newline = '\r\n' if '\r\n' in source else '\n'
        updated, keys, mixed = wrap(source.replace('\r\n', '\n'))
        print('%s: %d simple, %d mixed (manual)' % (name, len(keys), len(mixed)))
        for key in mixed:
            print('   MIXED:', key)
        if write and keys:
            path.write_bytes(updated.replace('\n', newline).encode('utf-8'))
        status |= bool(mixed)
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
