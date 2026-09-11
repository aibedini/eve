# Response security headers

## Baseline (every response)

| Header | Value | Why |
|--------|-------|-----|
| X-Content-Type-Options | nosniff | stop MIME sniffing of user-influenced responses |
| Referrer-Policy | same-origin | do not leak panel URLs to third parties |
| X-Frame-Options | SAMEORIGIN | clickjacking defence for older browsers |
| Cross-Origin-Opener-Policy | same-origin | isolate the browsing context from cross-origin openers |
| X-Permitted-Cross-Domain-Policies | none | no Flash/PDF cross-domain policy files |
| X-XSS-Protection | 0 | the legacy auditor is superseded by the CSP and has its own bug class |
| Permissions-Policy | denies accelerometer, autoplay, camera, display-capture, encrypted-media, geolocation, gyroscope, magnetometer, microphone, midi, payment, usb; allows clipboard-write, fullscreen and the WebAuthn entry points to self | deny what the panel never uses without breaking copy buttons, QR fullscreen or passkeys |

Headers are applied with setdefault, so a route that has a stronger, more
specific value keeps it.

## Content-Security-Policy

Sent only on text/html responses: JSON and asset responses execute nothing, so a
CSP there is pure noise (the dashboard snapshot is a multi-megabyte JSON body).

Directives: default-src, base-uri, object-src, frame-ancestors, form-action,
img-src, font-src, manifest-src, style-src, script-src, connect-src, plus
upgrade-insecure-requests on secure non-development requests. Inline scripts and
styles must carry the per-request nonce (g.csp_nonce); style-src-attr and
script-src-attr still allow inline attributes because the existing templates use
onclick/style attributes extensively - removing them is a template migration, not
a header change. When an online-chat widget is configured the subscription page
adds https: to script-src/connect-src and frame-src for the widget only.

## HSTS

Strict-Transport-Security is emitted only for secure requests outside
development: max-age=31536000; includeSubDomains. Setting EVE_HSTS_PRELOAD=1 adds
the preload token - only do that once every subdomain is guaranteed to speak
HTTPS, because the preload list is hard to undo.

## Cache policy for authenticated responses

Any response to a request that carries an admin or client session gets
Cache-Control: private, no-store, no-cache, must-revalidate, max-age=0 and
Vary: Cookie, unless it is a static asset (/static/, /assets/). The panel sits
behind a shared CDN, and several pages (dashboard, finance, settings) contain
per-operator data; without this policy the CDN could serve one operator's page to
another. Route-level values still win because the default uses setdefault, and
the public subscription routes (/s/, /cs/) keep their existing explicit
no-store rules.

## Tests

tests/test_security_headers.py asserts the baseline set, the Permissions-Policy
allow/deny split, the nonce-based CSP (and that the nonce matches the rendered
document), the absence of a CSP on JSON, HSTS behaviour across secure/insecure and
development/production requests, and the authenticated cache policy (API and
dashboard no-store, static assets untouched).
