// UI contracts only: browser HTTP fixtures do not replace the real-Postgres integration tests.
// Uses Playwright from the developer's environment; no frontend build or runtime dependency.
const { test, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { createServer } = require("node:http");
const { readFile } = require("node:fs/promises");
const { resolve } = require("node:path");
const { chromium } = require("playwright");

let browser, server, origin;
before(async () => {
  const files = new Map(
    await Promise.all(
      [
        ["/", "index.html", "text/html"],
        ["/static/console.css", "console.css", "text/css"],
        ["/static/console.js", "console.js", "application/javascript"],
      ].map(async ([url, file, type]) => [
        url,
        {
          body: await readFile(resolve(__dirname, "../../app/static", file)),
          type,
        },
      ]),
    ),
  );
  server = createServer((request, response) => {
    const file = files.get(request.url);
    response.writeHead(file ? 200 : 404, {
      "Content-Type": file?.type || "text/plain",
    });
    response.end(file?.body || "Not found");
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({
    headless: true,
    ...(process.env.BROWSER_CHANNEL
      ? { channel: process.env.BROWSER_CHANNEL }
      : {}),
  });
});
after(async () => {
  await browser?.close();
  await new Promise((done) => (server ? server.close(done) : done()));
});

function run(id, status = "completed") {
  return {
    run_id: id,
    ticket_id: `ticket_${id}`,
    customer_id: "customer_test",
    status,
    model: "scripted-test",
    prompt_version: "support_agent_v2",
    outcome: status === "completed" ? "refund_issued" : null,
    final_response: status === "completed" ? "Your refund was issued." : null,
    error: null,
    started_at: "2026-10-09T10:00:00Z",
    completed_at: null,
    quality: { score: 1, checks: { policy_checked: true } },
    workflow_path: ["agent", "gate", "execute", "finalize"],
  };
}
function trace(row) {
  return {
    run_id: row.run_id,
    status: row.status,
    model: row.model,
    prompt_version: row.prompt_version,
    workflow_path: row.workflow_path,
    summary: {
      llm_calls: 2,
      tool_calls: 1,
      tool_retries: 0,
      errors: 0,
      input_tokens: 240,
      output_tokens: 80,
      cost_usd: "0.001234",
      latency_ms: 900,
    },
    events: [
      {
        seq: 1,
        kind: "tool_call",
        name: "issue_refund",
        input: { amount: "349.00" },
        output: { created: true },
        error: null,
        attempts: 1,
        latency_ms: 25,
      },
    ],
  };
}
function approval() {
  return {
    approval_id: "approval_test",
    run_id: "run_review",
    action: "issue_refund",
    status: "pending",
    priority: "high",
    reason: "Amount exceeds the automatic refund threshold.",
    created_at: "2026-10-09T10:00:00Z",
    payload: { order_id: "ORD-TEST", amount: "349.00", reason: "damaged_item" },
    context: {
      customer: { name: "Test Customer", customer_id: "customer_test" },
      order: {
        order_id: "ORD-TEST",
        item: "Espresso machine",
        total: "349.00",
        currency: "USD",
        status: "delivered",
      },
    },
    policy_citation: {
      verified: true,
      title: "Refund policy",
      section: "Damaged items",
      excerpt: "Eligible within the return window.",
    },
  };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
async function workspace(t, options = {}) {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
  });
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const store = {
    runs: [
      run("run_done"),
      run("run_review", "awaiting_approval"),
      run("run_escalated", "escalated"),
    ],
    approvals: [approval()],
    writes: [],
    key: null,
    failReads: false,
    ...options,
  };
  await page.route(
    /\/(agent\/runs|approvals|traces)(\/|\?|$)/,
    async (route) => {
      const request = route.request(),
        url = new URL(request.url()),
        path = url.pathname;
      const send = (body, status = 200) =>
        route.fulfill({ status, json: body });
      if (store.key && request.headers()["x-api-key"] !== store.key)
        return send({ detail: "Invalid key" }, 401);
      if (request.method() === "POST") {
        store.writes.push({
          path,
          body: request.postDataJSON(),
          key: request.headers()["x-api-key"],
        });
        if (store.onWrite) return store.onWrite(path, send);
        if (path.startsWith("/approvals/")) {
          store.approvals = [];
          store.runs[1] = run("run_review");
          return send({ approval: { status: "approved" }, run: store.runs[1] });
        }
        const created = run("run_new", "running");
        store.runs.unshift(created);
        return send(created, 202);
      }
      if (store.failReads)
        return send({ detail: "Database temporarily unavailable" }, 503);
      if (store.onRead && (await store.onRead(path, send))) return;
      if (path === "/agent/runs") return send(store.runs);
      if (path === "/approvals") return send(store.approvals);
      const row = store.runs.find(
        (item) => item.run_id === decodeURIComponent(path.split("/").at(-1)),
      );
      if (!row) return send({ message: "No such run" }, 404);
      return send(path.startsWith("/traces/") ? trace(row) : row);
    },
  );
  await page.goto(origin);
  return { page, store, errors };
}
async function connect(
  page,
  { key = "", personal = false, reviewer = "Dana" } = {},
) {
  await page
    .getByRole("button", { name: "Connection settings", exact: true })
    .click();
  await page.getByLabel("API key", { exact: true }).fill(key);
  await page
    .getByLabel("This is my personal reviewer key")
    .setChecked(personal);
  if (!personal)
    await page.getByLabel("Reviewer name", { exact: true }).fill(reviewer);
  await page.getByRole("button", { name: "Connect workspace" }).click();
}
async function textIs(page, selector, value) {
  await page.locator(selector).filter({ hasText: value }).waitFor();
}

test("dashboard filters and trace render untrusted text without executing it", async (t) => {
  const { page, store, errors } = await workspace(t);
  await textIs(page, "#metric-total", "3");
  await page.getByRole("searchbox").fill("no-match");
  await textIs(page, "#runs", "No runs match");
  await page.getByRole("searchbox").fill("");
  await page.getByLabel("Filter by status").selectOption("completed");
  assert.equal(await page.locator("#runs tr").count(), 1);
  store.runs[0].final_response = '<img src=x onerror="window.injection=true">';
  await page.getByRole("button", { name: "run_done", exact: true }).click();
  await textIs(page, ".trace-response", "<img src=x");
  assert.equal(await page.locator(".trace-response img").count(), 0);
  assert.equal(await page.evaluate(() => window.injection), undefined);
  await page.locator(".trace-event summary").click();
  await textIs(page, ".event-data", "349.00");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download JSON" }).click();
  assert.equal((await download).suggestedFilename(), "run_done-trace.json");
  assert.deepEqual(errors, []);
});

test("authentication and refresh errors are visible and recoverable", async (t) => {
  const { page, store, errors } = await workspace(t, { key: "review-key" });
  await textIs(page, "#notice", "API key is required or invalid");
  await connect(page, { key: "review-key", personal: true });
  await textIs(page, "#connection-state", "Workspace connected");
  assert.equal(
    await page.evaluate(() => localStorage.length + sessionStorage.length),
    0,
  );
  store.failReads = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await textIs(page, "#notice", "Database temporarily unavailable");
  await textIs(page, "#last-updated", "showing last loaded data");
  store.failReads = false;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await page.locator("#notice").waitFor({ state: "hidden" });
  assert.deepEqual(errors, []);
});

test("ticket creation submits once and follows a running request to completion", async (t) => {
  const gate = deferred(),
    entered = deferred();
  const { page, store, errors } = await workspace(t, {
    onWrite: async (path, send) => {
      entered.resolve();
      await gate.promise;
      const row = run("run_new", "running");
      store.runs.unshift(row);
      return send(row, 202);
    },
  });
  await page.getByRole("button", { name: "New ticket", exact: true }).click();
  await page
    .getByLabel("Customer email", { exact: true })
    .fill("test@example.com");
  await page
    .getByLabel("Customer message", { exact: true })
    .fill("Please refund ORD-TEST.");
  await page.getByRole("button", { name: "Start agent run" }).click();
  await entered.promise;
  await page.locator("#ticket-form").evaluate((form) => form.requestSubmit());
  assert.equal(store.writes.length, 1);
  gate.resolve();
  await textIs(page, ".trace-response", "The agent is working");
  store.runs[0] = run("run_new");
  await textIs(page, ".trace-response", "Your refund was issued.");
  assert.deepEqual(store.writes[0].body, {
    customer_email: "test@example.com",
    message: "Please refund ORD-TEST.",
  });
  assert.deepEqual(errors, []);
});

test("approval includes context and note and cannot double submit", async (t) => {
  const gate = deferred(),
    entered = deferred();
  const { page, store } = await workspace(t, {
    onWrite: async (path, send) => {
      entered.resolve();
      await gate.promise;
      store.approvals = [];
      store.runs[1] = run("run_review");
      return send({ approval: { status: "approved" }, run: store.runs[1] });
    },
  });
  await connect(page);
  await page.getByRole("button", { name: "✓ Approve", exact: true }).click();
  await textIs(page, "#decision-context", "349.00 USD");
  await textIs(page, "#decision-context", "Refund policy");
  await page.getByLabel("Decision note").fill("Damage evidence reviewed.");
  await page.getByRole("button", { name: "Approve & resume" }).click();
  await entered.promise;
  await page.locator("#decision-form").evaluate((form) => form.requestSubmit());
  assert.equal(store.writes.length, 1);
  assert.deepEqual(store.writes[0].body, {
    reviewer: "Dana",
    note: "Damage evidence reviewed.",
  });
  gate.resolve();
  await textIs(page, ".trace-response", "Your refund was issued.");
  assert.equal(await page.locator("#decision-dialog").isVisible(), false);
});

test("personal reviewer rejection omits the caller-supplied name and displays conflicts", async (t) => {
  const { page, store, errors } = await workspace(t, {
    key: "personal-key",
    onWrite: (path, send) =>
      send({ message: "Approval was already decided." }, 409),
  });
  await connect(page, { key: "personal-key", personal: true });
  await page.getByRole("button", { name: "Reject", exact: true }).click();
  await page.getByRole("button", { name: "Reject & resume" }).click();
  await textIs(page, "#decision-error", "Approval was already decided");
  assert.equal(store.writes[0].path, "/approvals/approval_test/reject");
  assert.equal(store.writes[0].key, "personal-key");
  assert.equal("reviewer" in store.writes[0].body, false);
  assert.equal(
    await page.getByRole("button", { name: "Reject & resume" }).isEnabled(),
    true,
  );
  assert.deepEqual(errors, []);
});

test("validation errors preserve the ticket form and allow correction", async (t) => {
  const { page, errors } = await workspace(t, {
    onWrite: (path, send) =>
      send(
        {
          detail: [
            { loc: ["body", "customer_email"], msg: "Customer not found" },
          ],
        },
        422,
      ),
  });
  await page.getByRole("button", { name: "New ticket", exact: true }).click();
  await page
    .getByLabel("Customer email", { exact: true })
    .fill("test@example.com");
  await page
    .getByLabel("Customer message", { exact: true })
    .fill("Refund please.");
  await page.getByRole("button", { name: "Start agent run" }).click();
  await textIs(page, "#ticket-error", "customer_email: Customer not found");
  assert.equal(
    await page.getByLabel("Customer message", { exact: true }).inputValue(),
    "Refund please.",
  );
  assert.equal(
    await page.getByRole("button", { name: "Start agent run" }).isEnabled(),
    true,
  );
  assert.deepEqual(errors, []);
});

test("desktop and mobile fit the viewport and dialogs support Escape", async (t) => {
  const { page, errors } = await workspace(t);
  await textIs(page, "#metric-total", "3");
  for (const width of [1440, 768, 375]) {
    await page.setViewportSize({ width, height: 1000 });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
      true,
      `overflow at ${width}px`,
    );
    await page.getByRole("button", { name: "New ticket", exact: true }).click();
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#ticket-dialog").isVisible(), false);
    assert.equal(
      await page.evaluate(() => document.activeElement.id),
      "new-ticket",
    );
  }
  assert.deepEqual(errors, []);
});

test("an older trace response cannot overwrite a more recent selection", async (t) => {
  const gate = deferred(),
    entered = deferred();
  const { page, store } = await workspace(t, {
    onRead: async (path, send) => {
      if (path !== "/traces/run_done") return false;
      entered.resolve();
      await gate.promise;
      await send(trace(store.runs[0]));
      return true;
    },
  });
  await page.getByRole("button", { name: "run_done", exact: true }).click();
  await entered.promise;
  await page.getByLabel("Open a run", { exact: true }).fill("run_review");
  await page.getByRole("button", { name: "Load trace", exact: true }).click();
  await textIs(page, ".trace-title", "run_review");
  gate.resolve();
  await page.waitForResponse((response) =>
    response.url().endsWith("/traces/run_done"),
  );
  await textIs(page, ".trace-title", "run_review");
});
