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
