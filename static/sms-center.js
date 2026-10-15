"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const tabs = ["overview", "audience", "runs", "delivery", "debt", "gateway"];
  // The whole table for the panel language (panel/core/sms_center_copy.py). A key that
  // is missing renders as the key itself, which tests/test_sms_center_copy.py forbids.
  const COPY = (typeof window !== "undefined" && window.SMS_COPY) || {};
  const T = (key, vars) => {
    let text = Object.prototype.hasOwnProperty.call(COPY, key) ? COPY[key] : key;
    if (vars) text = text.replace(/\{(\w+)\}/g, (match, name) => (vars[name] == null ? match : vars[name]));
    return text;
  };
  const has = (key) => Object.prototype.hasOwnProperty.call(COPY, key);
  const humanize = (code) => String(code).replaceAll("_", " ").replaceAll(".", " · ");
  const num = (value) => Number(value || 0).toLocaleString();
  let offset = 0;
  let total = 0;
  let visibleLogs = [];
  let audienceRows = [];
  let audienceDiagnostics = null;
  let audiencePage = 1;
  let audiencePagination = null;
  let selectedTab = "overview";
  let knownReasons = {};
  const pageSize = 30;
  const inlineDecisionLimit = 5;
  // Dispositions in the order a reader should meet them, and the funnel group of each.
  const funnelGroups = [
    { key: "eligible_now", legend: "funnel.ready", tone: "success" },
    { key: "deferred", legend: "funnel.waiting", tone: "warning" },
    { key: "active_obligation", legend: "funnel.pending", tone: "info" },
    { key: "invalid_recipient", legend: "funnel.nomobile", tone: "danger" },
    { key: "suppressed", legend: "funnel.blocked", tone: "muted" }
  ];
  const decisionOfReason = (code) => {
    if (code === "no_recipient") return "invalid_recipient";
    if (code === "obligation_outstanding") return "active_obligation";
    return null;
  };

  function node(tag, className, value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value != null) item.textContent = String(value);
    return item;
  }
  function showError(container, error) {
    container.replaceChildren(node("p", "field-note field-note-warn", T("error.unavailable", { message: error.message })));
  }
  function fmt(value) {
    return value == null || value === "" ? "—" : String(value);
  }
  function date(value) {
    if (!value) return "—";
    return window.EveDate ? window.EveDate.format(value) : String(value);
  }
  function dayKey(value) {
    const instant = window.EveDate?.parse(value);
    if (!instant) return null;
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: window.EveDate.timezone, year: "numeric", month: "2-digit", day: "2-digit"
    }).formatToParts(instant);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  }
  function dayHeading(value, todayInstant = new Date()) {
    const today = dayKey(todayInstant);
    const [year, month, day] = today.split("-").map(Number);
    const yesterday = new Date(Date.UTC(year, month - 1, day - 1)).toISOString().slice(0, 10);
    const key = dayKey(value);
    if (key === today) return T("day.today");
    if (key === yesterday) return T("day.yesterday");
    return window.EveDate?.formatDate(value) || date(value);
  }
  function filterBoundary(value, through = false) {
    if (!value) return "";
    const parsed = window.EveDate.parseInput(through ? `${value} 23:59:59` : value);
    if (!parsed) throw new Error(T("error.invalid_date"));
    return new Date(parsed.getTime() + (through ? 1000 : 0)).toISOString();
  }
  function carrierStateForCard(log) {
    return log.delivery?.carrier?.state || "unknown";
  }
  async function json(url) {
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(
      [payload?.probe_state || payload?.error || T("error.http", { status: response.status }),
        payload?.diagnostic || payload?.message].filter(Boolean).join(" · "));
    return payload;
  }
  function field(label, value) {
    const cell = node("div", "sms-center-fact");
    cell.append(node("span", "sms-audit-kpi-label", label), node("strong", "", fmt(value)));
    return cell;
  }
  function statusTone(value) {
    const status = String(value || "").toLowerCase();
    // Past the carrier wait window with no receipt: not delivered, not failed,
    // not unavailable — the evidence never arrived. Muted, not alarming.
    if (status === "unconfirmed") return "unconfirmed";
    if (["sent", "completed", "confirmed", "gateway_accepted", "accepted", "ready", "active", "delivered", "eligible_now", "connected", "enabled"].includes(status)
      || /\.(sent|completed|accepted|delivered)$/.test(status)) return "success";
    if (["failed", "failed_terminal", "cancelled", "expired", "ended", "invalid_recipient", "disabled"].includes(status)
      || /\.(failed|cancelled|expired)$/.test(status)) return "danger";
    if (["retry", "failed_retryable", "deferred", "manual_review", "queued", "pending", "waiting", "suppressed", "skipped", "degraded", "unavailable", "active_obligation"].includes(status)
      || /(pending|queued|retry|deferred|suppressed)/.test(status)) return "warning";
    return "neutral";
  }
  // A status code the panel knows is shown in the panel language; a code it has never
  // seen is shown readable rather than hidden.
  function statusLabel(value) {
    if (value == null || value === "") return "—";
    const code = String(value).toLowerCase();
    if (has(`status.${code}`)) return T(`status.${code}`);
    if (has(`reason.${code}`)) return T(`reason.${code}`);
    return humanize(value);
  }
  function statusField(label, value) {
    const cell = node("div", "sms-center-fact");
    cell.append(node("span", "sms-audit-kpi-label", label),
      node("strong", `sms-status sms-status-${statusTone(value)}`, statusLabel(value)));
    return cell;
  }
  function setKpi(id, value, tone = "neutral") {
    const target = $(id);
    target.textContent = value;
    target.classList.remove("sms-kpi-success", "sms-kpi-warning", "sms-kpi-danger", "sms-kpi-neutral");
    target.classList.add(`sms-kpi-${tone}`);
  }
  function expandableRow(fields, label) {
    const entry = node("article", "sms-center-entry");
    const button = node("button", "sms-center-row", "");
    const panel = node("div", "sms-center-expansion hidden");
    const panelId = `sms-center-detail-${Math.random().toString(36).slice(2)}`;
    button.type = "button";
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", panelId);
    panel.id = panelId;
    button.append(...fields, node("span", "sms-center-disclosure", label || T("action.view_details")));
    entry.append(button, panel);
    return { entry, button, panel };
  }
  function toggleExpansion(button, panel, open) {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    panel.classList.toggle("hidden", !open);
    const disclosure = button.querySelector(".sms-center-disclosure");
    if (disclosure) disclosure.textContent = open ? T("action.hide_details") : T("action.view_details");
  }
  function detailRow(fields) {
    const row = node("div", "sms-center-detail-row");
    row.append(...fields);
    return row;
  }
  // label / what it means / what to do, for a reason code the evaluator or a send log
  // recorded. An unknown code still gets a readable label and the code itself.
  function reasonInfo(code) {
    if (!code) return { label: T("field.not_reported"), what: "", fix: "" };
    if (has(`reason.${code}`)) {
      return { label: T(`reason.${code}`), what: T(`reason.${code}.what`), fix: T(`reason.${code}.fix`) };
    }
    return { label: humanize(code), what: T("reason.unknown.what", { code }), fix: T("reason.unknown.fix") };
  }
  function audienceReason(code) {
    return reasonInfo(code).label;
  }
  function reasonNote(code) {
    const info = reasonInfo(code);
    if (!info.what) return null;
    const note = node("p", "field-note sms-reason-note");
    note.append(node("span", "", info.what));
    if (info.fix) note.append(" ", node("strong", "", `${T("why.action")}: `), node("span", "", info.fix));
    return note;
  }
  function evidenceLabel(value) {
    const key = `evidence.${value}`;
    return has(key) ? T(key) : value;
  }
  function deliveryDetail(result) {
    const container = node("div", "sms-center-timeline");
    container.append(detailRow([
      statusField(T("delivery.gateway_submission"), result.submission?.state || "unknown"),
      field(T("delivery.submission_evidence"), evidenceLabel(result.submission?.evidence)),
      field(T("delivery.submitted_at"), date(result.submission?.occurred_at))
    ]), detailRow([
      statusField(T("delivery.carrier_delivery"), result.carrier?.state || "unknown"),
      field(T("delivery.carrier_evidence"), evidenceLabel(result.carrier?.evidence)),
      field(T("delivery.carrier_at"), date(result.carrier?.occurred_at))
    ]));
    const pipeline = node("div", "sms-center-pipeline");
    pipeline.append(node("h4", "", T("delivery.pipeline")));
    for (const step of result.pipeline || []) pipeline.append(detailRow([
      field(T("field.stage"), step.name), statusField(T("field.state_word"), step.state),
      field(T("field.at"), date(step.at)), field(T("field.evidence"), step.evidence || T("field.not_reported")),
      field(T("field.reason"), step.reason || "—")
    ]));
    container.append(pipeline);
    const diagnostics = result.carrier?.diagnostics || {};
    const reported = [
      ["diag.carrier_result", diagnostics.carrier_result],
      ["diag.carrier_code", diagnostics.carrier_result_code],
      ["diag.segment", diagnostics.segment_index],
      ["diag.segment_count", diagnostics.segment_count],
      ["diag.all_segments", diagnostics.all_segments_delivered],
      ["diag.android_received", diagnostics.android_delivery_received_at && date(diagnostics.android_delivery_received_at)],
      ["diag.gmweb_received", diagnostics.gmweb_delivery_received_at && date(diagnostics.gmweb_delivery_received_at)],
      ["diag.relay_attempts", diagnostics.eve_dispatch_attempts],
      ["diag.relay_last", diagnostics.eve_last_attempt_at && date(diagnostics.eve_last_attempt_at)],
      ["diag.relay_ack", diagnostics.eve_ack_at && date(diagnostics.eve_ack_at)],
      ["diag.relay_error", diagnostics.last_error],
      ["diag.wait_window", result.carrier?.timeout_seconds ? `${result.carrier.timeout_seconds} s` : undefined],
      ["diag.event_id", result.carrier?.event_id]
    ].filter(([, value]) => value !== undefined && value !== null && value !== "");
    if (reported.length) {
      const facts = node("div", "sms-center-pipeline");
      facts.append(node("h4", "", T("delivery.carrier_diagnostics")), detailRow(reported.map(([key, value]) => field(T(key), value))));
      container.append(facts);
    }
    return container;
  }
  function summaryCard(label, value, tone = "neutral", filter = null) {
    const card = node(filter ? "button" : "div", `stat-card${filter ? " sms-summary-filter" : ""}`);
    if (filter) {
      card.type = "button";
      card.dataset.filterKind = filter.kind;
      card.dataset.filterValue = filter.value;
      card.setAttribute("aria-pressed", "false");
      card.addEventListener("click", () => {
        const decision = $("sms-center-audience-decision");
        const activity = $("sms-center-audience-activity");
        $("sms-center-audience-reason").value = "";
        if (filter.kind === "all") { decision.value = ""; activity.value = ""; }
        if (filter.kind === "decision") { decision.value = filter.value; activity.value = ""; }
        if (filter.kind === "activity") { activity.value = filter.value; decision.value = ""; }
        loadAudience();
      });
    }
    card.append(node("span", "sms-audit-kpi-label", label),
      node("strong", `sms-audit-kpi-value sms-kpi-${tone}`, value));
    return card;
  }
  function showAudienceSkeleton() {
    const target = $("sms-center-audience-list");
    target.replaceChildren();
    for (let index = 0; index < 6; index += 1) {
      const row = node("div", "sms-center-detail-row sms-audience-skeleton");
      for (let cell = 0; cell < 6; cell += 1) row.append(node("span", "skeleton skeleton-line"));
      target.append(row);
    }
  }
  async function renderAudienceTimeline(panel, logId) {
    const timeline = node("div", "sms-center-timeline");
    panel.append(timeline);
    timeline.replaceChildren(node("p", "field-note", T("delivery.loading_timeline")));
    try {
      const result = await json(`/api/sms/messages/${logId}/timeline`);
      timeline.replaceChildren(deliveryDetail(result));
    } catch (error) { showError(timeline, error); }
  }
  function renderAudience() {
    const target = $("sms-center-audience-list");
    const rows = audienceRows;
    target.replaceChildren();
    if (!rows.length) {
      const scanned = Number(audienceDiagnostics?.scanned || 0);
      const detected = Object.values(audienceDiagnostics?.detected_states || {})
        .reduce((sum, count) => sum + Number(count || 0), 0);
      const message = !scanned ? T("audience.no_clients")
        : !detected ? T("audience.none_meet", { scanned: num(scanned) })
          : T("audience.no_match");
      target.append(node("p", "field-note", message));
      return;
    }
    rows.forEach((row) => {
      const lastSms = row.last_sms;
      const disclosure = expandableRow([
        field(T("field.account"), row.email || row.service_key), field(T("field.server"), row.server_name),
        statusField(T("field.account_state"), row.state), statusField(T("field.decision"), row.disposition),
        field(T("field.why"), audienceReason(row.reason_code)),
        statusField(T("field.last_sms"), lastSms?.status || "not_sent"),
        field(T("field.last_sms_time"), date(lastSms?.created_at))
      ], lastSms ? T("action.delivery_timeline") : T("action.decision_details"));
      disclosure.panel.append(detailRow([
        field(T("field.reason"), audienceReason(row.reason_code)), field(T("field.recipient"), row.recipient),
        statusField(T("field.account"), row.account_enabled ? "enabled" : "disabled"),
        field(T("field.expiry"), date(row.expiry_at)),
        field(T("field.today_results"), row.today_statuses?.length ? row.today_statuses.map(statusLabel).join(" · ") : T("field.none")),
        field(T("field.today_events"), row.today_events || 0)
      ]));
      const explanation = reasonNote(row.reason_code);
      if (explanation) disclosure.panel.append(explanation);
      let loaded = false;
      disclosure.button.addEventListener("click", async () => {
        const opening = disclosure.button.getAttribute("aria-expanded") !== "true";
        toggleExpansion(disclosure.button, disclosure.panel, opening);
        if (opening && lastSms && !loaded) {
          await renderAudienceTimeline(disclosure.panel, lastSms.id);
          loaded = true;
        }
      });
      target.append(disclosure.entry);
    });
    if (audiencePagination?.pages > 1) {
      const nav = node("div", "sms-audience-pagination");
      const previous = node("button", "btn btn-outline", T("action.previous"));
      const next = node("button", "btn btn-outline", T("action.next"));
      previous.type = next.type = "button";
      previous.disabled = audiencePagination.page <= 1;
      next.disabled = audiencePagination.page >= audiencePagination.pages;
      previous.addEventListener("click", () => { audiencePage -= 1; loadAudience(false); });
      next.addEventListener("click", () => { audiencePage += 1; loadAudience(false); });
      nav.append(previous, node("span", "field-note", T("audience.page", {
        page: num(audiencePagination.page), pages: num(audiencePagination.pages), total: num(audiencePagination.total)
      })), next);
      target.append(nav);
    }
  }
  // The reason filter lists the reasons the current evaluation actually produced, so
  // every option leads to at least one account.
  function fillReasonFilter(reasons) {
    const select = $("sms-center-audience-reason");
    const keep = select.value;
    select.replaceChildren(node("option", "", T("filter.any_reason")));
    select.firstChild.value = "";
    for (const [code, count] of Object.entries(reasons || {}).sort((a, b) => b[1] - a[1])) {
      const option = node("option", "", `${audienceReason(code)} (${num(count)})`);
      option.value = code;
      option.dataset.label = audienceReason(code);
      select.append(option);
    }
    if (keep && Array.from(select.options).some((option) => option.value === keep)) select.value = keep;
  }
  function audienceErrorMessage(response, data) {
    const source = data?.source;
    if (source?.state === "stale") return T("audience.stale", { date: date(source.last_update) });
    if (source?.state === "unavailable") return T("audience.unavailable");
    return data?.error || T("error.http", { status: response.status });
  }
  async function postPreview(body) {
    const response = await fetch("/api/sms/scan/preview", {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(audienceErrorMessage(response, data));
    return data;
  }
  async function loadAudience(resetPage = true) {
    const target = $("sms-center-audience-list");
    if (resetPage) audiencePage = 1;
    showAudienceSkeleton();
    try {
      const data = await postPreview({
        page: audiencePage,
        per_page: pageSize,
        search: $("sms-center-audience-search").value.trim(),
        decision: $("sms-center-audience-decision").value,
        reason: $("sms-center-audience-reason").value,
        activity: $("sms-center-audience-activity").value,
        state: $("sms-center-audience-state").value,
        date_from: filterBoundary($("sms-center-audience-from").value),
        date_to: filterBoundary($("sms-center-audience-to").value, true),
        hide_disabled: $("sms-center-hide-disabled").checked,
        hide_reseller: $("sms-center-hide-reseller").checked,
        hide_no_recipient: $("sms-center-hide-no-recipient").checked,
        hide_opted_out: $("sms-center-hide-opted-out").checked,
        hide_unlimited: $("sms-center-hide-unlimited").checked,
        hide_expired_days: $("sms-center-hide-expired-days").value
      });
      knownReasons = data.reasons || {};
      fillReasonFilter(knownReasons);
      audienceRows = data.candidates || [];
      audienceDiagnostics = data;
      audiencePagination = data.pagination || null;
      const summary = $("sms-center-audience-summary");
      // matched = ready + waiting + in progress + no mobile + blocked, so the cards add up.
      summary.replaceChildren(
        summaryCard(T("audience.summary.matches"), num(data.matched), "neutral", { kind: "all", value: "" }),
        summaryCard(T("audience.summary.eligible"), num(data.eligible_now), "success", { kind: "decision", value: "eligible_now" }),
        summaryCard(T("audience.summary.deferred"), num(data.deferred), "warning", { kind: "decision", value: "deferred" }),
        summaryCard(T("audience.summary.pending"), num(data.active_obligation), "warning", { kind: "decision", value: "active_obligation" }),
        summaryCard(T("audience.summary.nomobile"), num(data.invalid_recipient), "danger", { kind: "decision", value: "invalid_recipient" }),
        summaryCard(T("audience.summary.blocked"), num(data.suppressed), "danger", { kind: "decision", value: "suppressed" }),
        summaryCard(T("audience.summary.sent_today"), num(data.today?.sent), "success", { kind: "activity", value: "sent" }),
        summaryCard(T("audience.summary.failed_today"), num(data.today?.failed), "danger", { kind: "activity", value: "failed" }),
        summaryCard(T("audience.summary.queued_today"), num(data.today?.queued), "warning", { kind: "activity", value: "queued" })
      );
      const activeFilters = [];
      for (const id of ["sms-center-audience-decision", "sms-center-audience-reason",
        "sms-center-audience-activity", "sms-center-audience-state"]) {
        if ($(id).value) activeFilters.push($(id).selectedOptions[0].dataset.label || $(id).selectedOptions[0].textContent);
      }
      if ($("sms-center-audience-from").value || $("sms-center-audience-to").value) activeFilters.push(T("audience.date_range"));
      const filterState = $("sms-center-audience-filter-state");
      filterState.textContent = activeFilters.length
        ? T("audience.filter_state", { total: num(data.pagination?.total), filters: activeFilters.join(" · ") })
        : "";
      filterState.classList.toggle("hidden", !activeFilters.length);
      const source = data.source || {};
      const states = data.detected_states || {};
      $("sms-center-audience-source").replaceChildren(
        node("p", "field-note", T("audience.source", {
          date: source.last_update ? date(source.last_update) : T("audience.unknown"),
          inbounds: num(source.inbounds), clients: num(data.scanned),
          days: source.thresholds?.expiry_days ?? "?", gb: source.thresholds?.volume_gb ?? "?"
        })),
        node("p", "field-note", T("audience.detected", {
          near_expiry: states.near_expiry || 0, low_volume: states.low_volume || 0,
          expired: states.expired || 0, ended: states.ended || 0
        })));
      renderAudience();
    } catch (error) {
      audienceRows = [];
      audienceDiagnostics = null;
      audiencePagination = null;
      $("sms-center-audience-summary").replaceChildren();
      $("sms-center-audience-source").replaceChildren();
      showError(target, error);
    }
  }
  async function refreshAudienceSource() {
    const button = $("sms-center-audience-refresh");
    const target = $("sms-center-audience-list");
    button.disabled = true;
    $("sms-center-audience-summary").replaceChildren();
    $("sms-center-audience-source").replaceChildren();
    target.replaceChildren(node("p", "field-note", T("audience.refreshing")));
    try {
      const response = await fetch("/api/monitor/refresh", {
        method: "POST", credentials: "same-origin", cache: "no-store"
      });
      const started = await response.json().catch(() => null);
      if (!response.ok || !started?.job_id) throw new Error(started?.error || T("error.http", { status: response.status }));
      for (let attempt = 0; attempt < 60; attempt++) {
        const status = await json(`/api/monitor/job/${encodeURIComponent(started.job_id)}`);
        if (status.job?.state === "done") {
          await loadAudience();
          return;
        }
        if (status.job?.state === "error") throw new Error(status.job.error || T("audience.monitor_failed"));
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
      throw new Error(T("audience.monitor_slow"));
    } catch (error) {
      showError(target, error);
    } finally {
      button.disabled = false;
    }
  }
  function appendDiagnosticFacts(container, diagnostics) {
    const android = diagnostics?.androidActivity;
    const reports = diagnostics?.carrierReports;
    const outbox = diagnostics?.callbackOutbox;
    if (android) container.append(
      statusField(T("gateway.android_activity"), android.authoritative ? "authoritative" : "informational"),
      field(T("gateway.last_pull"), date(android.lastSuccessfulPullAt)),
      field(T("gateway.last_validate"), `${date(android.lastValidateAt)} / ${fmt(android.lastValidateResult)}`)
    );
    if (reports) container.append(
      field(T("gateway.carrier_reports"), reports.total),
      statusField(T("gateway.carrier_delivered"), reports.delivered),
      statusField(T("gateway.carrier_failed"), reports.failed),
      field(T("gateway.dlr_dupes"), `${fmt(reports.duplicates)} / ${fmt(reports.conflicts)}`),
      field(T("gateway.unknown_requests"), reports.unknownRequests),
      field(T("gateway.last_report"), date(reports.lastReportAt))
    );
    if (outbox) container.append(
      field(T("gateway.cb_pending"), `${fmt(outbox.pending)} / ${fmt(outbox.retry_wait)}`),
      field(T("gateway.cb_delivering"), outbox.delivering),
      field(T("gateway.cb_delivered"), outbox.delivered),
      statusField(T("gateway.cb_dead"), outbox.dead_letter),
      field(T("gateway.cb_oldest"), outbox.oldest_pending_age_ms == null ? null : `${outbox.oldest_pending_age_ms} ms`),
      field(T("gateway.cb_success"), date(outbox.last_success_at))
    );
  }

  async function loadHealth() {
    try {
      const report = await json("/api/sms/transport-health");
      const health = report.health || {};
      const transport = health.transport || {};
      const device = health.device || {};
      const queue = health.queue || {};
      setKpi("sms-center-health", report.success
        ? (health.gmweb?.ready ? T("health.ready") : T("health.degraded", { reason: health.gmweb?.reason ? statusLabel(health.gmweb.reason) : T("health.no_reason") }))
        : T("health.unavailable_probe", {
          state: report.probe_state ? statusLabel(report.probe_state) : T("health.unavailable"),
          diagnostic: report.diagnostic ? statusLabel(report.diagnostic) : T("health.no_diagnostic")
        }),
      report.success && health.gmweb?.ready ? "success" : "warning");
      setKpi("sms-center-device", report.success
        ? `${statusLabel(device.state || "unreported")}${device.reason ? ` · ${device.reason}` : ""}` : T("health.transport_unavailable"),
      report.success ? statusTone(device.state) : "neutral");
      setKpi("sms-center-queue", report.success ? fmt(queue.pending) : T("health.transport_unavailable"), "neutral");
      const facts = $("sms-center-gateway-facts");
      facts.replaceChildren(
        field(T("gateway.probe"), report.probe_state), field(T("gateway.transport"), transport.active),
        field(T("gateway.mode"), transport.mode), field(T("gateway.state"), transport.state),
        field(T("gateway.device_seen"), date(device.last_seen_at)),
        field(T("gateway.pending_inflight"), report.success ? `${fmt(queue.pending)} / ${fmt(queue.inflight)}` : "—"),
        field(T("gateway.last_ack"), date(health.last_ack?.at)), field(T("gateway.ack_outcome"), health.last_ack?.outcome)
      );
      appendDiagnosticFacts(facts, health.diagnostics || {});
    } catch (error) {
      const missingScope = error.message.includes("scope_denied") || error.message.includes("transport:read");
      setKpi("sms-center-health", T("error.unavailable", { message: error.message }), "danger");
      setKpi("sms-center-device", missingScope ? T("health.requires_scope") : T("health.probe_unavailable"), "neutral");
      setKpi("sms-center-queue", missingScope ? T("health.requires_scope") : T("health.queue_unavailable"), "neutral");
      showError($("sms-center-gateway-facts"), error);
    }
  }
  // "Right now": the funnel from "needs a reminder" to "ready", with every reason for
  // the accounts that are not ready. The numbers come from the same evaluator a run uses.
  function renderNow(data) {
    const matched = Number(data.matched || 0);
    const ready = Number(data.eligible_now || 0);
    const sent = Number(data.today?.sent || 0);
    $("sms-now-ready").textContent = num(ready);
    $("sms-now-of").textContent = !matched ? T("now.none")
      : T(sent ? "now.of_sent" : "now.of", { total: num(matched), sent: num(sent) });
    const pause = $("sms-center-pause");
    const blocked = data.run_state && data.run_state !== "ready" ? `pause.${data.run_state}` : null;
    pause.classList.toggle("hidden", !blocked || !has(blocked));
    pause.textContent = blocked && has(blocked) ? T(blocked) : "";
    const bar = $("sms-funnel-bar");
    const legend = $("sms-funnel-legend");
    bar.replaceChildren();
    legend.replaceChildren();
    for (const group of funnelGroups) {
      const count = Number(data[group.key] || 0);
      if (count > 0) {
        const segment = node("span", `sms-funnel-seg sms-funnel-${group.tone}`);
        segment.style.flexGrow = String(count);
        segment.title = `${T(group.legend)}: ${num(count)}`;
        bar.append(segment);
      }
      const item = node("li", "sms-funnel-item");
      const button = node("button", `sms-funnel-button sms-funnel-${group.tone}`);
      button.type = "button";
      button.disabled = !count;
      button.append(node("span", "sms-funnel-dot"), node("strong", "sms-funnel-count", num(count)),
        node("span", "sms-funnel-name", T(group.legend)), node("small", "field-note", T(`${group.legend}.help`)));
      button.addEventListener("click", () => openAudience({ decision: group.key }));
      item.append(button);
      legend.append(item);
    }
    if (!matched) bar.append(node("span", "sms-funnel-seg sms-funnel-empty"));
  }
  function renderWhy(data) {
    const target = $("sms-center-why");
    target.replaceChildren();
    const reasons = Object.entries(data.reasons || {}).sort((a, b) => b[1] - a[1]);
    if (!reasons.length) {
      target.append(node("p", "field-note", T("why.empty")));
      return;
    }
    for (const [code, count] of reasons) {
      const info = reasonInfo(code);
      const row = node("article", "sms-why-row");
      const head = node("div", "sms-why-head");
      head.append(node("strong", "sms-why-name", info.label), node("span", "badge neutral", T("why.count", { count: num(count) })));
      const body = node("div", "sms-why-body");
      body.append(node("p", "", info.what));
      if (info.fix) {
        const fix = node("p", "field-note");
        fix.append(node("strong", "", `${T("why.action")}: `), info.fix);
        body.append(fix);
      }
      const view = node("button", "btn btn-outline", T("why.view"));
      view.type = "button";
      view.addEventListener("click", () => openAudience({ reason: code, decision: decisionOfReason(code) }));
      row.append(head, body, view);
      target.append(row);
    }
  }
  // Jump to the audience tab already narrowed to a decision and/or a reason.
  function openAudience({ decision = "", reason = "" } = {}) {
    fillReasonFilter(knownReasons);
    $("sms-center-audience-decision").value = reason ? "" : decision;
    $("sms-center-audience-reason").value = reason;
    $("sms-center-audience-activity").value = "";
    $("sms-center-audience-state").value = "";
    selectTab("audience");
  }
  async function loadNow() {
    try {
      const data = await postPreview({ page: 1, per_page: 10 });
      knownReasons = data.reasons || {};
      renderNow(data);
      renderWhy(data);
    } catch (error) {
      $("sms-now-ready").textContent = "—";
      $("sms-now-of").textContent = T("now.unavailable", { message: error.message });
      $("sms-funnel-bar").replaceChildren();
      $("sms-funnel-legend").replaceChildren();
      showError($("sms-center-why"), error);
    }
  }
  function renderCarrierEvidence(evidence) {
    const target = $("sms-center-delivered");
    if (!evidence.evidence_available) {
      target.replaceChildren(node("p", "field-note", T("carrier.none")));
      return;
    }
    const card = (key, value, tone) => {
      const item = node("div", "stat-card");
      item.append(node("span", "sms-audit-kpi-label", T(`carrier.${key}`)),
        node("strong", `sms-audit-kpi-value sms-kpi-${tone}`, num(value)),
        node("small", "field-note", T(`carrier.${key}.help`)));
      return item;
    };
    target.replaceChildren(card("delivered", evidence.carrier_delivered, "success"),
      card("failed", evidence.carrier_failed, "danger"), card("pending", evidence.carrier_pending, "warning"));
  }
  async function loadOverview() {
    const now = loadNow();
    try {
      renderCarrierEvidence(await json("/api/sms/overview"));
    } catch (error) { showError($("sms-center-delivered"), error); }
    try {
      const data = await json("/api/sms/daily-summary");
      const statuses = data.statuses || {};
      $("sms-center-attempts").textContent = num(data.attempts);
      $("sms-center-unique").textContent = T("today.unique", { count: num(data.unique_accounts) });
      $("sms-center-sent").textContent = num(statuses.sent);
      $("sms-center-segments").textContent = T("today.segments", { count: num(data.segments) });
      $("sms-center-accepted").textContent = num(data.gateway_accepted);
      const notSent = Number(statuses.queued || 0) + Number(statuses.failed || 0) + Number(statuses.skipped || 0);
      $("sms-center-not-sent").textContent = num(notSent);
      $("sms-center-not-sent-detail").textContent = T("today.notsent.detail", {
        queued: num(statuses.queued), failed: num(statuses.failed), skipped: num(statuses.skipped)
      });
      $("sms-center-day-window").textContent = T("today.window", {
        date: window.EveDate?.formatDate(data.window_start) || date(data.window_start),
        zone: data.timezone || window.EveDate?.timezone || "Asia/Tehran"
      });
      $("sms-center-purpose").replaceChildren(
        field(T("purpose.created"), data.transactional?.created || 0), field(T("purpose.renew"), data.transactional?.renew || 0),
        field(T("purpose.purchase"), data.transactional?.purchase || 0), field(T("purpose.royalty"), data.royalty || 0),
        field(T("purpose.announcement"), data.announcement || 0), field(T("purpose.other"), data.other || 0));
      $("sms-center-reminders").replaceChildren(
        field(T("state.near_expiry"), data.reminders?.near_expiry || 0), field(T("state.low_volume"), data.reminders?.low_volume || 0),
        field(T("state.expired"), data.reminders?.expired || 0), field(T("state.ended"), data.reminders?.ended || 0));
    } catch (error) {
      for (const id of ["sms-center-attempts", "sms-center-sent", "sms-center-accepted", "sms-center-not-sent"])
        $(id).textContent = T("today.unavailable", { message: error.message });
    }
    await now;
  }
  async function loadRuns() {
    const target = $("sms-center-run-list");
    try {
      const data = await json("/api/sms/scan/runs?limit=50");
      target.replaceChildren();
      if (!data.runs?.length) { target.append(node("p", "field-note", T("runs.none"))); return; }
      for (const run of data.runs) {
        const disclosure = expandableRow([
          field(T("field.run"), run.run_id), field(T("field.started"), date(run.started_at)),
          field(T("field.targets"), run.matched_count), field(T("field.submitted"), run.submitted_count),
          field(T("field.skipped_deferred"), (run.suppressed_count || 0) + (run.deferred_count || 0))
        ], T("action.view_decisions"));
        let loaded = false;
        disclosure.button.addEventListener("click", async () => {
          const opening = disclosure.panel.classList.contains("hidden");
          toggleExpansion(disclosure.button, disclosure.panel, opening);
          if (!opening || loaded) return;
          disclosure.panel.replaceChildren(node("p", "field-note", T("runs.loading_decisions")));
          try {
            const result = await json(`/api/sms/scan/runs/${encodeURIComponent(run.run_id)}/decisions?limit=100`);
            const decisions = result.decisions || [];
            const list = node("div", "sms-center-detail-list");
            if (!decisions.length) list.append(node("p", "field-note", T("runs.no_decisions")));
            decisions.forEach((decision, index) => {
              const item = detailRow([
                field(T("field.account"), decision.client_email || decision.service_key || T("runs.account_fallback")),
                statusField(T("field.decision"), decision.disposition || "unrecorded"),
                field(T("field.reason"), decision.reason_code ? audienceReason(decision.reason_code) : T("field.no_reason_code")),
                field(T("field.recipient"), decision.recipient_masked),
                field(T("field.decided"), date(decision.decision_at))
              ]);
              if (index >= inlineDecisionLimit) item.classList.add("sms-center-overflow", "hidden");
              list.append(item);
            });
            disclosure.panel.replaceChildren(list);
            if (decisions.length > inlineDecisionLimit) {
              const showAll = T("runs.show_all", { count: result.total || decisions.length });
              const toggle = node("button", "btn btn-outline sms-center-more", showAll);
              toggle.type = "button";
              toggle.addEventListener("click", () => {
                const hidden = list.querySelector(".sms-center-overflow.hidden");
                list.querySelectorAll(".sms-center-overflow").forEach((item) => item.classList.toggle("hidden", !hidden));
                toggle.textContent = hidden ? T("runs.show_fewer") : showAll;
              });
              disclosure.panel.append(toggle);
            }
            loaded = true;
          } catch (error) { showError(disclosure.panel, error); }
        });
        target.append(disclosure.entry);
      }
    } catch (error) { showError(target, error); }
  }
  async function loadDelivery() {
    const target = $("sms-center-delivery-list");
    const params = new URLSearchParams({ limit: String(pageSize), offset: String(offset) });
    if ($("sms-center-search").value.trim()) params.set("q", $("sms-center-search").value.trim());
    if ($("sms-center-status").value) params.set("status", $("sms-center-status").value);
    const from = $("sms-center-from").value;
    const through = $("sms-center-to").value;
    try {
      if (from) params.set("from", filterBoundary(from));
      if (through) params.set("to", filterBoundary(through, true));
    } catch (error) { showError(target, error); return; }
    try {
      const data = await json(`/api/sms/logs?${params}`);
      total = data.total || 0;
      visibleLogs = data.logs || [];
      target.replaceChildren();
      if (!data.logs?.length) target.append(node("p", "field-note", T("delivery.none")));
      let previousDay = null;
      for (const log of data.logs || []) {
        const currentDay = dayKey(log.created_at || log.updated_at);
        if (currentDay !== previousDay) {
          target.append(node("h3", "sms-center-day-heading", dayHeading(log.created_at || log.updated_at)));
          previousDay = currentDay;
        }
        const gatewayStatus = log.gateway_state || (log.request_id ? "accepted_pending" : "not_submitted");
        const disclosure = expandableRow([
          field(T("field.account"), log.email), field(T("field.phone"), log.recipient),
          statusField(T("field.trigger"), log.state), statusField(T("field.eve_status"), log.status),
          statusField(T("field.submission"), gatewayStatus), statusField(T("field.carrier"), carrierStateForCard(log)),
          field(T("field.last_event"), date(log.updated_at)),
          field(T("field.reason"), log.reason ? audienceReason(log.reason) : log.reason)
        ], T("action.view_timeline"));
        let loaded = false;
        disclosure.button.addEventListener("click", async () => {
          const opening = disclosure.panel.classList.contains("hidden");
          toggleExpansion(disclosure.button, disclosure.panel, opening);
          if (!opening || loaded) return;
          disclosure.panel.replaceChildren(node("p", "field-note", T("delivery.loading_timeline")));
          try {
            const result = await json(`/api/sms/messages/${log.id}/timeline`);
            const timeline = node("div", "sms-center-timeline");
            const events = result.gateway_events || [];
            timeline.append(deliveryDetail(result));
            if (!events.length) timeline.append(node("p", "field-note field-note-warn", T("delivery.no_callback")));
            for (const event of events) timeline.append(detailRow([
              statusField(T("field.event"), event.type), field(T("field.occurred"), date(event.occurred_at)),
              field(T("field.attempt"), event.attempt), field(T("field.device"), event.device_id || T("field.not_reported")),
              field(T("field.reason"), event.reason_code)
            ]));
            const identity = detailRow([
              field(T("delivery.id.eve"), log.id), field(T("delivery.id.request"), log.request_id),
              field(T("delivery.id.gateway_request"), log.gateway_request_id), field(T("delivery.id.gateway_job"), log.gateway_job_id),
              field(T("delivery.id.notification"), log.eve_notification_id), field(T("delivery.id.correlation"), log.correlation_id)
            ]);
            identity.classList.add("sms-center-identity");
            disclosure.panel.replaceChildren(identity, timeline);
            loaded = true;
          } catch (error) { showError(disclosure.panel, error); }
        });
        target.append(disclosure.entry);
      }
      $("sms-center-page-label").textContent = T("delivery.page", {
        from: total ? offset + 1 : 0, to: Math.min(total, offset + pageSize), total
      });
      $("sms-center-prev").disabled = offset === 0;
      $("sms-center-next").disabled = offset + pageSize >= total;
    } catch (error) { showError(target, error); }
  }
  // The health strip shows the owed-reminder count on every tab, not only after the
  // "Owed reminders" tab has been opened.
  async function loadDebtCount() {
    try {
      const data = await json("/api/sms/notification-debt?limit=1");
      $("sms-center-debt-count").textContent = fmt(data.active);
    } catch (error) {
      $("sms-center-debt-count").textContent = T("debt.unavailable");
    }
  }
  async function loadDebt() {
    const target = $("sms-center-debt-list");
    try {
      const data = await json("/api/sms/notification-debt?limit=100");
      $("sms-center-debt-count").textContent = fmt(data.active);
      target.replaceChildren();
      if (!data.obligations?.length) target.append(node("p", "field-note", T("debt.none")));
      for (const item of data.obligations || []) {
        const row = node("div", "sms-center-row");
        row.append(field(T("field.account"), item.account || item.service_key),
          field(T("field.notification"), statusLabel(item.notification_kind)), statusField(T("field.state_word"), item.status),
          field(T("field.attempts"), item.attempt_count), field(T("field.reason"), item.last_error || (item.reason_code ? audienceReason(item.reason_code) : null)),
          field(T("field.next_retry"), date(item.next_attempt_at)));
        target.append(row);
      }
    } catch (error) {
      $("sms-center-debt-count").textContent = T("debt.unavailable");
      showError(target, error);
    }
  }
  async function reconcileDeliveryEvents() {
    const target = $("sms-center-reconciliation-results");
    const params = new URLSearchParams({ limit: $("sms-center-reconcile-limit").value });
    const requestId = $("sms-center-reconcile-request").value.trim();
    const status = $("sms-center-reconcile-status").value;
    if (requestId) params.set("requestId", requestId);
    if (status) params.set("status", status);
    target.replaceChildren(node("p", "field-note", T("recon.checking")));
    try {
      const result = await json(`/api/sms/delivery-events?${params}`);
      if (!result.available) {
        target.replaceChildren(node("p", "field-note field-note-warn", T("recon.unavailable")));
        return;
      }
      const comparison = result.comparison || {};
      const list = node("div", "sms-center-detail-list");
      list.append(detailRow([
        field(T("recon.remote_events"), result.events?.length || 0),
        field(T("recon.matched"), comparison.matched || 0),
        statusField(T("recon.missing_local"), comparison.remote_only_event_ids?.length || 0),
        field(T("recon.local_only"), comparison.local_only_event_ids?.length || 0),
        field(T("recon.authority"), comparison.authoritative_source),
        field(T("recon.mutations"), comparison.mutated_local_events)
      ]));
      for (const event of result.events || []) list.append(detailRow([
        statusField(T("field.carrier"), event.status), field(T("recon.event"), event.eventId),
        field(T("delivery.id.request"), event.requestId), field(T("delivery.id.gateway_request"), event.gatewayRequestId),
        field(T("field.occurred"), date(event.occurredAt)), statusField(T("recon.callback"), event.callbackState)
      ]));
      if (!result.events?.length) list.append(node("p", "field-note", T("recon.none")));
      target.replaceChildren(list);
    } catch (error) { showError(target, error); }
  }
  const tabLoaders = { overview: loadOverview, audience: loadAudience, runs: loadRuns,
    delivery: loadDelivery, debt: loadDebt };
  function selectTab(name, load = true) {
    if (!tabs.includes(name)) return;
    selectedTab = name;
    for (const tab of tabs) $("sms-center-" + tab).classList.toggle("hidden", tab !== name);
    for (const button of document.querySelectorAll("[data-sms-tab]"))
      button.classList.toggle("active", button.dataset.smsTab === name);
    history.replaceState(null, "", `#${name}`);
    if (load) tabLoaders[name]?.();
  }
  async function refresh() {
    const tasks = [loadHealth(), loadDebtCount()];
    if (tabLoaders[selectedTab]) tasks.push(tabLoaders[selectedTab]());
    await Promise.allSettled(tasks);
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { dayKey, dayHeading, filterBoundary, carrierStateForCard };
    return;
  }
  document.querySelectorAll("[data-sms-tab]").forEach((button) =>
    button.addEventListener("click", () => selectTab(button.dataset.smsTab)));
  $("sms-center-refresh").addEventListener("click", refresh);
  $("sms-center-open-audience").addEventListener("click", () => openAudience({ decision: "eligible_now" }));
  $("sms-center-audience-refresh").addEventListener("click", refreshAudienceSource);
  let audienceSearchTimer = null;
  $("sms-center-audience-search").addEventListener("input", () => {
    clearTimeout(audienceSearchTimer);
    audienceSearchTimer = setTimeout(() => loadAudience(), 350);
  });
  $("sms-center-audience-decision").addEventListener("change", () => loadAudience());
  for (const id of ["sms-center-audience-reason", "sms-center-audience-activity", "sms-center-audience-state",
    "sms-center-audience-from", "sms-center-audience-to", "sms-center-hide-disabled",
    "sms-center-hide-reseller", "sms-center-hide-no-recipient", "sms-center-hide-opted-out",
    "sms-center-hide-unlimited", "sms-center-hide-expired-days"]) {
    $(id).addEventListener("change", () => loadAudience());
  }
  $("sms-center-filter").addEventListener("click", () => { offset = 0; loadDelivery(); });
  $("sms-center-reconcile").addEventListener("click", reconcileDeliveryEvents);
  $("sms-center-search").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { offset = 0; loadDelivery(); }
  });
  $("sms-center-prev").addEventListener("click", () => { offset = Math.max(0, offset - pageSize); loadDelivery(); });
  $("sms-center-next").addEventListener("click", () => { offset += pageSize; loadDelivery(); });
  $("sms-center-export").addEventListener("click", () => {
    const columns = ["id", "email", "recipient", "state", "status", "request_id", "gateway_state", "reason", "updated_at"];
    const escape = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
    const csv = [columns.join(","), ...visibleLogs.map((row) => columns.map((key) => escape(row[key])).join(","))].join("\r\n");
    const blob = new Blob(["﻿", csv], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "eve-sms-delivery-page.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 0);
  });
  selectTab(location.hash.slice(1) || "overview", false);
  if (window.EveDate?.calendar === "jalali" && typeof jalaliDatepicker !== "undefined") {
    jalaliDatepicker.startWatch({ time: false });
  }
  refresh();
  setInterval(() => {
    if (document.hidden) return;
    loadHealth();
  }, 30000);
})();
