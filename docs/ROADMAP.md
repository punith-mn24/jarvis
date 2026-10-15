# Roadmap

## Milestone 1 — delivered foundation

- MongoDB persistence, durable plan/step state, worker leases, audit history.
- Typed sandbox tools, output passing, exact-message approvals.
- Read-back verification, bounded retries, explicit failures and kill switch.
- Responsive client and live cross-tab task state.
- Failure-injection and persistence/concurrency test suite.

## Milestone 2a — implemented, awaiting configured-account validation

- Local Ollama agent loop with structured decisions, clarification, tool validation, and budgets.
- Gmail web sends in Brave with Sent checks, sender binding and durable duplicate protection.
- Editable email drafts, sender-bound approvals, read-only rechecks, and browser session setup.
- Real local notes in MongoDB and retained sandbox failure-injection tests.

The configured Qwen model is installed on the development machine. A real local Ollama-to-MongoDB
note task has been verified. Gmail still needs account setup for real-account validation. External
service tests use mocks; no external message was sent.

## Milestone 2b — extend account integrations

1. Evaluate the chosen local model against real email requests and ambiguous goals.
2. Add verified inbox reading and explicit contact lookup.
3. Implement Google OAuth with test accounts, minimum Gmail/Calendar scopes, encrypted refresh tokens,
   reconnect handling, and real read-back verification. Keep demo and live modes visibly distinct.
4. Add real Gmail reads and Calendar creates. Prove a task changes an external test account correctly.
5. Add connector-level timeout, HTTP error classification, reconciliation, and idempotency tests.

## Milestone 3 — complete the four-app path

Implemented experimental WhatsApp Web and Amazon India adapters: explicit recipient checks,
observed product/variant checks, exact action approvals, durable dispatch fences, and read-only
verification. Dedicated Firefox/Brave/Amazon profiles support manual QR/sign-in. These still need validation
against signed-in accounts and DOM fixtures from supported site versions. No checkout is available.
The futuristic UI and model-readiness/download controls are implemented.

1. Add Slack and Notion or another notes provider with scoped credentials and independent read-back.
2. Implement LLM summarisation with untrusted email/web content treated as data, never instructions.
3. Add bounded replanning for logic failures, exact-payload edit/reapprove, and real reconnect actions.
4. Benchmark roughly 30 real tasks. Track completion, false success, recovery, latency, and cost.

## Milestone 4 — devices and deployment

1. Add per-user sessions, rate limits, HTTPS hosting, and two-device tests across phone and laptop.
2. Add an installable PWA and optional React/Next.js client.
3. Add a paired device agent with a revocable token and an allow-listed filesystem root.
4. Add user-visible memory, deletion, supported undo, observability, and scalable event delivery.

Provider keys and OAuth app credentials are needed before real-account testing. Do not paste them into
source code or commit them; use environment variables or an appropriate secret store.
