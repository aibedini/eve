/*
 * Country flags for panel-provided names.
 *
 * Inbound remarks and server names arrive from the X-UI panels with regional
 * indicator pairs ("🇩🇪", "🇺🇸", ...). Those are emoji, and Windows has no flag
 * glyphs for them: it draws the two letters instead ("DE", "US"), which reads as
 * if Eve were printing a country code rather than a flag. This module replaces
 * them with the self-hosted SVG set in static/flags/4x3/, so the same markup
 * shows a real flag on every platform and never falls back to the letters.
 *
 * Usage:
 *   EveFlags.html(name)      -> escaped HTML with <span class="country-flag"> badges
 *   EveFlags.flagify(root)   -> rewrite the flag emoji inside an existing subtree
 *   EveFlags.codeFromEmoji() -> "de" for "🇩🇪", '' otherwise
 *
 * Auto-run: the module flagifies the document on load and keeps up with content
 * rendered later through a MutationObserver, so a page only has to load it.
 * <select>/<option> cannot hold an image and are left untouched; elements marked
 * with class="no-flags" (or data-no-flags) are skipped as well.
 */
(function (global) {
    'use strict';

    var PAIR = /[\u{1F1E6}-\u{1F1FF}]{2}/gu;      // global: used for replacements
    var PAIR_TEST = /[\u{1F1E6}-\u{1F1FF}]{2}/u;  // stateless: used for tests
    var A = 0x1F1E6;
    var SKIP_TAGS = {
        SCRIPT: 1, STYLE: 1, TEXTAREA: 1, INPUT: 1, SELECT: 1, OPTION: 1,
        CODE: 1, PRE: 1, KBD: 1, SAMP: 1, SVG: 1, MATH: 1,
    };
    var ATTRS = ['title', 'aria-label', 'data-label'];

    var current = document.currentScript;
    var rawBase = (current && current.dataset && current.dataset.flagBase) || '/static/flags/4x3/';
    // Flask stamps static URLs with "?v=<hash>", so the query has to survive and
    // land after the file name: "/static/flags/4x3/de.svg?v=..." -- concatenating
    // the code onto the raw base would request "/static/flags/4x3/?v=...de.svg",
    // which 404s and used to leave the emoji (the letters on Windows) on screen.
    var queryAt = rawBase.indexOf('?');
    var query = queryAt >= 0 ? rawBase.slice(queryAt) : '';
    var dir = queryAt >= 0 ? rawBase.slice(0, queryAt) : rawBase;
    if (dir.charAt(dir.length - 1) !== '/') dir += '/';

    function flagUrl(code) {
        return dir + code + '.svg' + query;
    }

    function esc(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    /** "🇩🇪" -> "de"; '' when the string is not exactly one regional pair. */
    function codeFromEmoji(flag) {
        var text = String(flag == null ? '' : flag);
        var points = Array.from(text, function (ch) { return ch.codePointAt(0); });
        if (points.length !== 2) return '';
        for (var i = 0; i < 2; i += 1) {
            if (points[i] < A || points[i] > A + 25) return '';
        }
        return points.map(function (cp) { return String.fromCharCode(97 + cp - A); }).join('');
    }

    function skipped(element) {
        if (!element || element.nodeType !== 1) return false;
        if (SKIP_TAGS[element.tagName]) return true;
        if (element.classList && element.classList.contains('no-flags')) return true;
        return element.hasAttribute && element.hasAttribute('data-no-flags');
    }

    /** Build the flag badge; the image is always the source of truth. */
    function badge(code) {
        var span = document.createElement('span');
        span.className = 'country-flag';
        var img = document.createElement('img');
        img.src = flagUrl(code);
        img.alt = code.toUpperCase();
        img.decoding = 'async';
        img.loading = 'lazy';
        img.addEventListener('error', function () {
            // A missing asset must not degrade into the letters: hide the badge and
            // leave the name text as the only thing on screen.
            span.classList.add('country-flag-missing');
        }, { once: true });
        span.appendChild(img);
        return span;
    }

    function flagifyTextNode(node) {
        var text = node.nodeValue;
        if (!text || !PAIR_TEST.test(text)) return;
        var parent = node.parentNode;
        if (!parent || skipped(parent) || (parent.classList && parent.classList.contains('country-flag'))) return;

        var fragment = document.createDocumentFragment();
        var last = 0;
        var match;
        PAIR.lastIndex = 0;
        while ((match = PAIR.exec(text)) !== null) {
            var code = codeFromEmoji(match[0]);
            if (!code) continue;
            if (match.index > last) {
                fragment.appendChild(document.createTextNode(text.slice(last, match.index)));
            }
            fragment.appendChild(badge(code));
            last = match.index + match[0].length;
        }
        if (!last) return;
        if (last < text.length) fragment.appendChild(document.createTextNode(text.slice(last)));
        parent.replaceChild(fragment, node);
    }

    function stripAttributeFlags(element) {
        for (var i = 0; i < ATTRS.length; i += 1) {
            var name = ATTRS[i];
            var value = element.getAttribute(name);
            if (!value || !PAIR_TEST.test(value)) continue;
            element.setAttribute(name, value.replace(PAIR, '').replace(/\s{2,}/g, ' ').trim());
        }
    }

    /** Rewrite the flag emoji inside a subtree (idempotent). */
    function flagify(root) {
        if (!root || !root.nodeType) return;
        if (root.nodeType === 3) {
            flagifyTextNode(root);
            return;
        }
        if (root.nodeType !== 1 || skipped(root)) return;
        // Hover text and mobile table labels must not show the letters either.
        stripAttributeFlags(root);
        if (root.querySelectorAll) {
            Array.prototype.forEach.call(
                root.querySelectorAll('[title], [aria-label], [data-label]'),
                stripAttributeFlags);
        }
        if (!global.document.createTreeWalker) return;
        var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
            acceptNode: function (node) {
                if (!PAIR_TEST.test(node.nodeValue || '')) return NodeFilter.FILTER_REJECT;
                if (skipped(node.parentNode)) return NodeFilter.FILTER_REJECT;
                return NodeFilter.FILTER_ACCEPT;
            },
        });
        var nodes = [];
        while (walker.nextNode()) nodes.push(walker.currentNode);
        nodes.forEach(flagifyTextNode);
    }

    /** Escaped HTML for a name, with flag badges instead of the emoji pairs. */
    function html(value) {
        return esc(value).replace(PAIR, function (flag) {
            var code = codeFromEmoji(flag);
            if (!code) return flag;
            return '<span class="country-flag"><img src="' + flagUrl(code) + '" alt="' +
                code.toUpperCase() + '" loading="lazy" decoding="async"></span>';
        });
    }

    var queue = [];
    var scheduled = false;

    function flush() {
        scheduled = false;
        var batch = queue;
        queue = [];
        batch.forEach(function (node) {
            try {
                flagify(node);
            } catch (error) {
                /* never let one node break the page */
            }
        });
    }

    function schedule(nodes) {
        if (!nodes.length) return;
        queue = queue.concat(nodes);
        if (scheduled) return;
        scheduled = true;
        if (global.requestAnimationFrame) global.requestAnimationFrame(flush);
        else global.setTimeout(flush, 16);
    }

    function start() {
        flagify(global.document.body);
        if (!global.MutationObserver) return;
        new MutationObserver(function (records) {
            var added = [];
            records.forEach(function (record) {
                Array.prototype.forEach.call(record.addedNodes, function (node) {
                    if (node.nodeType === 1 || node.nodeType === 3) added.push(node);
                });
            });
            schedule(added);
        }).observe(global.document.body, { childList: true, subtree: true });
    }

    if (global.document.readyState === 'loading') {
        global.document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }

    global.EveFlags = {
        dir: dir,
        query: query,
        flagUrl: flagUrl,
        codeFromEmoji: codeFromEmoji,
        html: html,
        flagify: flagify,
    };
}(window));
