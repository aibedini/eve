# Hot-path query budget

## Problem

The baseline harness (`scripts/benchmark_baseline.py`) recorded the number of SQL
statements per request. Three finance/dashboard paths were far above a sane budget:

| scenario | rows | statements before |
|----------|------|-------------------|
| `api_transactions_page` | 20 | 65 |
| `api_transactions_search` | 20 | 66 |
| `api_payments_page` | 60 | 86 |

A new profiler (`scripts/benchmark_queries.py`) recorded *which* statements repeat,
which identified three independent causes:

1. **`system_settings` re-SELECTed once per rendered row.** `format_app_datetime`
   reads the calendar and timezone through `_get_or_create_system_setting`, and
   the finance payload calls it for every row. Each call did
   `db.session.get(SystemSetting, key)` and then dropped its local reference.
   SQLAlchemy's identity map only holds instances **weakly**, so the row was
   collected as soon as the helper returned and the next call re-SELECTed it.
   A 20-row list therefore issued 40 pointless SELECTs (`x40` in the profile), and
   a payments page (payments + transactions + receipts) issued 80.
2. **Creating a missing default expired the whole session.** On a database whose
   defaults had not been seeded, the first read inserted the row and called
   `db.session.commit()`. With `expire_on_commit=True` that expired every object
   the request had just loaded, so each row was re-SELECTed and its relationships
   reloaded lazily (20 extra `SELECT transactions` for a 20-row page).
3. **Missing eager loading.** `GET /api/transactions` loaded no relationships, so
   `to_dict()` triggered three lazy SELECTs per row (admin, server, card), and
   `ManualReceipt.to_dict()` triggered one more (reviewer) per receipt row.

## Change

* `panel/core`-free app helpers in `app.py`:
  * `_current_db_session()` returns the real `Session` behind the
    `scoped_session` proxy (`scoped_session` does not forward `.info` or
    `.expire_on_commit`).
  * `_get_or_create_system_setting` memoizes values in `db.session.info`
    (`SETTINGS_MEMO_KEY`), so one strong reference per key lives for the session
    (one request, or one background-job cycle) instead of one SELECT per row.
    The memo is cleared by `after_commit` / `after_rollback` listeners, so a
    setting written in the same request is visible immediately.
  * When the helper must create a missing default it commits with
    `expire_on_commit=False` for that one insert. The statement adds a single row
    and changes nothing else, so the rest of the session must not be invalidated.
* `panel/routes/finance.py`: `GET /api/transactions` eager-loads
  `Transaction.admin/card/server`, and the payments list also eager-loads
  `ManualReceipt.reviewer`.
* `scripts/benchmark_queries.py` is the new measurement tool: it runs the same
  scenarios as the baseline harness and reports the top repeated statements per
  scenario (normalized, no parameters, no secrets).

## Result

Measured with `scripts/benchmark_baseline.py` on the same dataset (12 servers, 30
inbounds, 50 clients, 5000 transactions, 3000 payments, 4000 ownerships):

| scenario | statements before | after | change |
|----------|-------------------|-------|--------|
| `api_transactions_page` | 65 | 6 | -91% |
| `api_transactions_search` | 66 | 6 | -91% |
| `api_payments_page` | 86 | 8 | -91% |
| `html_dashboard` | 11 | 11 | unchanged |
| `html_login` | 5 | 5 | unchanged |
| `api_finance_stats` | 16 | 16 | unchanged (12 distinct aggregates) |

Latency moved in the same direction on the affected endpoints (mean 125 -> 108 ms
for the transaction page, 152 -> 114 ms for the search, 202 -> 176 ms for the
payments page). The machine is noisy at this scale, so the statement counts in
`docs/performance/query-before.json` and `query-after.json` are the primary metric;
both files were produced by the same harness and dataset.

## Residual work

`GET /api/finance/stats` still issues 12 aggregate queries (today, week, month,
previous-period comparisons for both transactions and payments). They are distinct
date windows rather than an N+1, and the endpoint answers in ~80 ms; collapsing
them into conditional `SUM(CASE WHEN ...)` aggregates is a separate change with a
larger correctness surface, so it is left for a later phase and recorded here with
a measurement.

`api_permissions` issues two `admin_permissions` SELECTs; that is the permission
loader resolving the role and its overrides, not a per-row pattern.

The settings memo is scoped to one session and cleared by any commit or rollback.
Only display settings flow through it (timezone, calendar, language, expiry
thresholds), so a long-lived read-only session that never commits can serve a
just-changed calendar until its next commit. Nothing security-relevant is
memoized.

## Verification

`tests/test_query_budget.py` (9 tests) pins the behaviour: the settings memo
serves repeated reads with at most one SELECT, a commit clears it, creating a
default no longer expires loaded rows, the memo does not leak across sessions, and
both finance list endpoints stay inside a fixed statement budget with a 20-row page.
