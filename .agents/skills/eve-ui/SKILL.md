---
name: eve-ui
description: Eve panel UI/theme guide: the single stylesheet, design tokens, dark/light theming, the component classes (buttons, form controls, checkboxes, toggles, badges, modals, cards, tables) and the CSP/RTL constraints. Load before touching any template or static CSS.
whenToUse: Any change to templates/*.html, static/style.css, static/tailwind.generated.css, or UI behaviour in Eve. Also load when reviewing a UI diff.
---

# Eve UI and theme

Read this before editing any template or CSS. Eve has one hand-written stylesheet and
a strict component vocabulary; inventing markup (for example a raw checkbox inside a
form group) reads as unskilled and breaks the theme.

See also `docs/UI_DESIGN_SYSTEM.md` for the full, human-readable design system.

## 1. Where the UI lives

| Path | Role |
|------|------|
| `templates/base.html` | shell: `<head>` incl. the theme boot script, sidebar, topbar, `.content-area`, jQuery, `csp_nonce` |
| `templates/<page>.html` | one page each; `{% block content %}`, `{% block scripts %}` |
| `static/style.css` | the design system: every component, ~5.7k lines, sections grouped by page prefix (`monitor-`, `server-`, `admin-`, `wallet-`, ...) |
| `static/tailwind.generated.css` | Tailwind output used only by `subscription.html` |
| `static/fonts/fonts.css` | self-hosted Inter + Vazirmatn |
| `static/*.js` | vendored third-party (jquery, quill, chart, jalalidatepicker, persian-date) |

Page libraries are loaded per page, never globally (Quill only for the announcement
editor, Chart only for Pulse/BNQO charts, the Jalali pickers only where a date field
exists).

## 2. Theme tokens (the only colours you may use)

`:root` in `static/style.css` defines `--primary`, `--primary-dark`, `--secondary`,
`--success`, `--warning`, `--danger`, `--bg-dark`, `--bg-card`, `--bg-card-hover`,
`--text-primary`, `--text-secondary`, `--border-color`, `--gradient-1`, `--shadow`,
`--shadow-lg`, `--sidebar-width` and `--header-height`.

Light mode is `html[data-theme="light"]`, which re-defines the same tokens; the boot
script in `base.html` sets `data-theme` from `localStorage.eve_theme` before paint.
Therefore:

* never hardcode a hex colour or `#fff` text in new markup; use a token or an existing
  class. The few light-theme overrides live in the `html[data-theme="light"]` block near
  the top of the stylesheet;
* never use inline `style="color:..."`/`font-size:` for text; use a helper class
  (`.field-note`, `.field-note-ok`, `.field-note-warn`, `.label-note`) or add one;
* show/hide with the `.hidden` class, not `style.display`.

## 3. Component vocabulary (canonical markup)

Buttons: `.btn` plus `.btn-primary`, `.btn-secondary`, `.btn-success`, `.btn-danger`,
`.btn-outline`; sizes `.btn-icon`, `.btn-block`.

Form fields:

```html
<div class="form-group">
    <label>Label</label>
    <input type="text" id="x" placeholder="...">
    <small class="field-note field-note-ok hidden">Helper text</small>
</div>
<select class="form-select">...</select>
```

Checkbox (this is the project checkbox; a bare `input[type=checkbox]` inside a
`.form-group` gets `width:100%` + a border from the `.form-group input` rule and looks
broken):

```html
<label class="checkbox-label">
    <input type="checkbox" id="x">
    <span class="checkmark"></span>
    <span>Option text</span>
</label>
```

Toggle switch (for on/off settings):

```html
<label class="form-group form-toggle-item" style="cursor: pointer;">
    <div class="toggle-switch"><input type="checkbox" id="x"><span class="slider"></span></div>
    <span>Enabled</span>
</label>
```

Badges: base `.badge`; inside a server card `.server-badges .badge.active` (green),
`.inactive` (red), `.panel-type` (blue, uppercase) and `.warning` (amber, informational).

Modals: `.modal-overlay.hidden` wrapper, `.modal` / `.modal-sm` / `.modal-lg` /
`.modal-xl`, `.modal-header` (`.modal-close`), `.modal-body`, `.form-actions`.

Cards and tables: `.stat-card`, `.server-card`, `.package-card`, `.inbound-card`;
`.monitor-table`, `.clients-table`, `.filter-item`, `.search-input`.

Text helpers: `.field-note` (block hint under a control), `.field-note-ok`
(`--success`), `.field-note-warn` (`--warning`), `.field-note-strong`, `.label-note`
(inline note inside a label). Utilities: `.hidden`; secondary text always uses
`var(--text-secondary)` rather than a hardcoded grey.

## 4. Hard rules

1. Search before you write: `grep -n "^\.<area>-" static/style.css` to see the components
   that already exist for the page you are changing. Reuse or extend; do not invent a
   parallel style.
2. A `<label>` inside `.form-group` receives label typography unless it carries
   `.checkbox-label`; do not put interactive controls inside a bare `.form-group label`.
3. Use the spacing/radius scale already in use (gaps 6/8/10/12/16/20 px, radii
   6/8/10/12/16 px, 9999 px pills) and the font sizes 0.65/0.7/0.78/0.8/0.875/1rem.
4. Keep the CSP happy: no external CDN links, every inline `<script>` needs
   `nonce="{{ csp_nonce }}"`, and assets come from `/static/`.
5. The UI is bilingual and RTL-capable; do not hardcode left/right where the existing
   flex/gap layout already handles direction, and keep new strings in the language of
   the surrounding page (`t.*` dictionaries for pages that have one).
6. Announcements/data tables that render from JS use `document.createElement` plus the
   `createBadge(text, className)` helper in the page script, not string HTML.
7. `static/style.css` must stay valid UTF-8 with no NUL bytes. It was once partly
   UTF-16LE (about 28 KB of monitor rules silently stopped applying because a shell
   append wrote UTF-16); never append with `>>` / `Add-Content`, edit the file in place.
   Recovering that block by decoding the bytes with the wrong byte order is just as
   bad as leaving it: the file stays valid UTF-8 with no NUL bytes while every
   recovered line ends in a stray non-ASCII character (U+0A0D/U+0D00), which attaches
   itself to the property name of the next declaration and silently kills the rule.
   Repair the encoding from the git blob (strip the NUL bytes of the UTF-16 run, or
   decode the run with the correct byte order) and check the result, never eyeball it.
   `tests/test_ui_design_system.py` fails if the encoding regresses, if a non-ASCII
   character outside the allowlist appears, if braces stop balancing, or if this skill
   drifts from the stylesheet.
8. After a UI change run the page tests plus `tests/test_ui_design_system.py`.

## 5. Server page specifics

`templates/servers.html` renders the grid from `/api/servers` and edits a server in the
`#server-modal` modal. Boolean server options use the checkbox component; the
`Enabled` row uses the toggle. Password and API-token fields are never prefilled: an
empty value keeps the stored secret, and removing the token is an explicit action.
