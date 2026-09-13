# Build identity and verifiable deploys

## Why

An intermittent rendering bug is the hardest kind to fix: the same URL renders correctly on one
load and corruptedly on the next. Almost always the bytes were not the same bytes - HTML from
one build and a stylesheet from another. Eve now makes that queryable instead of guessable
(see `docs/performance/STATIC_ASSETS.md` for the investigation that led here).

## What a response says

Every response carries:

| Header | Meaning |
|--------|---------|
| `X-Eve-Build` | the build that answered: `EVE_BUILD_SHA`, else the checkout's git revision, else `APP_VERSION` |
| `X-Eve-Build-Source` | `env` / `git` / `app_version` - how the value was derived |

HTML also carries `<meta name="eve-build" content="...">`, so a page's own build is visible in
DevTools → Elements even when the response headers are gone (saved pages, screenshots).

## What the deployment must do

1. **Stamp one value for the whole release.** `EVE_BUILD_SHA` is the authoritative source; set
   it from the same commit that produced the bundle:

   ```bash
   # docker-compose / systemd / CI
   export EVE_BUILD_SHA="$(git rev-parse --short=12 HEAD)"
   docker compose up -d --build            # or: systemctl restart eve
   ```

   Never let it default per node: an unset variable falls back to each checkout, which is exactly
   the per-node disagreement this exists to prevent.

2. **Verify all nodes agree.** After a deploy, ask every node and compare:

   ```bash
   for host in eve-1 eve-2 eve-3; do
     printf '%s ' "$host"; curl -sI "https://$host/login" | grep -i '^x-eve-build:'
   done
   ```

   One command should print the same value for every node. If it does not, the rollout is
   partial: finish it (or roll back) before diagnosing anything else.

3. **Deploy assets and templates together.** A stylesheet and the HTML that references it must
   come from the same build. Eve's versioned asset URLs (`?v=<content hash>`) make a *browser*
   safe against a mixed cache, but they cannot protect against a node serving a different build
   than the node that rendered the page - only an atomic (or at least finished-before-traffic)
   rollout can.

4. **Do not shared-cache the tokenized pages.** `/s/<server>/<token>` answers
   `Cache-Control: private, no-store`; it must not be cached at an edge. Static assets are the
   only thing that should be cached, and they are content-versioned.

## Diagnosing "it renders wrong sometimes"

1. DevTools → Network → **Disable cache**, then Ctrl+Shift+R. If the page is instantly correct,
   the browser cache was holding a mismatched pair.
2. Check `X-Eve-Build` on the document response and on each stylesheet response (Network →
   Headers). **A document and its stylesheets must report the same build.** Different values mean
   a partial rollout or a load balancer spreading one page across builds.
3. Check the cache keys: a stylesheet URL whose `?v=` does not match the file that answered it is
   a version-skew symptom (Eve now serves `immutable` only when the requested version equals the
   file's current content hash, so a stale key revalidates instead of being pinned for a year).
4. Zoom 100%, then compare `window.innerWidth` and `devicePixelRatio` between a correct and a
   broken load. If they match and the layout still differs, it is not a breakpoint.
5. Inspect one oversized icon: Computed → `width`/`height`/`display` names the selector and the
   stylesheet that sized it. If the computed size comes from default SVG sizing (no rule), the
   utility stylesheet did not load - back to step 2.

## Related

* `docs/performance/STATIC_ASSETS.md` - asset versioning, cache policy and the skew investigation
* `docs/operations/WORKERS.md` - which processes must run per node
* `tests/test_build_identity.py` - the header/meta contract
* `tests/test_static_version_identity.py` - the evidence for the version-identity weakness
