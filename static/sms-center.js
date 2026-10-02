"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const tabs = ["overview", "audience", "runs", "delivery", "debt", "gateway"];
  let offset = 0;
  let total = 0;
  let visibleLogs = [];
  let audienceRows = [];
  let audienceDiagnostics = null;
  let audiencePage = 1;
  let audiencePagination = null;
  let selectedTab = "overview";
  const pageSize = 30;
  const inlineDecisionLimit = 5;

  function node(tag, className, value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value != null) item.textContent = String(value);
    return item;
  }
  function showError(container, error) {
    container.replaceChildren(node("p", "field-note field-note-warn", `Unavailable: ${error.message}`));
  }
  function fmt(value) {
    return value == null || value === "" ? "—" : String(value);
  }
  function date(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
  }
  async function json(url) {
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    const payload = await response.json().catch(() => null);
    if (!response.ok) throw new Error(
      [payload?.probe_state || payload?.error || `HTTP ${response.status}`,
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
    if (["sent", "completed", "confirmed", "gateway_accepted", "accepted", "ready", "active", "delivered", "eligible_now", "healthy"].includes(status)
      || /\.(sent|completed|accepted|delivered)$/.test(status)) return "success";
    if (["failed", "failed_terminal", "cancelled", "expired", "ended", "invalid_recipient"].includes(status)
      || /\.(failed|cancelled|expired)$/.test(status)) return "danger";
    if (["retry", "failed_retryable", "deferred", "manual_review", "queued", "pending", "suppressed", "skipped", "degraded", "unavailable", "callback_missing", "not_configured"].includes(status)
      || /(pending|queued|retry|deferred|suppressed)/.test(status)) return "warning";
    return "neutral";
  }
  function statusField(label, value) {
    const cell = node("div", "sms-center-fact");
    const display = value == null || value === ""
      ? "—"
      : String(value).replaceAll("_", " ").replaceAll(".", " · ");
    cell.append(node("span", "sms-audit-kpi-label", label),
      node("strong", `sms-status sms-status-${statusTone(value)}`, display));
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
    button.append(...fields, node("span", "sms-center-disclosure", label || "View details"));
    entry.append(button, panel);
    return { entry, button, panel };
  }
  function toggleExpansion(button, panel, open) {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    panel.classList.toggle("hidden", !open);
    const disclosure = button.querySelector(".sms-center-disclosure");
    if (disclosure) disclosure.textContent = open ? "Hide details" : "View details";
  }
  function detailRow(fields) {
    const row = node("div", "sms-center-detail-row");
    row.append(...fields);
    return row;
  }
  const audienceReasonLabels = {
    sms_disabled: "SMS automation is disabled or no state trigger is enabled",
    gateway_not_ready: "Gateway is not ready",
    quiet_hours: "Quiet hours are active",
    trigger_disabled_by_operator: "This state trigger is disabled",
    reseller_owned: "Owned by a reseller",
    unlimited_skipped: "Unlimited-account policy excludes it",
    expired_too_old: "Expired longer than the configured age limit",
    ended_too_old: "Ended longer than the configured age limit",
    opted_out_recheck: "Account opted out of SMS",
    no_recipient: "No valid Iranian mobile was found",
    manual_review_pending: "A previous uncertain send needs manual review",
    obligation_outstanding: "The same notification is already outstanding",
    cooldown_active: "A message was sent inside the cooldown window",
    no_template: "No template is configured for this state",
    empty_message: "The rendered message is empty",
    daily_limit_reached: "Daily send limit reached",
    hourly_limit_reached: "Hourly send limit reached",
    evaluation_failed: "Eligibility evaluation failed"
  };
  function audienceReason(code) {
    if (!code) return "All read-only checks passed";
    return audienceReasonLabels[code] || String(code).replaceAll("_", " ");
  }
  function evidenceLabel(value) {
    if (value === "not_available") return "No evidence recorded";
    if (value === "historical_evidence_unavailable") return "Historical evidence was not collected for this message";
    if (value === "callback_pending") return "Waiting for signed GMweb callback";
    if (value === "callback_missing") return "Expected signed GMweb callback was not received";
    if (value === "carrier_not_exposed") return "Carrier delivery reports are not exposed for this message";
    if (value === "awaiting_carrier_receipt") return "Waiting for carrier receipt";
    return value;
  }
  function summaryCard(label, value, tone = "neutral") {
    const card = node("div", "stat-card");
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
  function noCallbackMessage(result) {
    const state = result.submission?.state;
    if (state === "legacy_record") {
      return { tone: "field-note", text: "Historical message: signed callback evidence was not collected when this send was recorded." };
    }
    if (state === "callback_missing" || result.submission?.actionable) {
      return { tone: "field-note field-note-warn", text: "Expected signed GMweb callback is missing. Check the callback URL/secret and GMweb callback outbox." };
    }
    return { tone: "field-note", text: "No signed callback has been recorded yet." };
  }
  async function renderAudienceTimeline(panel, logId) {
    panel.replaceChildren(node("p", "field-note", "Loading delivery timeline…"));
    try {
      const result = await json(`/api/sms/messages/${logId}/timeline`);
      const content = [detailRow([
        statusField("Gateway submission", result.submission?.state || "unknown"),
        field("Submission evidence", evidenceLabel(result.submission?.evidence)),
        statusField("Carrier outcome", result.carrier?.state || "not_exposed"),
        field("Carrier evidence", evidenceLabel(result.carrier?.evidence))
      ])];
      if (!(result.gateway_events || []).length) {
        const note = noCallbackMessage(result);
        content.push(node("p", note.tone, note.text));
      }
      panel.replaceChildren(...content);
    } catch (error) { showError(panel, error); }
  }
  function renderAudience() {
    const target = $("sms-center-audience-list");
    const rows = audienceRows;
    target.replaceChildren();
    if (!rows.length) {
      const scanned = Number(audienceDiagnostics?.scanned || 0);
      const detected = Object.values(audienceDiagnostics?.detected_states || {})
        .reduce((sum, count) => sum + Number(count || 0), 0);
      const message = !scanned ? "The snapshot contains no clients to evaluate."
        : !detected ? `Evaluated ${scanned.toLocaleString()} clients; none meet the SMS thresholds.`
          : "No current accounts match these filters.";
      target.append(node("p", "field-note", message));
      return;
    }
    rows.forEach((row) => {
      const lastSms = row.last_sms;
      const disclosure = expandableRow([
        field("Account", row.email || row.service_key), field("Server", row.server_name),
        statusField("Monitor state", row.state), statusField("Decision", row.disposition),
        statusField("EVE send log", lastSms?.status || "not sent"),
        field("EVE log time", date(lastSms?.created_at))
      ], lastSms ? "Delivery timeline" : "Decision details");
      disclosure.panel.append(detailRow([
        field("Reason", audienceReason(row.reason_code)), field("Recipient", row.recipient)
      ]));
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
      const previous = node("button", "btn btn-outline", "Previous");
      const next = node("button", "btn btn-outline", "Next");
      previous.type = next.type = "button";
      previous.disabled = audiencePagination.page <= 1;
      next.disabled = audiencePagination.page >= audiencePagination.pages;
      previous.addEventListener("click", () => { audiencePage -= 1; loadAudience(false); });
      next.addEventListener("click", () => { audiencePage += 1; loadAudience(false); });
      nav.append(previous, node("span", "field-note",
        `Page ${audiencePagination.page.toLocaleString()} of ${audiencePagination.pages.toLocaleString()} · ${audiencePagination.total.toLocaleString()} accounts`), next);
      target.append(nav);
    }
  }
  async function loadAudience(resetPage = true) {
    const target = $("sms-center-audience-list");
    if (resetPage) audiencePage = 1;
    showAudienceSkeleton();
    try {
      const response = await fetch("/api/sms/scan/preview", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          page: audiencePage,
          per_page: pageSize,
          search: $("sms-center-audience-search").value.trim(),
          decision: $("sms-center-audience-decision").value
        })
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const source = data?.source;
        throw new Error(source?.state === "stale"
          ? `Live Monitor snapshot is stale (last updated ${date(source.last_update)}).`
          : source?.state === "unavailable"
            ? "Audience source unavailable. Live Monitor snapshot has not been loaded."
            : data?.error || `HTTP ${response.status}`);
      }
      audienceRows = data.candidates || [];
      audienceDiagnostics = data;
      audiencePagination = data.pagination || null;
      const summary = $("sms-center-audience-summary");
      summary.replaceChildren(
        summaryCard("Current matches", Number(data.matched || 0).toLocaleString()),
        summaryCard("Eligible now", Number(data.eligible_now || 0).toLocaleString(), "success"),
        summaryCard("Deferred", Number(data.deferred || 0).toLocaleString(), "warning"),
        summaryCard("Suppressed", Number(data.suppressed || 0).toLocaleString(), "danger"),
        summaryCard("Sent today", Number(data.today?.sent || 0).toLocaleString(), "success"),
        summaryCard("Failed today", Number(data.today?.failed || 0).toLocaleString(), "danger"),
        summaryCard("Queued today", Number(data.today?.queued || 0).toLocaleString(), "warning")
      );
      const source = data.source || {};
      const states = data.detected_states || {};
      $("sms-center-audience-source").replaceChildren(
        node("p", "field-note",
          `Live Monitor: ${source.last_update ? date(source.last_update) : "unknown"} · ${Number(source.inbounds || 0).toLocaleString()} inbounds · ${Number(data.scanned || 0).toLocaleString()} clients scanned · SMS thresholds: ${source.thresholds?.expiry_days ?? "?"} days / ${source.thresholds?.volume_gb ?? "?"} GB`),
        node("p", "field-note",
          `Detected: near expiry ${states.near_expiry || 0} · low volume ${states.low_volume || 0} · expired ${states.expired || 0} · ended ${states.ended || 0}`));
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
    target.replaceChildren(node("p", "field-note", "Refreshing Live Monitor before re-evaluation…"));
    try {
      const response = await fetch("/api/monitor/refresh", {
        method: "POST", credentials: "same-origin", cache: "no-store"
      });
      const started = await response.json().catch(() => null);
      if (!response.ok || !started?.job_id) throw new Error(started?.error || `HTTP ${response.status}`);
      for (let attempt = 0; attempt < 60; attempt++) {
        const status = await json(`/api/monitor/job/${encodeURIComponent(started.job_id)}`);
        if (status.job?.state === "done") {
          await loadAudience();
          return;
        }
        if (status.job?.state === "error") throw new Error(status.job.error || "Monitor refresh failed");
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
      throw new Error("Monitor refresh is still running. Re-evaluate after it finishes.");
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
      statusField("Android activity", android.authoritative ? "authoritative" : "informational"),
      field("Last successful pull", date(android.lastSuccessfulPullAt)),
      field("Last validate", `${date(android.lastValidateAt)} / ${fmt(android.lastValidateResult)}`)
    );
    if (reports) container.append(
      field("Carrier reports", reports.total),
      statusField("Carrier delivered", reports.delivered),
      statusField("Carrier failed", reports.failed),
      field("DLR duplicates / conflicts", `${fmt(reports.duplicates)} / ${fmt(reports.conflicts)}`),
      field("Unknown requests", reports.unknownRequests),
      field("Last carrier report", date(reports.lastReportAt))
    );
    if (outbox) {
      const outboxState = !outbox.validConfig ? "not_configured"
        : Number(outbox.dead_letter || 0) > 0 ? "degraded" : "healthy";
      container.append(
        statusField("Callback outbox", outboxState),
        field("Callback pending / retry", `${fmt(outbox.pending)} / ${fmt(outbox.retry_wait)}`),
        field("Callback delivering", outbox.delivering),
        field("Callback delivered", outbox.delivered),
        statusField("Callback dead letter", outbox.dead_letter),
        field("Oldest callback age", outbox.oldest_pending_age_ms == null ? null : `${outbox.oldest_pending_age_ms} ms`),
        field("Last callback success", date(outbox.last_success_at)),
        field("Last callback failure", date(outbox.last_failure_at))
      );
    }
  }

  async function loadHealth() {
    try {
      const [report, evidenceHealth] = await Promise.all([
        json("/api/sms/transport-health"),
        json("/api/sms/evidence-health").catch(() => null)
      ]);
      const health = report.health || {};
      const transport = health.transport || {};
      const device = health.device || {};
      const queue = health.queue || {};
      const callbackOutbox = health.diagnostics?.callbackOutbox;
      const callbackProblem = evidenceHealth?.state === "degraded"
        || evidenceHealth?.state === "not_configured"
        || (callbackOutbox && (!callbackOutbox.validConfig || Number(callbackOutbox.dead_letter || 0) > 0));
      const fullyReady = report.success && health.gmweb?.ready && !callbackProblem;
      setKpi("sms-center-health", fullyReady
        ? "Ready"
        : report.success
          ? `Degraded · ${evidenceHealth?.state === "degraded" ? `${evidenceHealth.missing_after_grace} callback(s) missing` : health.gmweb?.reason || evidenceHealth?.state || "evidence pipeline needs attention"}`
          : `${report.probe_state || "Unavailable"} · ${report.diagnostic || "No diagnostic"}`,
      fullyReady ? "success" : "warning");
      setKpi("sms-center-device", report.success
        ? `${device.state || "unreported"}${device.reason ? ` · ${device.reason}` : ""}` : "Transport health unavailable",
      report.success ? statusTone(device.state) : "neutral");
      setKpi("sms-center-queue", report.success ? fmt(queue.pending) : "Transport health unavailable",
        report.success ? "neutral" : "neutral");
      const facts = $("sms-center-gateway-facts");
      facts.replaceChildren(
        field("Probe", report.probe_state), field("Active transport", transport.active),
        field("Mode", transport.mode), field("State", transport.state),
        field("Device last seen", date(device.last_seen_at)),
        field("Pending / inflight", report.success ? `${fmt(queue.pending)} / ${fmt(queue.inflight)}` : "—"),
        field("Last ACK", date(health.last_ack?.at)), field("ACK outcome", health.last_ack?.outcome)
      );
      appendDiagnosticFacts(facts, health.diagnostics || {});
      if (evidenceHealth) facts.append(
        statusField("EVE callback evidence", evidenceHealth.state),
        field("Expected / received (24h)", `${fmt(evidenceHealth.expected_after_grace)} / ${fmt(evidenceHealth.with_signed_callback)}`),
        field("Missing after grace", evidenceHealth.missing_after_grace),
        field("Last EVE callback", date(evidenceHealth.last_callback_received_at))
      );
    } catch (error) {
      const missingScope = error.message.includes("scope_denied") || error.message.includes("transport:read");
      setKpi("sms-center-health", `Unavailable · ${error.message}`, "danger");
      setKpi("sms-center-device", missingScope ? "Requires transport:read" : "Health probe unavailable", "neutral");
      setKpi("sms-center-queue", missingScope ? "Requires transport:read" : "Queue unavailable", "neutral");
      showError($("sms-center-gateway-facts"), error);
    }
  }
  async function loadOverview() {
    try {
      const evidence = await json("/api/sms/overview");
      $("sms-center-delivered").textContent = evidence.evidence_available
        ? `${Number(evidence.carrier_delivered || 0).toLocaleString()} delivered / ${Number(evidence.carrier_failed || 0).toLocaleString()} failed / ${Number(evidence.carrier_pending || 0).toLocaleString()} pending / ${Number(evidence.carrier_not_exposed || 0).toLocaleString()} DLR not exposed`
        : "No signed callback evidence today";
    } catch (error) { $("sms-center-delivered").textContent = `Unavailable · ${error.message}`; }
    try {
      const data = await json("/api/sms/logs?limit=1000");
      const logs = data.logs || [];
      $("sms-center-attempts").textContent = logs.length.toLocaleString();
      $("sms-center-accepted").textContent = logs.filter((row) => row.request_id).length.toLocaleString();
      $("sms-center-failed").textContent = logs.filter((row) => row.status === "failed").length.toLocaleString();
    } catch (error) {
      for (const id of ["sms-center-attempts", "sms-center-accepted", "sms-center-failed"])
        $(id).textContent = `Unavailable · ${error.message}`;
    }
  }
  async function loadRuns() {
    const target = $("sms-center-run-list");
    try {
      const data = await json("/api/sms/scan/runs?limit=50");
      target.replaceChildren();
      if (!data.runs?.length) { target.append(node("p", "field-note", "No runs recorded.")); return; }
      for (const run of data.runs) {
        const disclosure = expandableRow([
          field("Run", run.run_id), field("Started", date(run.started_at)),
          field("Targets", run.matched_count), field("Submitted", run.submitted_count),
          field("Skipped / deferred", (run.suppressed_count || 0) + (run.deferred_count || 0))
        ], "View recipient decisions");
        let loaded = false;
        disclosure.button.addEventListener("click", async () => {
          const opening = disclosure.panel.classList.contains("hidden");
          toggleExpansion(disclosure.button, disclosure.panel, opening);
          if (!opening || loaded) return;
          disclosure.panel.replaceChildren(node("p", "field-note", "Loading recipient decisions…"));
          try {
            const result = await json(`/api/sms/scan/runs/${encodeURIComponent(run.run_id)}/decisions?limit=100`);
            const decisions = result.decisions || [];
            const list = node("div", "sms-center-detail-list");
            if (!decisions.length) list.append(node("p", "field-note", "No recipient decisions were recorded for this run."));
            decisions.forEach((decision, index) => {
              const item = detailRow([
                field("Account", decision.client_email || decision.service_key || "Account"),
                statusField("Decision", decision.disposition || "unrecorded"),
                field("Reason", decision.reason_code || "No reason code"),
                field("Recipient", decision.recipient_masked),
                field("Decided", date(decision.decision_at))
              ]);
              if (index >= inlineDecisionLimit) item.classList.add("sms-center-overflow", "hidden");
              list.append(item);
            });
            disclosure.panel.replaceChildren(list);
            if (decisions.length > inlineDecisionLimit) {
              const toggle = node("button", "btn btn-outline sms-center-more", `Show all ${result.total || decisions.length} decisions`);
              toggle.type = "button";
              toggle.addEventListener("click", () => {
                const hidden = list.querySelector(".sms-center-overflow.hidden");
                list.querySelectorAll(".sms-center-overflow").forEach((item) => item.classList.toggle("hidden", !hidden));
                toggle.textContent = hidden ? "Show fewer decisions" : `Show all ${result.total || decisions.length} decisions`;
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
    if (from) params.set("from", new Date(`${from}T00:00:00`).toISOString());
    if (through) {
      const end = new Date(`${through}T00:00:00`);
      end.setDate(end.getDate() + 1);
      params.set("to", end.toISOString());
    }
    try {
      const data = await json(`/api/sms/logs?${params}`);
      total = data.total || 0;
      visibleLogs = data.logs || [];
      target.replaceChildren();
      if (!data.logs?.length) target.append(node("p", "field-note", "No matching messages."));
      for (const log of data.logs || []) {
        const gatewayStatus = log.gateway_state || (log.request_id ? "accepted / pending" : "not submitted");
        const disclosure = expandableRow([
          field("Account", log.email), field("Phone", log.recipient),
          field("Trigger", log.state), statusField("EVE", log.status),
          statusField("Submission", gatewayStatus), statusField("Carrier", log.carrier_state || "not_exposed"),
          field("Last event", date(log.updated_at)),
          field("Reason", log.reason)
        ], "View delivery timeline");
        let loaded = false;
        disclosure.button.addEventListener("click", async () => {
          const opening = disclosure.panel.classList.contains("hidden");
          toggleExpansion(disclosure.button, disclosure.panel, opening);
          if (!opening || loaded) return;
          disclosure.panel.replaceChildren(node("p", "field-note", "Loading delivery timeline…"));
          try {
            const result = await json(`/api/sms/messages/${log.id}/timeline`);
            const timeline = node("div", "sms-center-timeline");
            const events = result.gateway_events || [];
            timeline.append(detailRow([
              statusField("Gateway submission", result.submission?.state || "unknown"),
              field("Submission evidence", evidenceLabel(result.submission?.evidence)),
              statusField("Carrier outcome", result.carrier?.state || "not_exposed"),
              field("Carrier evidence", evidenceLabel(result.carrier?.evidence))
            ]));
            if (!events.length) {
              const note = noCallbackMessage(result);
              timeline.append(node("p", note.tone, note.text));
            }
            for (const event of events) timeline.append(detailRow([
              statusField("Event", event.type), field("Occurred", date(event.occurred_at)),
              field("Attempt", event.attempt), field("Device", event.device_id || "Not reported"),
              field("Reason", event.reason_code)
            ]));
            const identity = detailRow([
              field("EVE message", log.id), field("Request", log.request_id),
              field("Gateway request", log.gateway_request_id), field("Gateway job", log.gateway_job_id),
              field("Notification", log.eve_notification_id), field("Correlation", log.correlation_id)
            ]);
            identity.classList.add("sms-center-identity");
            disclosure.panel.replaceChildren(identity, timeline);
            loaded = true;
          } catch (error) { showError(disclosure.panel, error); }
        });
        target.append(disclosure.entry);
      }
      $("sms-center-page-label").textContent = `${total ? offset + 1 : 0}–${Math.min(total, offset + pageSize)} of ${total}`;
      $("sms-center-prev").disabled = offset === 0;
      $("sms-center-next").disabled = offset + pageSize >= total;
    } catch (error) { showError(target, error); }
  }
  async function loadDebt() {
    const target = $("sms-center-debt-list");
    try {
      const data = await json("/api/sms/notification-debt?limit=100");
      $("sms-center-debt-count").textContent = fmt(data.active);
      target.replaceChildren();
      if (!data.obligations?.length) target.append(node("p", "field-note", "No outstanding obligations in this page."));
      for (const item of data.obligations || []) {
        const row = node("div", "sms-center-row");
        row.append(field("Account", item.account || item.service_key),
          field("Notification", item.notification_kind), statusField("State", item.status),
          field("Attempts", item.attempt_count), field("Reason", item.last_error || item.reason_code),
          field("Next retry", date(item.next_attempt_at)));
        target.append(row);
      }
    } catch (error) {
      $("sms-center-debt-count").textContent = "Unavailable";
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
    target.replaceChildren(node("p", "field-note", "Checking GMweb delivery evidence..."));
    try {
      const result = await json(`/api/sms/delivery-events?${params}`);
      if (!result.available) {
        target.replaceChildren(node("p", "field-note field-note-warn", "Contract v5 delivery search is unavailable on the configured GMweb deployment."));
        return;
      }
      const comparison = result.comparison || {};
      const list = node("div", "sms-center-detail-list");
      list.append(detailRow([
        field("Remote events", result.events?.length || 0),
        field("Matched callbacks", comparison.matched || 0),
        statusField("Missing locally", comparison.remote_only_event_ids?.length || 0),
        field("Local only", comparison.local_only_event_ids?.length || 0),
        field("Authority", comparison.authoritative_source),
        field("Local mutations", comparison.mutated_local_events)
      ]));
      for (const event of result.events || []) list.append(detailRow([
        statusField("Carrier", event.status), field("Event", event.eventId),
        field("Request", event.requestId), field("Gateway request", event.gatewayRequestId),
        field("Occurred", date(event.occurredAt)), statusField("Callback", event.callbackState)
      ]));
      if (!result.events?.length) list.append(node("p", "field-note", "No matching carrier delivery events."));
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
    const tasks = [loadHealth()];
    if (tabLoaders[selectedTab]) tasks.push(tabLoaders[selectedTab]());
    await Promise.allSettled(tasks);
  }
  document.querySelectorAll("[data-sms-tab]").forEach((button) =>
    button.addEventListener("click", () => selectTab(button.dataset.smsTab)));
  $("sms-center-refresh").addEventListener("click", refresh);
  $("sms-center-audience-refresh").addEventListener("click", refreshAudienceSource);
  let audienceSearchTimer = null;
  $("sms-center-audience-search").addEventListener("input", () => {
    clearTimeout(audienceSearchTimer);
    audienceSearchTimer = setTimeout(() => loadAudience(), 350);
  });
  $("sms-center-audience-decision").addEventListener("change", () => loadAudience());
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
    const blob = new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "eve-sms-delivery-page.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 0);
  });
  selectTab(location.hash.slice(1) || "overview", false);
  refresh();
  setInterval(() => {
    if (document.hidden) return;
    loadHealth();
  }, 30000);
})();