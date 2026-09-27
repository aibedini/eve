"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const tabs = ["overview", "runs", "delivery", "debt", "gateway"];
  let offset = 0;
  let total = 0;
  let visibleLogs = [];
  const pageSize = 30;

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
  function openModal(title, entries) {
    $("sms-center-modal-title").textContent = title;
    const list = $("sms-center-timeline");
    list.replaceChildren();
    if (!entries.length) list.append(node("li", "field-note", "No recorded events yet."));
    for (const entry of entries) list.append(node("li", "", entry));
    $("sms-center-modal").classList.remove("hidden");
    $("sms-center-modal-close").focus();
  }
  function closeModal() { $("sms-center-modal").classList.add("hidden"); }

  async function loadHealth() {
    try {
      const report = await json("/api/sms/transport-health");
      const health = report.health || {};
      const transport = health.transport || {};
      const device = health.device || {};
      const queue = health.queue || {};
      $("sms-center-health").textContent = report.success
        ? (health.gmweb?.ready ? "Ready" : `Degraded · ${health.gmweb?.reason || "no reason provided"}`)
        : `${report.probe_state || "Unavailable"} · ${report.diagnostic || "No diagnostic"}`;
      $("sms-center-device").textContent = report.success
        ? `${device.state || "unreported"}${device.reason ? ` · ${device.reason}` : ""}` : "Not reachable";
      $("sms-center-queue").textContent = report.success ? fmt(queue.pending) : "—";
      const facts = $("sms-center-gateway-facts");
      facts.replaceChildren(
        field("Probe", report.probe_state), field("Active transport", transport.active),
        field("Mode", transport.mode), field("State", transport.state),
        field("Device last seen", date(device.last_seen_at)),
        field("Pending / inflight", report.success ? `${fmt(queue.pending)} / ${fmt(queue.inflight)}` : "—"),
        field("Last ACK", date(health.last_ack?.at)), field("ACK outcome", health.last_ack?.outcome)
      );
    } catch (error) {
      $("sms-center-health").textContent = `Unavailable · ${error.message}`;
      $("sms-center-device").textContent = "—";
      $("sms-center-queue").textContent = "—";
      showError($("sms-center-gateway-facts"), error);
    }
  }
  async function loadOverview() {
    try {
      const evidence = await json("/api/sms/overview");
      $("sms-center-delivered").textContent = evidence.evidence_available
        ? Number(evidence.carrier_delivered || 0).toLocaleString()
        : "No callback evidence";
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
        const row = node("button", "sms-center-row", "");
        row.type = "button";
        row.append(field("Run", run.run_id), field("Started", date(run.started_at)),
          field("Targets", run.matched_count), field("Submitted", run.submitted_count),
          field("Skipped / deferred", (run.suppressed_count || 0) + (run.deferred_count || 0)));
        row.addEventListener("click", async () => {
          try {
            const result = await json(`/api/sms/scan/runs/${encodeURIComponent(run.run_id)}/decisions?limit=100`);
            openModal(`Run ${run.run_id} · recipient decisions`, (result.decisions || []).map((decision) =>
              `${decision.client_email || decision.service_key || "Account"} · ${decision.disposition || "unrecorded"} · ${decision.reason_code || "no reason code"}`));
          } catch (error) { openModal(`Run ${run.run_id}`, [`Unable to load recipients: ${error.message}`]); }
        });
        target.append(row);
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
        const row = node("button", "sms-center-row", "");
        row.type = "button";
        row.append(field("Account", log.email), field("Phone", log.recipient),
          field("Trigger", log.state), field("EVE", log.status),
          field("GMweb", log.gateway_state || (log.request_id ? "accepted / pending" : "not submitted")),
          field("Last event", date(log.updated_at)), field("Reason", log.reason));
        row.addEventListener("click", async () => {
          try {
            const result = await json(`/api/sms/messages/${log.id}/timeline`);
            const events = (result.gateway_events || []).map((event) =>
              `${date(event.occurred_at)} · ${event.type} · attempt ${fmt(event.attempt)} · ${event.device_id || "no device"}${event.reason_code ? ` · ${event.reason_code}` : ""}`);
            openModal(`${log.email} · ${log.request_id || "not submitted"}`, events);
          } catch (error) { openModal(log.email, [`Unable to load timeline: ${error.message}`]); }
        });
        target.append(row);
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
        row.append(field("Account", item.client_email || item.service_key),
          field("Notification", item.notification_kind), field("State", item.status),
          field("Attempts", item.attempt_count), field("Reason", item.last_error || item.reason_code),
          field("Next retry", date(item.next_attempt_at)));
        target.append(row);
      }
    } catch (error) {
      $("sms-center-debt-count").textContent = "Unavailable";
      showError(target, error);
    }
  }
  function selectTab(name) {
    if (!tabs.includes(name)) return;
    for (const tab of tabs) $("sms-center-" + tab).classList.toggle("hidden", tab !== name);
    for (const button of document.querySelectorAll("[data-sms-tab]"))
      button.classList.toggle("active", button.dataset.smsTab === name);
    history.replaceState(null, "", `#${name}`);
  }
  async function refresh() {
    await Promise.allSettled([loadHealth(), loadOverview(), loadRuns(), loadDelivery(), loadDebt()]);
  }
  document.querySelectorAll("[data-sms-tab]").forEach((button) =>
    button.addEventListener("click", () => selectTab(button.dataset.smsTab)));
  $("sms-center-refresh").addEventListener("click", refresh);
  $("sms-center-filter").addEventListener("click", () => { offset = 0; loadDelivery(); });
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
  $("sms-center-modal-close").addEventListener("click", closeModal);
  $("sms-center-modal").addEventListener("click", (event) => {
    if (event.target === $("sms-center-modal")) closeModal();
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeModal(); });
  selectTab(location.hash.slice(1) || "overview");
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 30000);
})();
