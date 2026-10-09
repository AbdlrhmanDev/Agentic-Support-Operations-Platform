# Requirements audit — 2026-10-09

**The project is not fully complete against every PRD and repository requirement.**
The core support MVP is implemented: a stateful graph, typed business tools,
PostgreSQL checkpoints, refunds and replacements, human review, policy retrieval,
scenario evaluation, and inspectable traces. The updated web console is integrated
with that implementation. Several safety, recovery, and observability requirements
remain partial, as detailed below. Passing tests is not proof that every requirement
is covered.

Scope: [PRD](../02_agentic_support_ops_PRD.md), the supplied repository instructions,
source inspection, existing evaluation reports, and local verification. This change
adds the UI and documents gaps; it does not change prompts, business rules, tools,
or graph behavior.

## Functional requirements

| Requirement | Status | Evidence and remaining work |
| --- | --- | --- |
| §6 required tools; §12 at least six typed tools | Implemented | All nine named PRD tools plus `list_customer_orders` and `update_customer_email` are registered: 11 tools. [Registry](../app/tools/registry.py), [contract tests](../tests/unit/test_contracts.py). |
| §7.1 explicit workflow and conditional branches | Implemented | [Graph](../app/agent/graph.py) has agent, gate, execution, approval, escalation and finalization nodes. |
| §7.1 bounded retries and timeouts | Implemented, with limitations | [Executor](../app/tools/executor.py) uses configured timeouts/backoff, and does not retry domain errors. Its retryable set includes all `DBAPIError` instances, broader than just transient failures. |
| §7.1 write idempotency | Partial | Refunds/replacements use deterministic keys and database constraints. Ticket notes suppress only a consecutive identical update, and email updates compare current state; not every write uses a durable run/tool/arguments key. See [write tools](../app/tools/write.py), [tickets](../app/services/tickets.py), [customers](../app/services/customers.py). |
| §7.1 persistence and approval resume | Implemented for approval pauses | PostgreSQL checkpointer and approve/reject resume tests exist. In-flight background tasks lack crash recovery; see gap 4. [Runner](../app/agent/runner.py), [flow tests](../tests/integration/test_agent_flows.py). |
| §7.2 refunds above threshold and account changes | Implemented | Decisions are enforced in services, and approval identity/action/payload are checked again at the write boundary. [Authorization](../app/services/authorization.py), [refunds](../app/services/refunds.py). |
| §7.2 uncertain policy decisions | Partial | Uncovered refund reasons and explicitly requested exceptions can pause for approval. There is no general deterministic gate for every uncertain policy decision; ambiguous cases can escalate without an approval request. |
| §7.2 repeated failures require approval | Gap | The graph performs a forced escalation and finalizes after repeated tool failures. It does not create an approval request and interrupt. This behavior is explicitly tested, but conflicts with the PRD and repository safety rule 7. |
| §7.2 approval UI context | Implemented UI; backend evidence partial | The console displays action, reason, customer/order, amount/currency, full payload, agent justification and policy excerpt. Missing/unverified citations are visibly identified. The backend still permits a pending approval with no verified citation. |
| §7.3 read/write separation, allowlists, ORM-only tools, code-enforced caps | Implemented | Tool kinds, registry and service-level limits exist. Unknown arguments are rejected; ownership and abuse tests exist. |
| §7.3 schema validation / repository no-coercion rule | Partial | Pydantic schemas use `extra="forbid"`. `ToolArgs` does not enable strict validation, and decimal fields accept numeric values as well as strings. A blanket claim of non-coercing validation is unsupported. [Tool arguments](../app/tools/base.py). |
| §7.4 persisted state | Implemented | Conversation, scope, policy evidence, tool results, approvals, step/path and outcome are checkpointed. Order IDs are retained in tool arguments/results and actions rather than a dedicated top-level `RunState.order_id`. [State](../app/agent/state.py). |
| §7.5 100 scenarios in the required categories | Implemented | Ten scenarios in each of ten categories, with expected backend state and tool behavior. [Catalog](../evals/scenarios/catalog.py), [graders](../evals/grading.py). |
| §7.5 all specified evaluation metrics | Implemented; evidence limited | Both existing real-model reports include the specified metrics. No held-out evaluation or repeated-run variance measurement. The second report is in-sample. |
| §7.6 run/model/prompt/path/tools/retries/tokens/latency/status | Implemented | Stored trace events and run state are accessible through the API and new trace explorer. Trace summary latency is summed model/tool time, not wall-clock time including a human wait. [Queries](../app/observability/queries.py). |
| §7.6 evaluator scores on each run | Partial | CLI suite reports contain per-scenario scores linked by `run_id`. The HTTP evaluation endpoint persists the aggregate summary only. Run/trace responses show rule-based quality, not scenario evaluator scores. |
| §10 specified API routes | Implemented | All six required routes exist, with extra run/approval lists, email intake and evaluation polling. [API](../app/api/). |
| §11 domain objects | Implemented | Orders, tickets, refunds, approvals and runs are migrated PostgreSQL models. [Models](../app/db/models.py), [migrations](../migrations/versions/). |
| §12 five distinct support workflows | Implemented | Refund, policy refusal, lost-shipment replacement, duplicate-charge refund, approval/rejection, account change and escalation paths exist in scenarios and tests. |
| §12 injection/tool-abuse tests | Implemented | Hostile model calls, cross-customer access, invalid arguments, hard caps and prompt leakage are covered by tests/scenarios. This is scoped evidence, not a universal security guarantee. |
| §12 viewable full run traces | Implemented | Trace explorer includes run outcome, workflow, tokens/cost, retries, expandable event inputs/outputs and JSON download. |
| §12 Docker local deployment | Implemented; local startup/read smoke verified | Compose rebuild/start, PostgreSQL health, console assets and live trace reads passed. A new paid-model workflow was not rerun in Docker for this UI-only change; scripted end-to-end workflows passed against real PostgreSQL in Pytest. |

The PRD's React/Next.js/Streamlit and Redis entries are **suggested** technologies.
The dependency-free HTML/CSS/JavaScript console and PostgreSQL locking fit the
repository's architecture; their use is not a missing functional requirement.

## Remaining gaps, in priority order

1. **Approval coverage differs from the written requirements.** Add a persisted
   approval/interrupt path for repeated tool failures and define how uncertain
   policy decisions are detected in code. Preserve all existing financial gates.
   Require appropriate policy evidence where applicable instead of presenting a
   missing citation as a verified decision.
2. **PII/payment redaction is incomplete.** The
   [recorder](../app/observability/recorder.py) masks email addresses in input/output
   values only. Names, phone numbers, addresses, payment-like text and exception
   messages are not comprehensively scrubbed. The runner also logs exceptions.
   Repository instructions require broader redaction before traces/logs are stored.
3. **Durable idempotency and strict input contracts are not uniform.** Use durable
   write identities for ticket/account mutations, including retries after
   intervening writes; explicitly define accepted JSON money representations.
   Keep refund amounts as `Decimal` and do not weaken approval checks.
4. **Background runs can remain `running` after a process crash.**
   `run_in_background` uses in-process tasks. There is no durable worker/recovery
   loop or operator retry endpoint for abandoned runs. Approval checkpoints do
   support normal pause/resume, which is a different capability.
5. **Observability has limits.** Embedding cost is not included. OpenTelemetry starts
   a new root span on each advance/resume rather than persisting one trace context
   across approval interruptions. Evaluator results are not exposed on individual
   run/trace API responses. The default OTel configuration drops spans while the
   database trace remains available.
6. **Operational hardening remains.** Authentication is shared/personal API keys,
   with no accounts, roles or key rotation. Escalated tickets have no human take-back
   endpoint. These are readiness limitations; they are not all explicit MVP items.
7. **Evaluation generalization is unmeasured.** Add held-out cases and repeat runs.
   The checked-in README numbers describe two existing runs, not a new measurement
   performed during this UI change.

## Stretch goals (§13)

| Goal | Status |
| --- | --- |
| Multi-agent specialization | Not implemented; conditional stretch goal, not required for this manageable single graph. |
| Email/chat integration | Inbound email intake implemented; outbound replies, thread continuation and chat intake are missing. |
| Queue prioritization | Implemented by urgency, amount and waiting time. |
| Post-resolution quality scoring | Implemented as a fixed rule-based rubric; does not judge reply wording/tone. |
| Model routing | Implemented as optional configured rule-based routing. |
| Offline trace replay | Implemented; recorded model turns can be replayed in an isolated backend copy. |
| Policy-version-aware decisions | Not implemented end to end; decisions do not pin/enforce a policy version. |

## Measured evidence

The local reports `gpt-6.1-sol-20261008T145859Z.json` and
`gpt-6.1-sol-20261008T161415Z.json` were inspected. They agree with the
[README results](../README.md#results). Reports are git-ignored.

| Metric | Existing prompt v1 run | Existing prompt v2 run |
| --- | --- | --- |
| Scenarios | 100 | 100 |
| Task success | 0.85 | 1.0 |
| Tool selection accuracy | 0.94 | 1.0 |
| Argument accuracy | 0.88 | 1.0 |
| Policy compliance | 0.99 | 1.0 |
| Unsafe action rate | 0.0 | 0.0 |
| Escalation precision / recall | 0.8462 / 0.6111 | 1.0 / 1.0 |
| Average tool calls | 4.24 | 4.11 |
| Latency p50 / p95 | 14,041 / 21,219 ms | 15,977 / 36,601 ms |
| Average model cost | $0.005722 | $0.005480 |

Prompt v2 was evaluated after tuning against the same suite. Its result does not
establish performance on unseen tickets. No paid model evaluation was triggered
for this UI-only change. The Python suite includes the 100-case oracle self-check;
that checks the harness/business rules and is not agent performance evidence.

Local checks for this change:

- Full Pytest suite: **149 passed**, including PostgreSQL integration tests and
  the 100-case oracle self-check.
- Browser regression suite: **8 passed** in headless Edge, using isolated HTTP
  fixtures. Covers submission guards, polling, approval notes/identity, conflicts,
  validation and connection errors, safe text rendering, trace races/download,
  keyboard dialogs, and 1440/768/375-pixel layouts.
- `node --check app/static/console.js`: passed.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed.
- `uv run mypy app evals`: passed (69 source files).
- `git diff --check`: passed.
- `docker compose up -d --build api`: passed; API started and PostgreSQL was healthy.
- Browser smoke check against the rebuilt `http://localhost:8000`: connected to
  the real API, displayed the five existing runs and empty approval queue, and
  opened an existing seven-event trace without JavaScript errors. Desktop and
  375-pixel mobile pages had no document-level horizontal overflow. This was a
  read-only smoke check; no customer action or model call was submitted.
