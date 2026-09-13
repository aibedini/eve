# Static asset delivery

## Problem

`scripts/benchmark_frontend.py` (new in this phase) measures every page, the local
static assets it references and their cache headers. On the benchmark dataset the
dashboard pulls six assets totalling about 293 KB:

| asset | bytes | Cache-Control before |
|-------|-------|----------------------|
| `/static/fonts/fonts.css` | 1,645 | `no-cache` |
| `/static/style.css` | 137,235 | `no-cache` |
| `/static/jquery-3.6.0.min.js` | 89,503 | `no-cache` |
| `/static/persian-date.min.js` | 36,963 | `no-cache` |
| `/static/jalalidatepicker.min.css` | 7,980 | `no-cache` |
| `/static/jalalidatepicker.min.js` | 19,868 | `no-cache` |

Flask's default policy revalidates every one of them on every page load, so the
browser issues six conditional requests before it can reuse anything it already
has. A long `max-age` was not an option either: the URLs carried no version, so a
cached stylesheet would survive an upgrade and the operator would see a mixture of
old and new UI.

## Change

`app.py` now versions static URLs and caches them accordingly:

* `_static_asset_version(filename)` fingerprints a file as `mtime` + `size` in hex;
  the result is memoized per process with `lru_cache` and is `None` for a missing
  file.
* An `app.url_defaults` hook (`_versioned_static_url`) appends `?v=<fingerprint>`
  to every `url_for(positional static)` call. Templates are unchanged: they keep
  calling `url_for` and get a cache-busting URL. An explicit `v` is never
  overwritten.
* `add_static_cache_headers` (an `after_request` hook, only for the `static`
  endpoint) sets the policy:
  * versioned css/js/mjs/svg/json/webmanifest/woff/ttf/otf/eot/png/jpg/jpeg/gif/
    webp/ico/avif -> `public, max-age=31536000, immutable`;
  * an unversioned font or image (the relative URLs inside `fonts.css`) ->
    `public, max-age=604800`;
  * an unversioned stylesheet or script -> Flask's revalidation policy, so an
    upgrade is picked up on the next page load.
* Two knobs: `EVE_STATIC_IMMUTABLE_SECONDS` (default 31536000) and
  `EVE_STATIC_LONG_LIVED_SECONDS` (default 604800).

The authenticated `private, no-store` policy already excluded `/static/`, so no
interaction was needed there.

## Result

Measured with `scripts/benchmark_frontend.py` before (`frontend-baseline.json`,
commit `39b7729`) and after (`frontend-after.json`):

| metric | before | after |
|--------|--------|-------|
| dashboard assets with `immutable` | 0 / 6 | 6 / 6 |
| dashboard assets with a version | 0 / 6 | 6 / 6 |
| dashboard HTML bytes | 501,553 | 503,976 (about 2.4 KB of `?v=`) |
| asset bytes | 293,194 | 293,194 (unchanged) |
| `/api/refresh` (8.2 MB JSON) | gzip potential 770 KB | unchanged |

The transfer total is unchanged on a cold load (the version is 13 bytes per URL),
but after the first visit the browser stops revalidating about 293 KB of assets:
with the old policy every page load made six conditional requests, now zero until a
file changes.

The extractor in the measurement tool was updated in the same commit to keep the
`?v=` part of a URL, because that is what a browser requests; the baseline artifact
was produced before the templates emitted versions, so it shows the bare URLs that
were served then.

## Residual risk

* Fonts referenced from inside `fonts.css` cannot carry the version (the CSS is
  static); they get a 7-day `max-age` instead. A font file change reaches clients
  within a week.
* The fingerprint is memoized per process, so a file replaced while the process
  keeps running keeps its old version until restart. Deployments restart the app
  when assets change; uploaded app files use per-file names.
* A CDN may cache a versioned URL for a year. That is the intent; the URL changes
  with the file, so no purge is required.

## Verification

`tests/test_static_caching.py` (11 tests): the version is appended and stable, a
missing file has none, an explicit `v` is respected, the env parser tolerates
garbage, versioned assets are immutable, unversioned CSS revalidates, unversioned
fonts get the week, 404s do not get the immutable policy, and the rendered
dashboard actually contains versioned asset URLs.

## Intermittent layout corruption: the version is not an identity (investigation)

A subscription page that renders correctly most of the time and with collapsed columns and
oversized SVG icons the rest of the time points at the stylesheets it loads
(`tailwind.generated.css`, `style.css`, `fonts.css`, `phosphor-regular.css`): a bare `<svg>`
with no utility CSS takes its intrinsic size, and grid/flex utilities that never arrive leave
the columns stacked. The page's own `.pkg-*` rules are inline in the HTML, so they cannot skew;
the *static* stylesheets can.

The dynamic HTML is already safe: `/s/<server>/<token>` answers
`Cache-Control: no-store, no-cache, must-revalidate, max-age=0`. The weak half is the asset
identity, and `tests/test_static_version_identity.py` proves it with executable evidence:

1. **The fingerprint is `mtime+size`, not content.** The same bytes get a different `?v=` as
   soon as the mtime moves (every deploy, and every node independently), and two *different*
   stylesheets written with the same size and the same mtime share one `?v=`. The key therefore
   does not identify the bytes: a node can advertise a key that means something else on another
   node, a CDN can hold different content under the same-looking key, and a rollback can
   reintroduce an old file under a key a browser has already cached with newer content.
2. **The served bytes are not bound to the requested version.** Flask's static route ignores the
   query string, and the response hook marks *any* `?v=` on a versionable suffix as
   `public, max-age=31536000, immutable` - even `?v=whatever`. A request for a stale version is
   answered with the *current* bytes and pinned in every cache on the path for a year. That is
   how one browser (or CDN edge, or tab) pairs an old stylesheet with a new page while the next
   load pairs the new one.

The cascade is not the cause: with a fixed pair of stylesheets the rendering is deterministic,
which is why an "it changes between loads" symptom is attributed to *which* stylesheet arrived.
Scoping the subscription UI is still worth doing as defence in depth (any global rule change in
`style.css` can reach it) and is tracked separately.

### Remediation (in progress)

1. **Done** - the version is now the truncated sha256 of the file's bytes (directories such as
   `flags/4x3/` hash their children), so every node computes the same key for the same content
   and a redeploy of identical bytes no longer invalidates every cache.
2. **Done** - `immutable` is granted only when the requested `?v=` equals the file's current
   content hash; a stale or unknown version is served with `no-cache, must-revalidate`
   (plus `X-Eve-Stale-Asset-Version: 1`) so no browser or CDN can pin new bytes under an old key.
3. **Done** - every response carries `X-Eve-Build` / `X-Eve-Build-Source` and HTML repeats it as
   `<meta name="eve-build">`; `docs/operations/BUILD_IDENTITY.md` is the deploy contract
   (stamp one `EVE_BUILD_SHA` per release, verify all nodes agree, deploy assets and templates
   together).
4. **Done** - the dynamic `/s/*` HTML is `private, no-store` and must stay out of shared caches.
5. Scoping the subscription UI under `.subscription-page`, with an inventory test that refuses
   new global `svg`/`.icon`/`.card`/grid/flex rules.
6. Visual regression at 360/390/640/768/1024/1280/1440 (icon size, overflow, column stability)
   and a contract test that rendered HTML references only assets of its own build.

### Residual risk after the fix

* A grouped directory key is memoized on its children's (name, size, mtime): a child rewritten
  with an identical size *and* mtime would keep the old group key. Individual asset URLs use
  exact content hashes, so this cannot affect a stylesheet or script reference.
* A CDN may still cache a *matching* versioned URL for a year - that is the intent, and the key
  changes with the content.
* Static files must be served by the same build that rendered the HTML; the identity header and
  the deployment check in `docs/operations/BUILD_IDENTITY.md` are what make a mismatch visible.
