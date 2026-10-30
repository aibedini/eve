/*
 * Declarative event handlers without inline script.
 *
 * The CSP forbids inline event-handler attributes (onclick="..."), so a markup
 * injection cannot run script. Templates write data-onclick="fn(arg, this)"
 * instead, and this file runs it with a small interpreter -- never eval/Function.
 *
 * The grammar is deliberately narrow:
 *   handler   := statement (';' statement)*
 *   statement := ['return'] expr
 *   expr      := '!' expr | '-' expr | postfix
 *   postfix   := primary ('.' name | '[' expr ']' | '(' args ')')*
 *   primary   := number | 'string' | true | false | null | undefined
 *              | this | event | name | '{' key ':' expr, ... '}' | '[' expr, ... ']' | '(' expr ')'
 * A call may only target a page-defined global function (never a native one
 * such as eval, fetch or setTimeout), event.stopPropagation/preventDefault, or
 * document.getElementById. Property reads are allowed; assignments are not.
 * "return false" (or a handler that returns false) cancels the event, as the
 * inline attribute did.
 *
 * Elements are bound lazily: a capture listener on document sees every event
 * before it reaches its target and attaches a real listener to each element on
 * the path that carries data-on<type>. Listeners added before the dispatch
 * reaches an element still run, so the handler fires in the target phase with
 * the same this/stopPropagation semantics the inline attribute had.
 */
(function () {
    'use strict';

    var EVENTS = ['click', 'dblclick', 'change', 'input', 'submit', 'keydown', 'keyup', 'focus',
        'blur', 'error', 'load', 'dragstart', 'dragend', 'dragover', 'dragleave', 'drop',
        'mouseenter', 'mouseleave', 'paste'];
    var BLOCKED_NAMES = { $: 1, jQuery: 1, eval: 1, Function: 1 };
    var nativeToString = Function.prototype.toString;
    var cache = new Map();
    var bound = new WeakMap();

    // ── Tokenizer ──────────────────────────────────────────────────────────
    function tokenize(src) {
        var tokens = [];
        var i = 0;
        while (i < src.length) {
            var c = src[i];
            if (/\s/.test(c)) { i++; continue; }
            if (/[0-9]/.test(c) || (c === '.' && /[0-9]/.test(src[i + 1] || ''))) {
                var m = /^(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/.exec(src.slice(i));
                tokens.push({ t: 'num', v: Number(m[0]) });
                i += m[0].length;
                continue;
            }
            if (/[A-Za-z_$]/.test(c)) {
                var id = /^[A-Za-z_$][\w$]*/.exec(src.slice(i))[0];
                tokens.push({ t: 'id', v: id });
                i += id.length;
                continue;
            }
            if (c === '"' || c === "'") {
                var out = '';
                i++;
                while (i < src.length && src[i] !== c) {
                    if (src[i] === '\\') {
                        var n = src[i + 1];
                        if (n === 'n') out += '\n';
                        else if (n === 't') out += '\t';
                        else if (n === 'u') { out += String.fromCharCode(parseInt(src.substr(i + 2, 4), 16)); i += 4; }
                        else out += n;
                        i += 2;
                    } else {
                        out += src[i++];
                    }
                }
                if (src[i] !== c) throw new SyntaxError('unterminated string');
                i++;
                tokens.push({ t: 'str', v: out });
                continue;
            }
            if ('(),.;{}:[]!-'.indexOf(c) >= 0) {
                tokens.push({ t: 'p', v: c });
                i++;
                continue;
            }
            throw new SyntaxError('unexpected "' + c + '"');
        }
        return tokens;
    }

    // ── Parser (to a small AST) ────────────────────────────────────────────
    function parse(src) {
        var tokens = tokenize(src);
        var pos = 0;
        function peek(v) { var t = tokens[pos]; return t && (v === undefined || t.v === v) ? t : null; }
        function eat(v) {
            var t = tokens[pos];
            if (!t || (v !== undefined && t.v !== v)) throw new SyntaxError('expected ' + v);
            pos++;
            return t;
        }
        function list(close) {
            var items = [];
            if (peek(close) && tokens[pos].t === 'p') { pos++; return items; }
            for (;;) {
                items.push(expr());
                if (peek(',')) { pos++; if (peek(close)) { pos++; return items; } continue; }
                eat(close);
                return items;
            }
        }
        function primary() {
            var t = eat();
            if (t.t === 'num' || t.t === 'str') return { k: 'lit', v: t.v };
            if (t.t === 'id') {
                if (t.v === 'true') return { k: 'lit', v: true };
                if (t.v === 'false') return { k: 'lit', v: false };
                if (t.v === 'null') return { k: 'lit', v: null };
                if (t.v === 'undefined') return { k: 'lit', v: undefined };
                if (t.v === 'this' || t.v === 'event' || t.v === 'document') return { k: t.v };
                return { k: 'global', name: t.v };
            }
            if (t.v === '(') { var e = expr(); eat(')'); return e; }
            if (t.v === '[') return { k: 'array', items: list(']') };
            if (t.v === '{') {
                var props = [];
                if (peek('}')) { pos++; return { k: 'object', props: props }; }
                for (;;) {
                    var key = eat();
                    if (key.t !== 'id' && key.t !== 'str') throw new SyntaxError('bad key');
                    eat(':');
                    props.push([String(key.v), expr()]);
                    if (peek(',')) { pos++; if (peek('}')) { pos++; break; } continue; }
                    eat('}');
                    break;
                }
                return { k: 'object', props: props };
            }
            throw new SyntaxError('unexpected ' + t.v);
        }
        function postfix() {
            var node = primary();
            for (;;) {
                if (peek('.') && tokens[pos].t === 'p') {
                    pos++;
                    var name = eat();
                    if (name.t !== 'id') throw new SyntaxError('bad property');
                    node = { k: 'get', obj: node, prop: { k: 'lit', v: name.v } };
                } else if (peek('[') && tokens[pos].t === 'p') {
                    pos++;
                    var prop = expr();
                    eat(']');
                    node = { k: 'get', obj: node, prop: prop };
                } else if (peek('(') && tokens[pos].t === 'p') {
                    pos++;
                    node = { k: 'call', callee: node, args: list(')') };
                } else {
                    return node;
                }
            }
        }
        function expr() {
            if (peek('!') && tokens[pos].t === 'p') { pos++; return { k: 'not', e: expr() }; }
            if (peek('-') && tokens[pos].t === 'p') { pos++; return { k: 'neg', e: expr() }; }
            return postfix();
        }
        var body = [];
        while (pos < tokens.length) {
            if (peek(';') && tokens[pos].t === 'p') { pos++; continue; }
            var ret = false;
            if (peek('return') && tokens[pos].t === 'id') { pos++; ret = true; }
            body.push({ ret: ret, e: expr() });
            if (pos < tokens.length) eat(';');
        }
        return body;
    }

    // ── Evaluator ──────────────────────────────────────────────────────────
    var UNSAFE_PROPS = { __proto__: 1, constructor: 1, prototype: 1 };

    function pageFunction(name) {
        if (BLOCKED_NAMES[name]) throw new Error('blocked: ' + name);
        var fn = window[name];
        if (typeof fn !== 'function') throw new Error('not a function: ' + name);
        if (/\[native code\]/.test(nativeToString.call(fn))) throw new Error('blocked native: ' + name);
        return fn;
    }

    function evaluate(node, ctx) {
        switch (node.k) {
        case 'lit': return node.v;
        case 'this': return ctx.self;
        case 'event': return ctx.event;
        case 'document': return document;
        case 'global': {
            var value = window[node.name];
            if (typeof value === 'function') pageFunction(node.name);
            return value;
        }
        case 'array': return node.items.map(function (n) { return evaluate(n, ctx); });
        case 'object': {
            var o = {};
            node.props.forEach(function (p) {
                if (!UNSAFE_PROPS[p[0]]) o[p[0]] = evaluate(p[1], ctx);
            });
            return o;
        }
        case 'not': return !evaluate(node.e, ctx);
        case 'neg': return -evaluate(node.e, ctx);
        case 'get': {
            var obj = evaluate(node.obj, ctx);
            var prop = String(evaluate(node.prop, ctx));
            if (UNSAFE_PROPS[prop]) throw new Error('blocked property');
            return obj == null ? undefined : obj[prop];
        }
        case 'call': return call(node, ctx);
        }
        throw new Error('bad node');
    }

    function call(node, ctx) {
        var args = node.args.map(function (n) { return evaluate(n, ctx); });
        var callee = node.callee;
        if (callee.k === 'global') return pageFunction(callee.name).apply(window, args);
        if (callee.k === 'get' && callee.prop.k === 'lit') {
            var method = callee.prop.v;
            if (callee.obj.k === 'event' && (method === 'stopPropagation' || method === 'preventDefault')) {
                return ctx.event[method]();
            }
            if (callee.obj.k === 'document' && method === 'getElementById') {
                return document.getElementById(String(args[0]));
            }
        }
        throw new Error('call not allowed');
    }

    function run(src, self, event) {
        var program = cache.get(src);
        if (!program) {
            program = parse(src);
            cache.set(src, program);
        }
        var ctx = { self: self, event: event };
        for (var i = 0; i < program.length; i++) {
            var result = evaluate(program[i].e, ctx);
            if (program[i].ret) {
                if (result === false) event.preventDefault();
                return result;
            }
        }
        return undefined;
    }

    // ── Binding ────────────────────────────────────────────────────────────
    function listenerFor(type) {
        return function (event) {
            var src = this.getAttribute('data-on' + type);
            if (!src || !src.trim()) return;
            try {
                run(src, this, event);
            } catch (err) {
                if (window.console) console.error('[eve-actions] ' + type + ': ' + src, err);
            }
        };
    }
    var listeners = {};
    EVENTS.forEach(function (type) { listeners[type] = listenerFor(type); });

    function bind(el, type) {
        var types = bound.get(el);
        if (!types) { types = {}; bound.set(el, types); }
        if (types[type]) return;
        types[type] = true;
        el.addEventListener(type, listeners[type]);
    }

    EVENTS.forEach(function (type) {
        var attr = 'data-on' + type;
        document.addEventListener(type, function (event) {
            var path = event.composedPath ? event.composedPath() : [];
            for (var i = 0; i < path.length; i++) {
                var el = path[i];
                if (el && el.nodeType === 1 && el.hasAttribute(attr)) bind(el, type);
            }
        }, true);
    });

    // Exposed for tests and for code that needs to run a handler string directly.
    window.EveActions = { parse: parse, run: run };
})();

// Small helpers for handlers that used to assign or toggle state inline.
function trimInputValue(el) {
    el.value = el.value.trim();
}

function hideElementById(id) {
    var el = document.getElementById(id);
    if (el) el.classList.add('hidden');
}

function hideElementDisplay(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
}

function removeElementById(id) {
    var el = document.getElementById(id);
    if (el) el.remove();
}

function clickElementById(id) {
    var el = document.getElementById(id);
    if (el) el.click();
}

function removeReadonly(el) {
    el.removeAttribute('readonly');
}

function openExternalUrl(url) {
    var href = String(url || '');
    if (/^https?:\/\//i.test(href)) window.open(href, '_blank', 'noopener');
}

// <img> fallback: hide the broken image and show the element right after it.
function imageFallbackToNext(img) {
    img.style.display = 'none';
    if (img.nextElementSibling) img.nextElementSibling.style.display = 'flex';
}

// Escape text for an HTML attribute value built in a template string.
function eveAttr(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
