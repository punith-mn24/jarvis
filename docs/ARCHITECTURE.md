# Architecture decisions — sandbox and connected local agent

## Live agent

Ollama chooses one validated action at a time, using the user's goal, clarification answers, and
previous verified results. The live registry exposes `email.send`, `notes.save`, `whatsapp.send`,
`amazon.search`, `amazon.inspect`, and `amazon.add_to_cart`.
Unrecognized tools are rejected. The agent cannot silently use sandbox tools for a live task.
Inference renews the task lease every ten seconds and is bounded by time, output tokens, and 15 turns.

Email actions pause for exact sender/recipient/subject/body approval. Draft edits invalidate approval.
Recipient addresses must come from user text. The live email tool uses Gmail's web UI exclusively in
Brave. It checks the active sending account and exact draft, records a dispatch intent before Send,
then requires a new matching Sent message absent from the pre-send baseline. No SMTP fallback runs.
The legacy SMTP adapter is retained only for protocol regression tests.

WhatsApp runs in installed Firefox through Selenium/geckodriver with a dedicated persistent profile.
Browser commands are serialized in one background thread; there is no fallback to Chromium.
The send fence is recorded after exact draft validation and before the send click. Rechecks are
read-only. Normal desktop profiles are never copied or attached.

## Browser adapters and startup

Model readiness is checked before creating live tasks. The dashboard can start/resume Ollama model
pulls, displays progress and refreshes readiness. No incomplete download is treated as an installed model.

Dedicated profiles own manual sessions: Playwright controls Amazon Chromium and Gmail Brave;
Selenium controls WhatsApp Firefox. Tools use fixed domains
and a fixed set of selectors, not model-supplied JavaScript. Amazon ASINs must originate in verified
search/inspection results; cart inputs must match an observed exact variant. WhatsApp phone numbers
must come from user text, and the chat identity must independently match before sending. Unsafe
mutations pause for exact-payload approval. `browser_dispatches` stores intent before the click;
recovery only reads provider state, never blindly repeats uncertain clicks. Browser markup is not a
stable API; these connectors are experimental until tested with real signed-in accounts.

## MongoDB is the source of truth

The requested database is MongoDB, replacing the draft TRD's PostgreSQL/pgvector choice.
The runtime uses the supported PyMongo async driver, not Motor.

- `tasks`: `_id`, goal, scenario, status, embedded plan/steps/events, revision, lease owner/expiry, timestamps.
- `artifacts`: deterministic `_id = task_id:step_id`, sandbox provider, payload, creation time.
- `email_dispatches`: SMTP intent and reconciliation state.
- `browser_dispatches`: unique action keys and pre-send observations.
- `browser_observations`: observed Amazon variant details scoped to each task.
- `devices`: browser identifier, name, last-seen timestamp, capabilities. A TTL removes old sessions.

Tasks embed bounded steps and audit records to atomically checkpoint state and its audit event in one
MongoDB document. There are at most 15 steps and three action attempts per step. No multi-document
transactions or replica set are needed. OAuth tokens are not used in this milestone.
For production, enforce document-size/retention limits and separate larger audit payloads.

## Concurrency and recovery

The in-process worker claims one queued task with `find_one_and_update`. A 60-second lease prevents
another worker from claiming it until it expires. Every checkpoint renews the lease. Tool calls are
bounded by each tool’s timeout; long browser tools renew the worker lease while running. Every state write compares
the current revision, so a stale worker cannot overwrite a cancellation or another worker's progress.

An interrupted worker leaves durable state. After expiry, another worker reads the provider's state
before repeating an unfinished call. Sandbox writes use a deterministic artifact key with
`$setOnInsert`, making repeated writes idempotent. New live connectors must explicitly establish their
retry/idempotency behavior; they must not assume a remote API supports idempotency.

Cancellation is cooperative. A provider operation already in flight may still finish; the task remains
cancelled and later steps do not execute. This is surfaced in the API/UI. It is not rollback.

## Approval and verification

Plans require initial approval. Each external-facing step then pauses separately after input references
resolve, showing the exact payload. Approval binds to both the task revision and that resolved payload.
The worker rechecks the payload before acting. Risk comes from the tool registry, never the planner.

Sandbox writes return a record ID. Verification makes a separate MongoDB read and compares provider and
every expected field. Missing, mismatched, or inconclusive state stops the task. A fake success response
alone cannot complete a step. Fixture inbox reads compare against the fixture source of truth.

## Synchronization and identity

Clients authenticate with a single shared workspace token. Task snapshots come from MongoDB every
500 ms while a WebSocket is open; changed revisions trigger a fresh snapshot. Reconnection gets current
state plus embedded audit history. This works without replica-set change streams or Redis, at the cost
of polling/full snapshots. The stream contains the newest 50 tasks; older history uses REST pagination.

Browser IDs identify presence only, not security principals. JWT sessions, individual users, rate
limiting, OAuth scopes/token encryption, and public hosting are later milestones. Do not treat the
shared token as production multi-user authorization.
