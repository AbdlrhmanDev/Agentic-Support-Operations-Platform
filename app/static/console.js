"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  runs: [],
  approvals: [],
  loaded: false,
  view: "overview",
  trace: null,
  run: null,
  selectedRun: null,
  refreshVersion: 0,
  traceVersion: 0,
  refreshing: false,
  decision: null,
  credentials: { key: "", reviewer: "", personal: false },
};
const label = (value) => (value ? String(value).replaceAll("_", " ") : "—");
const dateText = (value) =>
  value
    ? new Date(value).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";

// Tickets, policies and model output are untrusted. Never parse dynamic data as HTML.
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "onclick") node.addEventListener("click", value);
    else node[key] = value;
  }
  node.append(...children.filter((child) => child != null));
  return node;
}
function pill(value) {
  const known = [
    "completed",
    "approved",
    "awaiting_approval",
    "pending",
    "high",
    "failed",
    "rejected",
    "escalated",
    "urgent",
    "running",
  ];
  return el(
    "span",
    { class: `pill ${known.includes(value) ? value : ""}` },
    label(value),
  );
}
function empty(title, text, icon = "✓") {
  return el(
    "div",
    { class: "empty-state" },
    el("span", { class: "empty-icon", ariaHidden: "true" }, icon),
    el("h2", {}, title),
    el("p", {}, text),
  );
}
function notice(message = "") {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}
function announce(message) {
  $("announcement").textContent = message;
}
function connection(online) {
  $("connection-state").textContent = online
    ? "Workspace connected"
    : "Connection unavailable";
  $("connection-state").className =
    `connection-state ${online ? "connected" : "disconnected"}`;
}
async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.credentials.key) headers["X-API-Key"] = state.credentials.key;
  let response;
  try {
    response = await fetch(path, {
      headers,
      signal: AbortSignal.timeout(options.method ? 180000 : 20000),
      ...options,
    });
  } catch {
    throw new Error(
      options.method
        ? "The response was interrupted. The action may have been accepted. Refresh the workspace before trying again."
        : "Cannot reach the API. Check that the app is running, then refresh.",
    );
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(body.detail)
      ? body.detail
          .map(
            (item) =>
              `${item.loc?.slice(1).join(".") || "Request"}: ${item.msg}`,
          )
          .join("; ")
      : body.detail;
    const message =
      response.status === 401
        ? "An API key is required or invalid. Open Connection settings to connect."
        : body.message || detail || `Request failed (${response.status}).`;
    const error = new Error(
      typeof message === "string" ? message : JSON.stringify(message),
    );
    error.status = response.status;
    throw error;
  }
  return body;
}

function navigate() {
  const requested = location.hash.slice(1).split("/")[0];
  const view = ["overview", "runs", "approvals", "trace"].includes(requested)
    ? requested
    : "overview";
  state.view = view;
  const copy = {
    overview: [
      "Overview",
      "Your operations, in focus.",
      "A clear view of agent activity and the decisions that need you.",
    ],
    runs: [
      "Agent runs",
      "Every request. Every outcome.",
      "Search the latest 200 runs and follow a workflow from start to finish.",
    ],
    approvals: [
      "Approvals",
      "Your judgment makes the difference.",
      "Review the proposed action, customer context and policy before deciding.",
    ],
    trace: [
      "Trace explorer",
      "Follow the decision trail.",
      "Inspect model calls, tools, guard decisions and the final outcome.",
    ],
  }[view];
  $("breadcrumb").textContent = copy[0];
  $("page-title").textContent = copy[1];
  $("page-description").textContent = copy[2];
  document.querySelectorAll("[data-nav]").forEach((link) => {
    if (link.dataset.nav === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $("metrics").hidden = view === "trace";
  $("workspace-grid").hidden = view === "trace";
  $("workspace-grid").classList.toggle("single", view !== "overview");
  $("runs-panel").hidden = view === "approvals";
  $("approvals-panel").hidden = view === "runs";
  $("trace-panel").hidden = view !== "trace";
  if (view === "trace" && location.hash.startsWith("#trace/")) {
    let id;
    try {
      id = decodeURIComponent(location.hash.slice(7));
    } catch {
      notice("Invalid run link.");
      return;
    }
    if (id) void loadTrace(id);
  }
}
function openTrace(id) {
  const hash = `#trace/${encodeURIComponent(id)}`;
  if (location.hash === hash) void loadTrace(id);
  else location.hash = hash;
}

function renderRuns() {
  const query = $("run-search").value.trim().toLowerCase();
  const status = $("status-filter").value;
  const filtered = state.runs.filter(
    (run) =>
      (!status || run.status === status) &&
      [run.run_id, run.ticket_id, run.customer_id, run.outcome, run.model].some(
        (value) =>
          String(value || "")
            .toLowerCase()
            .includes(query),
      ),
  );
  $("run-count").textContent = String(state.runs.length);
  $("runs-caption").textContent =
    `Showing ${filtered.length} of ${state.runs.length} loaded runs`;
  $("runs").replaceChildren(
    ...(filtered.length
      ? filtered.map((run) =>
          el(
            "tr",
            {},
            el(
              "td",
              {},
              el(
                "button",
                {
                  class: "run-link",
                  title: run.run_id,
                  onclick: () => openTrace(run.run_id),
                },
                run.run_id,
              ),
              el("small", {}, run.ticket_id),
            ),
            el("td", {}, pill(run.status)),
            el("td", {}, label(run.outcome)),
            el("td", {}, run.model, el("small", {}, run.prompt_version)),
            el(
              "td",
              {},
              el(
                "time",
                {
                  dateTime: run.started_at,
                  title: new Date(run.started_at).toLocaleString(),
                },
                dateText(run.started_at),
              ),
            ),
          ),
        )
      : [
          el(
            "tr",
            {},
            el(
              "td",
              { colSpan: 5, class: "table-empty" },
              state.runs.length
                ? "No runs match these filters."
                : "No runs yet. Start a new ticket to see the agent in action.",
            ),
          ),
        ]),
  );
}
function approvalContext(approval) {
  const ctx = approval.context || {},
    payload = approval.payload || {},
    citation = approval.policy_citation;
  const fields = [
    [
      "Customer",
      ctx.customer
        ? `${ctx.customer.name} · ${ctx.customer.customer_id}`
        : "Not supplied",
    ],
    [
      "Order",
      ctx.order
        ? `${ctx.order.order_id} · ${ctx.order.item}`
        : payload.order_id || "Not applicable",
    ],
    ["Order status", ctx.order ? label(ctx.order.status) : "—"],
    [
      "Order total",
      ctx.order ? `${ctx.order.total} ${ctx.order.currency}` : "—",
    ],
    [
      "Proposed amount",
      payload.amount != null
        ? `${payload.amount} ${ctx.order?.currency || ""}`.trim()
        : "Not specified",
    ],
  ];
  const list = el("dl", { class: "approval-context" });
  fields.forEach(([key, value]) =>
    list.append(el("dt", {}, key), el("dd", {}, value)),
  );
  const proposal = el(
    "details",
    { class: "policy" },
    el("summary", {}, "Full proposed action"),
    el("pre", {}, JSON.stringify(payload, null, 2)),
  );
  let policy;
  if (citation?.verified) {
    policy = el(
      "details",
      { class: "policy" },
      el(
        "summary",
        {},
        `${citation.title || "Policy"} · ${citation.section || "Citation"}`,
      ),
      el("p", {}, citation.excerpt || "No excerpt supplied."),
    );
  } else {
    policy = el(
      "div",
      { class: "policy unverified" },
      citation
        ? `Unverified policy citation: ${citation.cited || citation.title || "unknown"}`
        : "No policy citation provided. Review the context carefully.",
    );
  }
  return el(
    "div",
    {},
    list,
    ctx.justification
      ? el("p", { class: "reason" }, `Agent’s case: ${ctx.justification}`)
      : null,
    policy,
    proposal,
  );
}
function approvalCard(approval) {
  return el(
    "article",
    { class: "approval-card" },
    el(
      "div",
      { class: "approval-top" },
      pill(approval.priority || "pending"),
      el(
        "time",
        { dateTime: approval.created_at },
        dateText(approval.created_at),
      ),
    ),
    el("h3", {}, label(approval.action)),
    el("p", { class: "reason" }, approval.reason),
    approvalContext(approval),
    el(
      "div",
      { class: "approval-actions" },
      el(
        "button",
        {
          class: "button primary",
          onclick: () => openDecision(approval, "approve"),
        },
        "✓ Approve",
      ),
      el(
        "button",
        {
          class: "button secondary",
          onclick: () => openDecision(approval, "reject"),
        },
        "Reject",
      ),
      el(
        "button",
        { class: "button subtle", onclick: () => openTrace(approval.run_id) },
        "Trace ↗",
      ),
    ),
  );
}
async function refresh(force = false) {
  if (state.refreshing && !force) return;
  const version = ++state.refreshVersion;
  state.refreshing = true;
  $("refresh").disabled = true;
  try {
    const [runs, approvals] = await Promise.all([
      api("/agent/runs?limit=200"),
      api("/approvals?status=pending"),
    ]);
    if (version !== state.refreshVersion) return;
    state.runs = runs;
    state.approvals = approvals;
    state.loaded = true;
    renderRuns();
    $("approvals").replaceChildren(
      ...(approvals.length
        ? approvals.map(approvalCard)
        : [
            empty(
              "You’re all caught up.",
              "Actions that need a human decision will appear here.",
            ),
          ]),
    );
    for (const id of ["approval-count", "nav-approval-count", "metric-pending"])
      $(id).textContent = String(approvals.length);
    $("metric-total").textContent = String(runs.length);
    $("metric-completed").textContent = String(
      runs.filter((run) => run.status === "completed").length,
    );
    $("metric-attention").textContent = String(
      runs.filter((run) => ["escalated", "failed"].includes(run.status)).length,
    );
    $("last-updated").textContent =
      `Updated ${new Date().toLocaleTimeString()}`;
    connection(true);
    notice();
  } catch (error) {
    if (version !== state.refreshVersion) return;
    connection(false);
    notice(error.message);
    $("last-updated").textContent = state.loaded
      ? "Connection lost · showing last loaded data"
      : "Workspace data unavailable";
    if (!state.loaded) {
      $("runs").replaceChildren(
        el(
          "tr",
          {},
          el(
            "td",
            { colSpan: 5, class: "table-empty" },
            "Run history unavailable. Connect and refresh to try again.",
          ),
        ),
      );
      $("approvals").replaceChildren(
        empty(
          "Unable to load approvals",
          "Check your connection settings, then refresh.",
          "!",
        ),
      );
    }
  } finally {
    if (version === state.refreshVersion) {
      state.refreshing = false;
      $("refresh").disabled = false;
    }
  }
}

function renderTrace(run, trace) {
  const summary = trace.summary;
  const totals = el("dl", { class: "trace-metrics" });
  [
    ["Model / prompt", `${trace.model} / ${trace.prompt_version}`],
    ["Model calls", summary.llm_calls],
    ["Tool calls / retries", `${summary.tool_calls} / ${summary.tool_retries}`],
    ["Tokens · in / out", `${summary.input_tokens} / ${summary.output_tokens}`],
    [
      "Model cost",
      summary.cost_usd == null ? "Not available" : `$${summary.cost_usd}`,
    ],
    ["Latency", `${summary.latency_ms} ms`],
  ].forEach(([key, value]) =>
    totals.append(
      el("div", {}, el("dt", {}, key), el("dd", {}, String(value))),
    ),
  );
  const quality = run.quality
    ? `Rule-based quality: ${run.quality.score} · ${Object.entries(
        run.quality.checks || {},
      )
        .map(([name, passed]) => `${label(name)}: ${passed ? "pass" : "fail"}`)
        .join("; ")}`
    : "Quality is not scored yet.";
  const overview = el(
    "div",
    { class: "panel trace-overview" },
    el(
      "div",
      { class: "trace-title" },
      el("h2", {}, run.run_id),
      pill(run.status),
      pill(run.outcome),
    ),
    el(
      "p",
      { class: "field-help" },
      `Ticket ${run.ticket_id} · Customer ${run.customer_id}`,
    ),
    el(
      "p",
      { class: "trace-response" },
      run.final_response ||
        run.error ||
        (run.status === "awaiting_approval"
          ? "This run is paused for a human decision. Open Approvals to review the proposed action."
          : "The agent is working. This trace refreshes as calls complete."),
    ),
    el(
      "div",
      { class: "workflow", ariaLabel: "Workflow path" },
      ...trace.workflow_path.map((step) => el("span", {}, label(step))),
    ),
    totals,
    el("p", { class: "field-help" }, quality),
  );
  const timeline = el(
    "div",
    { class: "panel" },
    el(
      "div",
      { class: "panel-heading" },
      el(
        "div",
        {},
        el("h2", {}, "Execution timeline"),
        el(
          "p",
          {},
          `${trace.events.length} recorded events · expand an event to inspect its data`,
        ),
      ),
    ),
  );
  const expanded = new Set(
    [...$("trace-content").querySelectorAll("details[open][data-event]")].map(
      (node) => node.dataset.event,
    ),
  );
  trace.events.forEach((event, index) => {
    const details = el(
      "details",
      { class: "trace-event", open: expanded.has(String(event.seq)) },
      el(
        "summary",
        {},
        el(
          "span",
          { class: "event-index" },
          String(index + 1).padStart(2, "0"),
        ),
        pill(event.kind),
        el("span", { class: "event-name" }, event.name),
        el(
          "span",
          { class: "event-timing" },
          `${event.latency_ms ?? "—"} ms · ${event.attempts} attempt(s) ▾`,
        ),
      ),
      event.error ? el("p", { class: "event-error" }, event.error) : null,
      el(
        "div",
        { class: "event-data" },
        ...[
          ["Input", event.input],
          ["Output", event.output],
        ].map(([name, value]) =>
          el(
            "div",
            {},
            el("h3", {}, name),
            el(
              "pre",
              {},
              value == null
                ? "No data recorded"
                : JSON.stringify(value, null, 2),
            ),
          ),
        ),
      ),
    );
    details.dataset.event = String(event.seq);
    timeline.append(details);
  });
  if (!trace.events.length)
    timeline.append(
      empty(
        "Waiting for the first event",
        "The trace will update as the agent works.",
        "◷",
      ),
    );
  $("trace-content").replaceChildren(overview, timeline);
}
async function loadTrace(id, silent = false) {
  const version = ++state.traceVersion;
  const changed = id !== state.selectedRun;
  state.selectedRun = id;
  $("trace-run-id").value = id;
  if (changed || !silent) {
    state.trace = null;
    $("download-trace").disabled = true;
    $("trace-content").replaceChildren(
      el("div", { class: "panel loading" }, "Loading run and trace…"),
    );
  }
  try {
    const [run, trace] = await Promise.all([
      api(`/agent/runs/${encodeURIComponent(id)}`),
      api(`/traces/${encodeURIComponent(id)}`),
    ]);
    if (version !== state.traceVersion) return;
    state.run = run;
    state.trace = trace;
    renderTrace(run, trace);
    $("download-trace").disabled = false;
    if (!silent) announce("Run trace loaded.");
  } catch (error) {
    if (version !== state.traceVersion) return;
    state.run = null;
    state.trace = null;
    $("download-trace").disabled = true;
    $("trace-content").replaceChildren(
      el(
        "div",
        { class: "panel" },
        empty("Trace unavailable", error.message, "!"),
      ),
    );
  }
}

function setBusy(dialogId, busy) {
  const dialog = $(dialogId);
  dialog.dataset.busy = String(busy);
  dialog.querySelectorAll("button, input, textarea").forEach((control) => {
    control.disabled = busy;
  });
}
function openDecision(approval, verb) {
  state.decision = { approval, verb };
  $("decision-title").textContent =
    verb === "approve" ? "Approve this action?" : "Reject this action?";
  $("decision-context").replaceChildren(
    el("h3", {}, label(approval.action)),
    el("p", { class: "reason" }, approval.reason),
    approvalContext(approval),
  );
  $("decision-effect").textContent =
    verb === "approve"
      ? "Approval resumes the run and authorizes this proposed action, subject to server-side business rules."
      : "Rejection resumes the run without authorizing this proposed action.";
  $("decision-identity").textContent = state.credentials.personal
    ? "Reviewer identity will be verified by your personal key."
    : `Reviewer: ${state.credentials.reviewer || "not set — add your name in Connection settings"}`;
  $("decision-note").value = "";
  $("decision-error").textContent = "";
  $("submit-decision").className =
    `button ${verb === "approve" ? "primary" : "danger"}`;
  $("submit-decision").textContent =
    verb === "approve" ? "Approve & resume" : "Reject & resume";
  $("decision-dialog").showModal();
}
$("decision-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("decision-dialog").dataset.busy === "true" || !state.decision) return;
  if (!state.credentials.personal && !state.credentials.reviewer) {
    $("decision-error").textContent =
      "Set a reviewer name in Connection settings before deciding.";
    return;
  }
  const { approval, verb } = state.decision;
  const body = { note: $("decision-note").value.trim() || null };
  if (!state.credentials.personal) body.reviewer = state.credentials.reviewer;
  setBusy("decision-dialog", true);
  $("decision-error").textContent = "";
  try {
    await api(
      `/approvals/${encodeURIComponent(approval.approval_id)}/${verb}`,
      { method: "POST", body: JSON.stringify(body) },
    );
    $("decision-dialog").close();
    announce(
      `Action ${verb === "approve" ? "approved" : "rejected"}. Run resumed.`,
    );
    await refresh();
    openTrace(approval.run_id);
  } catch (error) {
    $("decision-error").textContent = error.message;
    await refresh();
  } finally {
    setBusy("decision-dialog", false);
  }
});
$("ticket-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("ticket-dialog").dataset.busy === "true") return;
  const email = $("email").value.trim(),
    message = $("message").value.trim();
  if (!message) {
    $("ticket-error").textContent = "Enter a customer message.";
    return;
  }
  setBusy("ticket-dialog", true);
  $("start").textContent = "Starting…";
  $("ticket-error").textContent = "";
  try {
    const run = await api("/agent/runs?wait=false", {
      method: "POST",
      body: JSON.stringify({ customer_email: email, message }),
    });
    $("ticket-dialog").close();
    $("ticket-form").reset();
    announce("Agent run started.");
    await refresh();
    openTrace(run.run_id);
  } catch (error) {
    $("ticket-error").textContent = error.message;
  } finally {
    setBusy("ticket-dialog", false);
    $("start").textContent = "Start agent run ↗";
  }
});
$("connection-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.credentials = {
    key: $("apikey").value.trim(),
    reviewer: $("reviewer").value.trim(),
    personal: $("reviewer-key").checked,
  };
  $("connection-caption").textContent = state.credentials.personal
    ? "Personal reviewer key"
    : state.credentials.reviewer || "Session connection";
  $("connection-dialog").close();
  ++state.traceVersion;
  state.trace = null;
  state.run = null;
  void refresh(true);
  if (state.selectedRun) void loadTrace(state.selectedRun);
});
$("reviewer-key").addEventListener("change", () => {
  $("reviewer").disabled = $("reviewer-key").checked;
});
$("open-connection").addEventListener("click", () =>
  $("connection-dialog").showModal(),
);
$("new-ticket").addEventListener("click", () => {
  $("ticket-error").textContent = "";
  $("ticket-dialog").showModal();
});
$("refresh").addEventListener("click", async () => {
  await refresh();
  if (state.view === "trace" && state.selectedRun)
    await loadTrace(state.selectedRun);
});
$("run-search").addEventListener("input", renderRuns);
$("status-filter").addEventListener("change", renderRuns);
$("trace-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const id = $("trace-run-id").value.trim();
  if (id) openTrace(id);
});
$("download-trace").addEventListener("click", () => {
  if (!state.trace) return;
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(state.trace, null, 2)], {
      type: "application/json",
    }),
  );
  const link = el("a", {
    href: url,
    download: `${state.trace.run_id.replace(/[^a-zA-Z0-9_-]/g, "_")}-trace.json`,
  });
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
const examples = {
  refund: [
    "alice@example.com",
    "My order ORD-1001 arrived damaged. Can you refund it?",
  ],
  approval: [
    "alice@example.com",
    "Order ORD-1002 arrived damaged. I'd like a refund.",
  ],
  shipping: [
    "bob@example.com",
    "My order ORD-2001 has not arrived. Can you send a replacement?",
  ],
};
document.querySelectorAll("[data-example]").forEach((button) =>
  button.addEventListener("click", () => {
    const [email, message] = examples[button.dataset.example];
    $("email").value = email;
    $("message").value = message;
  }),
);
document
  .querySelectorAll("[data-close]")
  .forEach((button) =>
    button.addEventListener("click", () => $(button.dataset.close).close()),
  );
document.querySelectorAll("dialog").forEach((dialog) =>
  dialog.addEventListener("cancel", (event) => {
    if (dialog.dataset.busy === "true") event.preventDefault();
  }),
);
window.addEventListener("hashchange", navigate);
async function poll() {
  // Do not replace focused controls, an expanded review, or a form being edited.
  if (
    !document.hidden &&
    !document.querySelector("dialog[open]") &&
    !document.activeElement?.closest(
      "#runs-panel, #approvals-panel, #trace-content",
    )
  ) {
    await refresh();
    if (
      state.view === "trace" &&
      state.selectedRun &&
      (!state.run ||
        ["running", "awaiting_approval"].includes(state.run.status))
    )
      await loadTrace(state.selectedRun, true);
  }
  setTimeout(poll, 8000);
}
navigate();
void refresh();
setTimeout(poll, 8000);
