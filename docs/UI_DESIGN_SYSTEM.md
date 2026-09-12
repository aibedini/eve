# Eve UI design system

This is the official, human-readable design system for the Eve control panel. It is
the reference that the agent skill in `.agents/skills/eve-ui/SKILL.md` (and its DSH
mirror) summarises, and it describes what is actually implemented in the code - not an
aspirational redesign.

## Design Identity

Eve has one coherent visual language, used consistently across every page:

* **Product surface:** *Eve Control Surface* - a dense, dark-first operator console for
  managing X-UI panels, clients, finance, messaging and the BNQO observability plane.
* **Visual language:** *Eve Dark Operations UI* - a Slate + Indigo palette, restrained
  glassmorphism, dense but readable spacing, and mobile-first responsive layouts.

These two names are descriptive labels for the system that already exists. The repo
does not contain a record of the original design system's own name, and none is
claimed or invented here; the implementation is the authority.

## Source of Truth

| Path | Role |
|------|------|
| [static/style.css](../static/style.css) | The design system. One hand-written stylesheet with every token, layout and component (about 730 rules). |
| [templates/base.html](../templates/base.html) | The shell: `<head>`, theme boot script, sidebar, top bar, content area, jQuery, CSP nonce. |
| [templates/](../templates/) | One template per page; each renders `{% block content %}` inside the base shell. |
| `static/fonts/fonts.css` | Self-hosted Inter and Vazirmatn. |
| `static/tailwind.generated.css` | Tailwind output loaded only by the subscription page. |

There is exactly one visual system. New work extends the stylesheet and the existing
components; it never introduces a parallel CSS framework, an external CDN, or an
inline style that bypasses the tokens.

## Core Colors

The only colours a change may use are the tokens defined in `:root`. The palette is
Slate for surfaces and Indigo for interaction, with green/amber/red reserved for
state:

| Token | Value | Meaning |
|-------|-------|---------|
| `--primary` | `#6366f1` | Indigo: primary actions, focus, active state |
| `--primary-dark` | `#4f46e5` | Pressed / hovered primary |
| `--secondary` | `#64748b` | Slate: secondary text and neutral controls |
| `--success` | `#22c55e` | Healthy, enabled, connected |
| `--warning` | `#f59e0b` | Caution, degraded, insecure opt-in |
| `--danger` | `#ef4444` | Error, disabled, destructive |
| `--bg-dark` | `#0f172a` | Page background (dark theme) |
| `--bg-card` | `#1e293b` | Card and panel surface |
| `--bg-card-hover` | `#334155` | Hover surface |
| `--text-primary` | `#f8fafc` | Primary text |
| `--text-secondary` | `#94a3b8` | Secondary / muted text |
| `--border-color` | `#334155` | Default border |
| `--gradient-1` | `linear-gradient(135deg, #667eea 0%, #764ba2 100%)` | Brand gradient |
| `--shadow` / `--shadow-lg` | layered rgba blacks | Elevation |
| `--sidebar-width` | `260px` | Layout constant |
| `--header-height` | `70px` | Layout constant |

Never hardcode a hex value or a named colour in new markup or CSS; if a new colour
is genuinely needed it becomes a token in `:root` and (when it must adapt) an override
in the light-theme block.

## Theme

* Dark is the default and the design target. `html` carries `data-theme` and defaults
  to `dark`; `html { color-scheme: dark; }` fixes native control rendering.
* Light mode is `html[data-theme="light"]`, which re-declares the same tokens near the
  top of the stylesheet. Components written against tokens switch automatically; a
  component that hardcodes a dark colour breaks light mode.
* The boot script in [base.html](../templates/base.html) reads `localStorage.eve_theme`
  and sets `data-theme` before first paint, so there is no theme flash. A few
  light-theme overrides exist for native form controls and a handful of surfaces.
* Theme changes are applied through `data-theme` only; never rewrite rules per theme.

## Typography

* Body stack: `Inter, Vazirmatn, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif`, `line-height: 1.6`.
  Inter is the Latin face, Vazirmatn the Persian face; both are self-hosted.
* Scale in active use: `0.65rem`, `0.7rem`, `0.78rem`, `0.8rem`, `0.875rem`, `1rem`, with
  `0.7rem` for meta text and badges and `1rem` for card titles.
* Weights: 400 body, 500 controls and badges, 600 titles and emphasis.
* Persian pages rely on `Vazirmatn, sans-serif` and the same scale; do not hand-set a
  different size for a translated string.
* Text colour always comes from `--text-primary` or `--text-secondary`.

## Surfaces

* Page: `--bg-dark`. Panels and cards: `--bg-card` with `1px solid var(--border-color)`.
* Radii in use: `6px`, `8px`, `10px`, `12px`, `16px`, and `9999px` for pills.
* Elevation: `--shadow` for resting cards, `--shadow-lg` for hover and modals.
* Separation is by border and surface, not by heavy shadows or outlines.

## Glassmorphism

Glassmorphism is used sparingly and only where content scrolls underneath a fixed
layer: the modal overlay and the sticky top bar use `backdrop-filter: blur(8px)` (and
A smaller `blur(4px)` where the layer is thinner), over a translucent surface.

* Never apply blur to ordinary cards or to text-heavy panels.
* `backdrop-filter` is the only sanctioned blur; there is no second effect layer.
* Always keep a solid fallback background so text stays readable if blur is unsupported.

## Layout

* Shell: a fixed sidebar (`.sidebar`) of `--sidebar-width`, a top bar of
  `--header-height`, and the scrolling `.main-content` / `.content-area`.
* Content is a vertical flow of `.stat-card` / `.server-card` grids and tables, with
  gaps on the 6/8/10/12/16/20 px scale.
* Responsive breakpoints already in the stylesheet: 1500, 1200, 1100, 1024, 900, 768,
  640, 600, 520, 480, 444 px (max-width queries). Reuse one of these; do not invent a
  new breakpoint for a small tweak.
* The sidebar collapses to an off-canvas drawer on narrow screens; the template toggles
  `.open` / `.show` classes on the sidebar and overlay.

## Cards

* `--bg-card` surface, `--border-color` border, 12px radius, 0.2s border/transform
  transition on hover.
* Canonical cards: `.stat-card`, `.server-card`, `.package-card`, `.inbound-card`, and
  the compact `.monitor-compact-row`.
* A card header is a flex row with `justify-content: space-between` and a bottom border;
  the body is a flex column with gap-based spacing.
* Hover raises the border or the elevation slightly; never change the layout on hover.

## Status Semantics

State colour is fixed, so an operator can read a screen without reading every label:

* green (`--success`) = active, healthy, connected;
* red (`--danger`) = disabled, expired, failed, destructive;
* amber (`--warning`) = caution, degraded, temporary, or an explicit insecure opt-in;
* indigo (`--primary`) = informational and interactive;
* grey (`--secondary` / `--text-secondary`) = neutral and unknown.

Badges use the base `.badge` plus a variant. Inside a server card the variants are
`.server-badges .badge.active` (green), `.badge.inactive` (red), `.badge.panel-type`
(blue, uppercase label) and `.badge.warning` (amber, informational - for example a
server allowed to use plaintext HTTP or an unverified certificate). Amber is never a
stand-in for the red Disabled state.

## Buttons

* Base `.btn` plus exactly one intent: `.btn-primary`, `.btn-secondary`,
  `.btn-success`, `.btn-danger`, `.btn-outline`.
* Sizes and modifiers: `.btn-icon` for a compact button whose content includes an
  inline icon, `.btn-block` for a full-width action.
* A destructive action uses `.btn-danger`; a cancel action uses `.btn-secondary` or
  `.btn-outline`. Labels stay short and verb-first.

## Icons

* Icons are inline `<svg>` (font icons and icon fonts are not used), sized by
  `.btn-icon svg` and `.action-icon svg`, so they inherit the surrounding text colour.
* Outline icons are stroke-only: the project ships them with `fill="none"` and
  `stroke="currentColor"`, and `.eve-icon-outline` (which also covers `.action-btn svg`)
  pins `fill: none; stroke: currentColor`. A stylesheet rule that sets
  `fill: currentColor` beats the SVG's own attribute, so the icon renders as a solid
  blob — that regression already happened once when a legacy monitor rule
  (`width: 48px; ... fill: currentColor`) was left written as a bare `.action-btn`
  further down the file, where it re-sized and re-filled every action button in the
  panel. Page-specific button styles must be scoped to that page's container.
* Icon-only controls keep a text label or tooltip for accessibility.
* Country flags in panel-provided names are the `.country-flag` badge over the
  self-hosted `static/flags/4x3/` SVG set, rendered by `EveFlags.html()` or by
  `static/name-flags.js` (loaded by `base.html` and by the standalone subscription
  page). The regional-indicator emoji is never the rendered flag: Windows draws it as
  the two letters, which reads as a country code rather than a flag, so a missing asset
  hides the badge instead of falling back to those letters.
* Unicode glyphs appear only where the stylesheet already uses them (for example the
  disclosure triangle and the box-drawing comment separators).

## Forms

* Wrap each control in `.form-group` with a `<label>`; text and number inputs use the
  bare element or `.form-input`, selects use `.form-select`, search boxes use
  `.search-input`.
* Helper text uses `.field-note`, with `.field-note-ok` / `.field-note-warn` /
  `.field-note-strong` for tone; inline notes inside a label use `.label-note`. Do not
  write `style="font-size:...` or `style="color:...` in a template.
* Checkbox - use the project component, because a bare `input[type=checkbox]` inside a
  `.form-group` inherits the full-width bordered input rule and looks broken:

```html
<label class="checkbox-label">
    <input type="checkbox" id="x">
    <span class="checkmark"></span>
    <span>Option text</span>
</label>
```

* Toggle switch - for an on/off setting, use the slider component:

```html
<div class="toggle-switch">
    <input type="checkbox" id="x">
    <span class="slider"></span>
</div>
```

* Never prefill a stored secret. An empty password or token field keeps the stored
  value; removing a secret is always an explicit, separately confirmed action.

## Modals

* Structure: a `.modal-overlay` wrapper (hidden with the `.hidden` class), the `.modal`
  (or `.modal-sm` / `.modal-lg` / `.modal-xl`), a `.modal-header` with the title and a
  `.modal-close` button, a `.modal-body`, and a `.form-actions` footer.
* Show and hide with classes; never `style.display`.
* Keep the primary action on the right (leading edge in RTL) and the cancel next to it.

## Tables

* `.monitor-table` and `.clients-table` are the standard data tables; filters use
  `.filter-item` / `.filter-btn` and `.search-input`.
* On narrow screens, tables collapse into block cards (a label per cell) rather than
  scrolling horizontally; the responsive rules already exist, so reuse them.
* Numeric columns align consistently, and status columns use the badge vocabulary.

## Mobile First

* Design for the small screen first, then widen. Every new layout must be checked at
  480 px and 768 px as well as desktop.
* Prefer flex/grid with `gap` and `flex-wrap` so a layout reflows without a bespoke
  media query; add a max-width query only when content genuinely needs different
  structure.
* Tap targets stay comfortably sized; the 36-44 px range is already used throughout.

## RTL

* The document direction comes from `dir="{{ panel_dir }}"` on `<html>`; a page must
  work in both directions.
* Use logical, flow-relative layout (flex order, `gap`, `margin-inline`) instead of
  hardcoded `left`/`right`; when a physical side is unavoidable, guard it with the
  direction-aware selectors the stylesheet already uses.
* Persian strings are authored in the template's translation dictionary, not inline in
  a component that must stay language-neutral.

## Motion

* Transitions are short and functional: 0.2s for colour/border/shadow, 0.3s for
  transforms and larger movement, mostly `ease` or `cubic-bezier(0.4, 0, 0.2, 1)`.
* Keyframe animations are limited to feedback and loading: `skeleton-loading`,
  `monitor-spin`, `monitor-fade-in`, `monitor-rise`, `onlinePulseRing`, `slideDown`,
  `slideUp`, `spin`, `ann-preview-shimmer`, `qr-spin`.
* Animation must never be required to understand state, and it must not animate layout
  properties that force reflow.
* There is no `prefers-reduced-motion` guard yet; adding one is a welcome, isolated
  improvement.

## Performance

* One stylesheet, loaded once from `/static/`, with versioned, long-lived immutable
  caching (see [performance/STATIC_ASSETS.md](performance/STATIC_ASSETS.md)).
* No external fonts, CSS or JS from a CDN; everything is served from `/static/`, which
  also keeps the Content-Security-Policy strict (see [security/HEADERS.md](security/HEADERS.md)).
* Prefer CSS state (`.hidden`, `:hover`, `:focus-visible`, `[data-*]`) over JavaScript
  that rewrites inline styles on every render.
* Avoid selectors whose cost grows with list size, and avoid layout thrash in page
  scripts; live data arrives over SSE and re-renders through `document.createElement`.

## Accessibility

* Every form control has a `<label>` associated with it; icon-only buttons carry an
  accessible name.
* Interactive elements are reachable and visibly focused; the stylesheet provides a
  `:focus-visible` outline built from `color-mix` with an offset so it reads on both
  themes.
* Text contrast is guaranteed by using `--text-primary` / `--text-secondary` on token
  surfaces rather than ad-hoc greys.
* Colour is never the only signal: a status badge also carries a word.
* Modals move focus into the dialog and are dismissible with the close control and the
  keyboard.

## What to Avoid

* A hardcoded hex colour or `#fff` where a token exists.
* Inline `style="..."` for colour, size or visibility in a template.
* A bare `input[type=checkbox]` inside a `.form-group`, or any new control that ignores
  the checkbox/toggle components.
* `style.display` toggling instead of the `.hidden` class.
* A second CSS framework, a CDN asset, an icon font, or a page-local `<style>` block.
* Editing `static/style.css` with a shell append (`>>`, `Add-Content`): an earlier append
  wrote about 28 KB of it as UTF-16LE and those rules silently stopped applying. Edit it
  in place and keep it valid UTF-8 with no NUL bytes.
* Recovering a damaged block by decoding it the wrong way; a bad byte-order decode can
  look valid while poisoning every rule (see the structure guard tests).

## Enforcement

The contract is executable, not aspirational:

* `tests/test_ui_design_system.py` guards the stylesheet (valid UTF-8 with no NUL bytes,
  no stray non-ASCII left by a bad decode, balanced braces, the token and component
  vocabulary), the skill in both discovery roots, this document, and the UI rule in every
  agent instruction file.
* `scripts/ui_design_audit.py` counts the drift the templates still carry: inline
  `style="..."` attributes, hardcoded hex colours, inline `color:` / `font-size:`,
  `style.display` toggles, bare checkboxes and emoji. `tests/ui_design_baseline.json`
  records the count per template, and the guard test fails any file that goes above its
  recorded number. Run `python scripts/ui_design_audit.py --check` before committing a UI
  change; after a real cleanup rerun `python scripts/ui_design_audit.py
  --write-baseline` (the backlog may shrink, never grow).
* Every inline colour that spelled a token exactly has been migrated with
  `python scripts/ui_design_audit.py --fix-colors --apply`. That migration is
  property-aware: a surface token is never mapped onto a `color:`, and a permanently
  dark surface (a dialog with a white hairline border, a code block, the fixed navy
  announcement overlay) keeps its own colours, because the text tokens invert in the
  light theme and would make that text unreadable.
* The remaining backlog is measured rather than hidden. The tool prints it per template,
  which is what makes an incremental cleanup reviewable: pick a page, move its styles
  into the stylesheet section for that page, and lower its baseline.

## Change Workflow

1. Read the [eve-ui skill](../.agents/skills/eve-ui/SKILL.md) and this document.
2. Search the stylesheet for the components that already serve the page
   (`grep -n "^\\.<area>-" static/style.css`) and reuse or extend them.
3. Implement with tokens and the documented markup; add CSS in the existing section for
   the page rather than at the end of the file.
4. Run the page's tests plus [the guard tests](../tests/test_ui_design_system.py) and
   `python scripts/ui_design_audit.py --check`.
5. Bump `APP_VERSION` and describe the change honestly in the commit.

## Agent Rule

> For every user-facing UI change, read and follow `.agents/skills/eve-ui/SKILL.md`.
> `static/style.css` and `templates/base.html` remain the implementation source of truth.
> Do not introduce a parallel visual system.

The same rule is recorded in [AGENTS.md](../AGENTS.md) and the other agent instruction
files so it applies regardless of which assistant is editing the project.
