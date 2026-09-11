# Bounded list responses (pagination contract)

## Problem

A single GET must never materialise an unbounded table. The audit of every
`/api` list endpoint found most of them already fine (receipts clamp at 1000, SMS
logs at 1000, finance at 100 per page, announcement deliveries at 200, pulse queue
at 200), but the BNQO control-plane lists had no ceiling at all:

* `GET /api/bnqo/links` ran `BnqoLink.query.order_by(...).all()` and serialised
  every row, with `to_dict()` lazy-loading both agents per row.
* `GET /api/bnqo/agents` did the same for agents.
* `GET /api/bnqo/incidents` hardcoded `.limit(500)`: bounded, but the number was
  not configurable and it returned no total, so a client could not tell whether
  more rows existed.

The link inventory is the one that grows with every monitored path, so its response
size, query time and memory grew with the deployment.

## Change

`panel/routes/common.py` now owns one pagination contract:

* `page_params(default, maximum)` reads `limit`/`offset` (or `page`/`per_page`),
  applies the default for a missing limit, clamps an oversized one to the server
  maximum and rejects garbage and negatives safely. Defaults are
  `DEFAULT_PAGE_SIZE = 200` and `MAX_PAGE_SIZE = 1000`.
* `page_meta(total, limit, offset)` returns `total`, `limit`, `offset`, `count`,
  `has_more` and `next_offset` so a client can walk the table without guessing.
* `paginate_query(query, default, maximum)` returns `(rows, meta)`, keeping the
  caller ORDER BY and dropping it only for the count.

Applied to the BNQO admin API:

* `GET /api/bnqo/links` is paged, eager-loads both agents for the page (no per-row
  SELECT), and accepts `?link_id=` for the single-link lookup the detail page
  needs.
* `GET /api/bnqo/agents` is paged with the same metadata.
* `GET /api/bnqo/incidents` keeps its 500-row default but accepts `?limit=` up to
  1000 and reports the total.

The two template consumers (`bnqo_links.html`, `bnqo_link_detail.html`) keep working
with large inventories: the list page walks the pages with a small
`fetchAllPages` helper, and the detail page uses the new `link_id` filter instead of
downloading the whole inventory to find one row.

## Result

Measured with `scripts/benchmark_pagination.py` on a seeded inventory of 1000
links (`docs/performance/api-pagination.json`). The unbounded column reproduces the
replaced query exactly (`BnqoLink.query...all()` plus `to_dict()`), so it is the
behaviour of commit `6d4bf3d`:

| shape | rows | bytes | mean ms | statements |
|-------|------|-------|---------|------------|
| unbounded (before) | 1000 | 288,783 | 54.1 | 3 |
| one page (after) | 200 | 57,687 | 27.4 | 2 |

That is a 5x reduction in rows and bytes per request, with the response time
halved. A client that really wants every row still gets it in 5 requests
(`full walk: 5 requests, 1000 rows, 1000 unique`), each bounded and each reporting
`has_more`/`next_offset`.

## Residual risk

* The ceiling is per request, not per table: a client can still walk every page.
  That is intentional (the operator dashboard needs the inventory), and each page
  is bounded in memory, query time and response size.
* `getRowCount` via `count()` adds one COUNT query per request. It is indexed by
  the primary key ordering and cheap next to the row fetch; the measured statement
  count is 2 per page.
* Agents or links beyond `MAX_PAGE_SIZE` are no longer in the first response. Both
  UI consumers page through, and `has_more`/`next_offset` make the contract
  explicit for future clients.

## Verification

`tests/test_api_pagination.py` (13 tests): the helper defaults, clamps, aliases,
garbage handling and metadata; the 750-link endpoint returning a bounded default
page with the right total and `next_offset`; walking to the last page; the maximum
clamp; the single-link filter; a statement budget proving the page does not load
agents per row; the agents total; and the incidents default with `limit` support.
`tests/test_bnqo_web.py` (22 tests) still passes unchanged.
