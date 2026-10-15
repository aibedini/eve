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
    'curl', 'sms:', 'wa.me',
    '3X-UI (Sanaei)', 'X-UI (Alireza)', 'Pulse', 'BNQO', 'Redis', 'PostgreSQL', 'OK', 'N/A', 'Clash', 'QR', 'VLESS', 'VMess',
}
USED = re.compile(r'''(?<![\w.])_t?\(\s*(?:'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)")''')


def normalize(text):
    return ' '.join(html.unescape(text).split())


PERSIAN = re.compile('[؀-ۿ]')


def is_translatable(text):
    if PERSIAN.search(text):
        return False  # already Persian
    # A list of {variables} is code, not a sentence.
    if not (LETTERS.search(re.sub(r'\{[^}]*\}', '', text)) or text.startswith('e.g.')) or text in KEEP:
        return False
    # A URL path is data too.
    if text.startswith('/') and ' ' not in text:
        return False
    # An example value (card number, e-mail address) is data, not a sentence.
    if ' ' not in text and re.search(r'[\d@]', text):
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
    in_language_block = False
    for index, (kind, start, end) in enumerate(parts):
        chunk = source[start:end]
        if kind == 'jinja':
            if re.match(r'\{%-?\s*if\s[^%]*panel_lang', chunk):
                in_language_block = True
            elif in_language_block and re.match(r'\{%-?\s*endif', chunk):
                in_language_block = False
        if in_language_block:
            continue  # a region already written once per language
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


INTERPOLATED = True  # also convert plain interpolated sentences
SCRIPT_BLOCK = re.compile(r'(<script(?![^>]*\bsrc=)[^>]*>)(.*?)(</script>)', re.S)
JS_ATTRIBUTE = re.compile(r'\b(title|placeholder|aria-label|alt)="([^"\n]*)"')
JS_TEXT_NODE = re.compile(r'>([^<>]*?)<')
ANCHOR = re.compile(
    r'(?:\b(?:showToast|alert|confirm|prompt)\(\s*|\bnew Error\(\s*'
    r'|\.(?:textContent|innerText|title|placeholder)\s*=\s*)$')
_REGEX_BEFORE = set('(,=:[!&|?{};+-*%<>~^')


def literal_spans(js, templates=None):
    """Yield (quote, start, end, whole) for every string literal body and template quasi.

    `whole` is (open, close) positions of the complete literal when it has no
    interpolation, else None. Handles nested templates, comments and regex literals.
    """
    spans = []
    n = len(js)

    def scan_string(i, quote):
        start = i + 1
        i = start
        while i < n and js[i] != quote:
            i += 2 if js[i] == '\\' else 1
        return start, i

    def scan_code(i, stop_at_brace):
        """Scan code from i; return index of the closing } when stop_at_brace else n."""
        depth = 0
        last = ''
        while i < n:
            c = js[i]
            if c in '\'"':
                start, end = scan_string(i, c)
                spans.append((c, start, end, (i, end)))
                i = end + 1
                last = c
                continue
            if c == '`':
                i = scan_template(i)
                last = 'x'
                continue
            if c == '/' and i + 1 < n and js[i + 1] == '/':
                while i < n and js[i] != '\n':
                    i += 1
                continue
            if c == '/' and i + 1 < n and js[i + 1] == '*':
                end = js.find('*/', i + 2)
                i = n if end < 0 else end + 2
                continue
            if c == '/' and (last == '' or last in _REGEX_BEFORE or js[max(0, i - 7):i].rstrip().endswith('return')):
                i += 1
                in_class = False
                while i < n and (js[i] != '/' or in_class):
                    if js[i] == '\\':
                        i += 1
                    elif js[i] == '[':
                        in_class = True
                    elif js[i] == ']':
                        in_class = False
                    elif js[i] == '\n':
                        break
                    i += 1
                i += 1
                last = 'x'
                continue
            if stop_at_brace:
                if c == '{':
                    depth += 1
                elif c == '}':
                    if depth == 0:
                        return i
                    depth -= 1
            if not c.isspace():
                last = c
            i += 1
        return n

    def scan_template(i):
        """i is at the opening backtick; return index after the closing backtick."""
        opening = i
        i += 1
        quasi = i
        parts = []
        exprs = []
        while i < n:
            c = js[i]
            if c == '\\':
                i += 2
                continue
            if c == '`':
                parts.append((quasi, i))
                whole = (opening, i) if len(parts) == 1 else None
                if templates is not None:
                    templates.append((opening, i, list(parts), list(exprs)))
                for start, end in parts:
                    spans.append(('`', start, end, whole))
                return i + 1
            if c == '$' and i + 1 < n and js[i + 1] == '{':
                parts.append((quasi, i))
                expression_start = i + 2
                i = scan_code(i + 2, True)
                exprs.append((expression_start, i))
                i += 1
                quasi = i
                continue
            i += 1
        return n

    scan_code(0, False)
    return sorted(spans, key=lambda s: s[1])


def wrap_js_block(js):
    """Wrap unambiguous visible strings of one script body; return (new_js, keys, skipped)."""
    keys, skipped, edits = [], [], []
    for quote_char, start, end, whole in literal_spans(js):
        body = js[start:end]
        # 1. a whole literal in a context that is certainly shown to the user
        if whole is not None and '<' not in body and '\n' not in body and ANCHOR.search(js[max(0, whole[0] - 40):whole[0]]):
            key = normalize(re.sub(r'\\(.)', r'\1', body))
            if is_translatable(key) and '${' not in body:
                edits.append((whole[0], whole[1] + 1, "_t(%s)" % quote(key)))
                keys.append(key)
                continue
        if '<' not in body or PERSIAN.search(body):
            continue  # no HTML, or the already-translated half of a per-language pair
        # 2. visible text and attributes inside HTML built in a string
        for node in JS_TEXT_NODE.finditer(body):
            raw = node.group(1)
            key = normalize(raw)
            if not key or '\\' in raw or not is_translatable(key):
                continue
            lead = raw[:len(raw) - len(raw.lstrip())]
            trail = raw[len(raw.rstrip()):]
            expr = "_t(%s)" % quote(key)
            piece = lead + ('${%s}' % expr if quote_char == '`' else "%s + %s + %s" % (quote_char, expr, quote_char)) + trail
            edits.append((start + node.start(1), start + node.end(1), piece))
            keys.append(key)
        for attribute in JS_ATTRIBUTE.finditer(body):
            key = normalize(attribute.group(2))
            if not key or '\\' in attribute.group(2) or not is_translatable(key):
                continue
            expr = "_t(%s)" % quote(key)
            piece = '${%s}' % expr if quote_char == '`' else "%s + %s + %s" % (quote_char, expr, quote_char)
            edits.append((start + attribute.start(2), start + attribute.end(2), piece))
            keys.append(key)
    out, cursor = [], 0
    for start, end, text in sorted(edits):
        if start < cursor:
            continue
        out.append(js[cursor:start])
        out.append(text)
        cursor = end
    out.append(js[cursor:])
    return ''.join(out), keys, skipped


NOT_UI_CALL = re.compile(
    r'(?:querySelector(?:All)?|getElementById|fetch|closest|matches|setAttribute|getAttribute|dataset|'
    r'console\.\w+|URL|URLSearchParams|RegExp|encodeURIComponent|Map|Set|\.get|\.set|\.has|\.delete|'
    r'[Kk]ey|\.push|\.add|\.remove|\.toggle|\.contains|import|require)\(\s*$')
NOT_UI_TEXT = re.compile(r'px|rgba?\(|solid|translate|calc\(|var\(--|\.css|\.js|\.json|https?:|data-|aria-|href|onclick')


def interpolated_sentence(js, opening, quasis, exprs):
    """(key, args) when a template literal is a plain sentence around values, else None."""
    if not exprs or len(quasis) != len(exprs) + 1:
        return None
    texts = [js[a:b] for a, b in quasis]
    joined = ''.join(texts)
    if re.search(r'[<>=/\\;{}&\n\r`]', joined) or joined.lstrip().startswith('?') or NOT_UI_TEXT.search(joined):
        return None
    if NOT_UI_CALL.search(js[max(0, opening - 60):opening]):
        return None
    # CSS class lists, file names, hashtags: not sentences.
    if (re.match(r'\s*[a-z][a-z0-9]*(?:-[a-z0-9]+)+(?:\s|$)', joined) or re.search(r'\.(?:db|txt|csv|json)\b|[#_]', joined)):
        return None
    names, args = [], []
    for index, (a, b) in enumerate(exprs):
        code = js[a:b].strip()
        if not code or '`' in code or '\n' in code or len(code) > 120:
            return None
        if re.fullmatch(r'[A-Z][A-Z0-9_]+', code):
            return None  # an HTML/SVG constant, not a value to put in a sentence
        name = code if re.fullmatch(r'[A-Za-z_]\w*', code) else 'v%d' % (index + 1)
        if name in names:
            name = 'v%d' % (index + 1)
        names.append(name)
        args.append((name, code))
    key = ''
    for index, text in enumerate(texts):
        key += text
        if index < len(names):
            key += '{%s}' % names[index]
    if re.search(r'[A-Za-z]\{', key):
        return None  # a value glued to a word, e.g. English plural suffixes
    bare = re.sub(r'\{[^}]*\}', '', key)
    if not re.search(r'[A-Za-z]{3,}', bare) or not is_translatable(normalize(bare)):
        return None
    return key, args


def wrap_interpolated(js):
    """Convert plain interpolated sentences to _t('... {name} ...', {name: value}); (new, keys)."""
    templates = []
    literal_spans(js, templates)
    edits, keys = [], []
    for opening, closing, quasis, exprs in templates:
        found = interpolated_sentence(js, opening, quasis, exprs)
        if found is None:
            continue
        key, args = found
        values = ', '.join(name if name == code else '%s: %s' % (name, code) for name, code in args)
        edits.append((opening, closing + 1, "_t(%s, {%s})" % (quote(key), values)))
        keys.append(key)
    out, cursor = [], 0
    for start, end, text in sorted(edits):
        if start < cursor:
            continue
        out.append(js[cursor:start])
        out.append(text)
        cursor = end
    out.append(js[cursor:])
    return ''.join(out), keys


def wrap_js(source):
    """Apply wrap_js_block to every inline script of a template."""
    keys, skipped = [], []

    def one(match):
        new, k, sk = wrap_js_block(match.group(2))
        new, more = wrap_interpolated(new) if INTERPOLATED else (new, [])
        keys.extend(k + more)
        skipped.extend(sk)
        return match.group(1) + new + match.group(3)

    return SCRIPT_BLOCK.sub(one, source), keys, skipped


def check_js(source):
    """Syntax-check every inline script with node (Jinja tags neutralised). [] when fine."""
    import shutil
    import subprocess
    import tempfile
    node = shutil.which('node')
    if not node:
        return ['node not found']
    problems = []
    for index, match in enumerate(SCRIPT_BLOCK.finditer(source)):
        js = match.group(2)
        js = re.sub(r'\{#.*?#\}', '', js, flags=re.S)
        js = re.sub(r'\{%.*?%\}', '', js, flags=re.S)
        js = re.sub(r'\{\{.*?\}\}', '0', js, flags=re.S)
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
            handle.write(js)
        result = subprocess.run([node, '--check', handle.name], capture_output=True, text=True)
        Path(handle.name).unlink()
        if result.returncode:
            lines = result.stderr.strip().splitlines()
            problems.append('script %d: %s' % (index, ' | '.join(lines[:4])))
    return problems


def main(argv):
    write = '--write' in argv
    js_mode = '--js' in argv
    status = 0
    for name in [a for a in argv if not a.startswith('--')]:
        path = Path(name)
        source = path.read_bytes().decode('utf-8')
        newline = '\r\n' if '\r\n' in source else '\n'
        source = source.replace('\r\n', '\n')
        if js_mode:
            updated, keys, skipped = wrap_js(source)
            problems = check_js(updated)
            print('%s: %d script strings wrapped, %d syntax problems' % (name, len(keys), len(problems)))
            for problem in problems:
                print('   SYNTAX:', problem)
            status |= bool(problems)
            if write and not problems and keys:
                path.write_bytes(updated.replace('\n', newline).encode('utf-8'))
            continue
        updated, keys, mixed = wrap(source)
        print('%s: %d simple, %d mixed (manual)' % (name, len(keys), len(mixed)))
        for key in mixed:
            print('   MIXED:', key)
        if write and keys:
            path.write_bytes(updated.replace('\n', newline).encode('utf-8'))
        status |= bool(mixed)
    return status


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
