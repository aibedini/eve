# 📨 SMS Gateway — Delivery-Confirmation Integration Spec

> **For the GMweb-API gateway project.** Hand this whole file to Claude on the **gateway** side. It explains how Eve currently talks to the gateway, what is missing (real delivery confirmation), and exactly what the gateway must add so Eve can show **delivered / failed** instead of just **accepted**. The end has a **"What to send back to Eve"** checklist — answer it.

> 🇮🇷 خلاصه فارسی: الان وقتی Eve یک SMS می‌فرسته، gateway فقط `200/202` (یعنی «درخواست رو قبول کردم») برمی‌گردونه. Eve همین رو به‌عنوان «sent» ثبت می‌کنه — ولی این **رسیدن واقعی به گوشی مقصد نیست**. این داکیومنت می‌گه gateway چه چیزی باید برگردونه تا Eve وضعیت واقعی (delivered/failed) رو بفهمه. این فایل رو به Claude پروژه‌ی gateway بده و جواب بخش آخر رو برگردون.

---

## 1) Who is calling you

**Eve — X-UI Manager** sends transactional and reminder SMS through your gateway. It is a Flask app; sends happen from background threads and a periodic scan. Volume is bursty (tens of messages in a scan), which is why pacing and 429 handling already exist on Eve's side.

## 2) Current integration (what Eve does **today**)

Base URL and a project API key are configured in Eve. Three calls are used:

**Health check (public):**
```http
GET {BASE}/health
```

**Readiness (token-validated):**
```http
GET {BASE}/ready
Authorization: Bearer {API_KEY}
```
- `200` → ready, `401` → bad key, `503` → not paired/connected yet.

**Send one SMS:**
```http
POST {BASE}/send
Authorization: Bearer {API_KEY}
Content-Type: application/json

{ "to": "09123456789", "text": "..." }
```

**Promote delayed high-priority jobs (queue control):**
```http
POST {BASE}/queue/promote-high
Authorization: Bearer {API_KEY}
Content-Type: application/json

{
  "all": true,
  "priority": "high",
  "states": ["delayed"],
  "releaseDelayed": true,
  "position": "front"
}
```

Expected `200`/`202` response:
```json
{ "success": true, "promoted": 12 }
```

This operation must mutate existing queued jobs rather than enqueue copies. It
sets their due time to now and places them before normal-priority work.

**How Eve interprets the result today:**
- HTTP `200` or `202` → Eve marks the row **`sent`**.
- Any other status → **`failed`** with reason `http_<code>` (e.g. `http_429`).
- ⚠️ **Eve does NOT read the response body.** Any `jobId`/`messageId` you return is currently ignored.
- ⚠️ **There is no delivery tracking.** "sent" only means *"the gateway accepted the request"*, not *"the SIM actually delivered it"*.

## 3) The problem we want to solve

Operators see **`sent`** in Eve's log, but the message may never have left the phone (no signal, SIM out of credit, carrier rejection, wrong number, queued forever). We need the **real outcome** so the log shows **delivered / failed**, not just **accepted**.

To do that, the gateway must (a) return a **stable message id** on `/send`, and (b) expose the **delivery status** for that id — via **webhook (preferred)** and/or **polling (fallback)**.

---

## 4) What we need you to add

### 4.0 `/send` must return a stable message id

Change the `/send` response body to **always** include a unique id Eve can store and look up later.

```jsonc
// 202 Accepted
{
  "id": "msg_abc123",          // REQUIRED: stable, unique per message
  "status": "queued",          // queued | sending  (initial state)
  "to": "09123456789",
  "accepted_at": "2026-06-25T09:10:17Z"   // ISO-8601 UTC
}
```

### 4.1 Delivery status lifecycle (define these states)

Use this enum for every status you report (webhook and polling):

| status | meaning |
|---|---|
| `queued` | accepted by the gateway, not yet handed to the SIM/modem |
| `sending` | handed to the modem, awaiting network result |
| `sent` | the network accepted it (left the device) |
| `delivered` | **confirmed delivered to the handset** (delivery report received) |
| `failed` | permanently failed (carrier reject, no credit, bad number, etc.) |
| `expired` | gave up after retries/timeout |
| `unknown` | no delivery report available (modem/carrier doesn't support DLR) |

> If the SIM/modem **cannot** provide real delivery reports (DLR), say so — see §6. In that case the best you can offer is `sent` (left the device) and we'll treat that as the terminal success state.

### 4.2 Option A — Webhook callback (PREFERRED)

When a message's status changes (at least to a terminal state: `delivered` / `failed` / `expired`), POST to a callback URL Eve configures:

```http
POST {EVE_CALLBACK_URL}
Content-Type: application/json
X-GMweb-Signature: sha256=<hex hmac of the raw body using a shared secret>

{
  "id": "msg_abc123",
  "to": "09123456789",
  "status": "delivered",          // any status from §4.1
  "error_code": null,              // string/enum when status=failed (see §4.3)
  "error_detail": null,            // human-readable reason when failed
  "updated_at": "2026-06-25T09:10:42Z",
  "segments": 1                    // number of SMS parts, if known
}
```

Requirements:
- **Retry** the webhook with backoff (e.g. 3–5 tries) until Eve answers `2xx`.
- **Sign** the body (HMAC-SHA256 with a shared secret) so Eve can verify authenticity → header `X-GMweb-Signature`.
- Webhook must be **idempotent** (same `id` + `status` may arrive more than once; Eve will dedupe).

### 4.3 Option B — Polling status endpoint (FALLBACK / also nice to have)

Let Eve poll by id:

```http
GET {BASE}/status/{id}
Authorization: Bearer {API_KEY}
```
```jsonc
// 200 OK
{
  "id": "msg_abc123",
  "to": "09123456789",
  "status": "delivered",
  "error_code": null,
  "error_detail": null,
  "accepted_at": "2026-06-25T09:10:17Z",
  "updated_at": "2026-06-25T09:10:42Z",
  "segments": 1
}
```
- `404` if the id is unknown/expired.
- Bonus: a **batch** form `POST {BASE}/status` with `{ "ids": ["msg_a","msg_b"] }` → array, so Eve can poll many at once without hammering you.

### 4.4 Standardize the error vocabulary (for `failed`)

Return a **stable machine code** plus a human detail. Suggested codes:

`no_credit`, `invalid_number`, `blocked_number`, `carrier_rejected`, `no_signal`,
`sim_not_ready`, `rate_limited`, `expired`, `unknown_error`.

### 4.5 Rate-limit signalling (improve the existing 429)

When you return `429`, include a `Retry-After` header (seconds) so Eve waits exactly that long instead of guessing. Optionally a JSON body `{ "retry_after": 5 }`.

---

## 5) What Eve will do on its side (so you know the shape of the contract)

- Store your `id` against each send-log row.
- If you support **webhook**: Eve will expose `POST /api/sms/gmweb/delivery` (token/secret protected) and update the row's status to `delivered` / `failed` (+ reason) when your callback arrives.
- If you support **polling**: Eve will poll `/status/{id}` for non-terminal messages a few times with backoff, then stop.
- Eve already paces sends and backs off on `429`; a `Retry-After` just makes that exact.

---

## 6) ❓ Questions to answer / what to send back to Eve

Please reply with **concrete answers** so Eve can implement the matching side:

1. **Delivery reports (DLR):** Does the underlying SIM/modem/carrier give real delivery confirmations? If yes, which of `delivered` can you actually guarantee? If no, is `sent` (left the device) the best terminal state you can offer?
2. **`/send` id:** Will you return a stable `id` in the `/send` response now? What exact field name and JSON shape? Paste the real response body.
3. **Webhook:** Can you POST status callbacks? If yes:
   - What is the exact payload (paste a real example for `delivered` and for `failed`)?
   - How do you sign it (HMAC algo + which header)? How does Eve set the **shared secret** and the **callback URL** (config field? API call)?
   - Retry policy (how many tries, backoff)?
4. **Polling:** Will you expose `GET /status/{id}` (and/or batch `POST /status`)? Paste the real response shape and the `404` behavior.
5. **Error codes:** What is your actual list of failure codes/strings, and what does each mean?
6. **Rate limits:** What are the real limits (per second / minute / day)? Do you send `Retry-After` on `429`?
7. **Idempotency:** If Eve retries a `/send` (network blip), will you create a **duplicate** SMS, or do you support an idempotency key (e.g. header `Idempotency-Key`)? If supported, what's the header/field?
8. **ID lifetime:** How long is a message `id` queryable via `/status` before it's purged?
9. **Timestamps & timezone:** Confirm all timestamps are ISO-8601 **UTC** (`...Z`). Eve converts to Asia/Tehran for display.

> When you've answered, paste the answers back into Eve's Claude so it can wire up the delivery side (store `id`, add the webhook endpoint and/or the polling loop, and surface `delivered` / `failed` in the SMS log).

---

## 7) Minimal vs. full

- **Minimum useful:** §4.0 (return an `id`) + §4.3 (polling `/status/{id}`) + §4.4 (error codes). Eve can then show real outcomes by polling.
- **Best:** add §4.2 (signed webhook) so Eve updates instantly without polling, plus §4.5 (`Retry-After`) and §6.7 (idempotency key) for robustness.
