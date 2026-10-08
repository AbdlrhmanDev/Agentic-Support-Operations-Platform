# PRD — Agentic Support Operations Platform

## 1. Product Summary

Build an AI operations agent that can resolve customer-support requests by reasoning over policies, calling external tools, updating business systems, requesting human approval for high-risk actions, and producing a complete trace of every decision and tool call.

The agent must do real workflow orchestration, not just answer questions.

## 2. Problem

Support teams repeatedly perform multi-step workflows: identify a customer, retrieve an order, check policy eligibility, decide the next action, update a ticket, issue or request a refund, and write a response.

A production AI agent must interact safely with tools, preserve state, recover from failures, and defer sensitive actions to humans.

## 3. Target Users

- Customer support agents.
- Operations teams.
- Team leads approving refunds/escalations.
- Engineers monitoring agent quality.

## 4. Goals

- Implement stateful agent workflows.
- Give the agent typed tools for CRM/order/ticket/refund operations.
- Add a knowledge base for policy retrieval.
- Require human approval above configurable risk thresholds.
- Persist workflow state and support resume/retry.
- Evaluate tool selection and end-to-end task success.
- Record full traces, latency, tokens, and cost.

## 5. Example Workflow

User:
"My order arrived damaged. Can you refund it?"

Agent:
1. Identifies customer/order.
2. Calls `get_order`.
3. Retrieves refund policy.
4. Checks eligibility.
5. Calculates refund amount.
6. If amount <= automatic threshold: executes refund.
7. If amount > threshold: creates approval request.
8. Updates support ticket.
9. Responds with action/status.

## 6. Core Tools

Required typed tools:

- get_customer(customer_id/email)
- get_order(order_id)
- search_policy(query)
- calculate_refund(order_id)
- issue_refund(order_id, amount, reason)
- create_approval_request(...)
- update_ticket(ticket_id, status, note)
- create_shipping_replacement(...)
- escalate_to_human(reason)

Tools should operate against a mock or seeded backend that behaves like a real business system.

## 7. Functional Requirements

### 7.1 Agent Orchestration

- Explicit state graph/workflow.
- Conditional branches.
- Tool retries.
- Timeout handling.
- Idempotency keys for write operations.
- Workflow persistence.
- Resume after human approval.

### 7.2 Human-in-the-Loop

Require approval for:
- refunds over threshold;
- account-changing actions;
- uncertain policy decisions;
- repeated tool failures.

Approval UI must show:
- requested action;
- reason;
- customer/order context;
- proposed amount;
- relevant policy citation.

### 7.3 Safety

- Separate read tools from write tools.
- Validate all tool inputs with schemas.
- Never let free-form model output directly execute SQL.
- Use explicit allowlists for writable fields/actions.
- Add maximum refund amount and business rules outside the model.
- Log tool calls and outcomes.

### 7.4 State

Persist:
- conversation state
- customer/order IDs
- selected policy evidence
- tool results
- approval status
- workflow step
- final outcome

### 7.5 Evaluation

Create at least 100 scenario cases covering:
- valid refund
- invalid refund
- damaged item
- lost shipment
- duplicate charge
- order not found
- ambiguous user request
- tool failure
- approval required
- malicious prompt/tool injection attempts

Metrics:
- task success rate
- correct tool selection
- correct tool arguments
- policy compliance
- escalation precision/recall
- unsafe action rate
- average tool calls/task
- p50/p95 latency
- cost/task

### 7.6 Observability

Each run should capture:
- run_id
- model/version
- prompt version
- workflow path
- tool inputs/outputs
- retries/errors
- token use
- latency
- final status
- evaluator scores

## 8. Suggested Architecture

Web UI
→ FastAPI
→ Agent Orchestrator
→ Policy RAG
→ Tool Layer
   → Customer service
   → Order service
   → Refund service
   → Ticket service
→ PostgreSQL state store
→ Approval Queue/UI
→ Trace/Evaluation store

## 9. Suggested Tech Stack

- Python
- FastAPI
- LangGraph or custom state-machine orchestration
- OpenAI Responses/Agents SDK or another tool-calling model API
- PostgreSQL
- Redis
- Qdrant/pgvector for policy RAG
- Pydantic schemas
- OpenTelemetry
- Docker
- Pytest
- React/Next.js or Streamlit
- Optional AWS deployment with ECS/RDS/ElastiCache

## 10. API Surface

### POST /agent/runs
Starts a workflow.

### GET /agent/runs/{run_id}
Returns current state and trace summary.

### POST /approvals/{approval_id}/approve
Approves a pending action.

### POST /approvals/{approval_id}/reject
Rejects and returns the run to the agent.

### GET /traces/{run_id}
Returns structured workflow/tool trace.

### POST /eval/run
Runs scenario regression suite.

## 11. Backend Domain Objects

### orders
- id
- customer_id
- total
- currency
- status
- delivered_at

### tickets
- id
- customer_id
- order_id
- status
- messages

### refunds
- id
- order_id
- amount
- reason
- status
- idempotency_key

### approvals
- id
- run_id
- action
- payload
- status
- reviewer
- created_at

### agent_runs
- id
- state_json
- status
- model
- prompt_version
- started_at
- completed_at

## 12. MVP Acceptance Criteria

- Agent completes at least five distinct support workflows.
- At least six typed tools are implemented.
- Write operations are idempotent.
- Human approval pauses and resumes correctly.
- 100-case scenario evaluation set exists.
- Tool selection and task-success metrics are reported.
- Prompt-injection/tool-abuse tests exist.
- Full run traces are viewable.
- Dockerized local deployment works end to end.

## 13. Stretch Goals

- Multi-agent specialization only if a single graph becomes genuinely unwieldy.
- Email or chat integration.
- Queue prioritization.
- Automatic post-resolution quality scoring.
- Model routing based on task complexity.
- Offline replay of production traces.
- Policy-version-aware decisions.

## 14. Repository Structure

```text
agentic-support-ops/
├── app/
│   ├── api/
│   ├── agent/
│   ├── tools/
│   ├── policies/
│   ├── approvals/
│   ├── services/
│   └── observability/
├── evals/
├── fixtures/
├── tests/
├── migrations/
├── docker-compose.yml
└── README.md
```

## 15. Metrics to Put on the CV

Use measured numbers such as:

- Achieved X% end-to-end task success across 100 support scenarios.
- Reduced unsafe write-action rate from X% to Y% after adding typed tools and approval gates.
- Reached X% correct-tool selection and Y% correct-argument rate.
- Implemented resumable workflows with zero duplicate refunds under retry testing.

## 16. Example CV Bullet

> Built a stateful AI support agent with typed tool calling, policy RAG, resumable workflows, human approval gates, idempotent business actions, trace-level observability, and a 100-scenario regression evaluation suite.
