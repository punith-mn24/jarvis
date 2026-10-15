# Jarvis

A personal-agent foundation for the hackathon problem statement: **plan → act → verify → decide**.
Built from [the PRD](Jarvis_PRD.md) and [the TRD](Jarvis_TRD.md), using **MongoDB** for persistent state.

**Current implementation: local Ollama planning, Gmail in Brave, WhatsApp in Firefox,
Amazon India clothing/cart actions, and MongoDB notes.** Task results and audit history persist
in MongoDB. Browser adapters use fixed actions and verify provider state; account login and
CAPTCHA remain manual. General website automation and checkout are not implemented.

## Start the live agent

1. Start MongoDB and the API using the native Python instructions below.
2. Start Ollama (`ollama serve`), then download `qwen3:4b` using the dashboard's
   **Download / resume model** control or `ollama pull qwen3:4b`.
3. Install **Firefox** and **Brave** on this laptop. Install the Amazon browser once:
   `python -m playwright install chromium`. Selenium Manager downloads geckodriver on first use;
   install it separately or set `JARVIS_GECKODRIVER_BINARY` if automatic setup is unavailable.
4. Open **http://localhost:8000**, unlock with `JARVIS_ACCESS_TOKEN` from `.env`, and open **Connections**.
5. For **Gmail**, click **Open Gmail in Brave**, sign in, then **Verify Gmail session**.
   Jarvis uses Gmail's web interface in Brave; it does not use SMTP or ask for an app password.
   Review the exact sending account, recipient, subject and message before approving a send.
6. For **WhatsApp**, click **Open WhatsApp in Firefox** and scan the QR code with your phone.
   Supply an exact international number (for example `+91…`) and message. Jarvis uses only Firefox.
7. For **Amazon India**, open Amazon sign-in and sign in in the dedicated browser.
   Try “Find a black T-shirt on Amazon India, size M, under ₹1,000 and add it to my cart.”
   Jarvis searches, inspects the selected variant, asks for missing size information, then pauses
   before adding the exact item. It checks the active cart afterward; it never checks out.

Each browser uses a **dedicated Jarvis profile**, so signing into your usual personal browser
window does not sign into Jarvis. Profiles live in ignored `.local/jarvis-firefox`,
`.local/jarvis-brave`, and `.local/jarvis-browser`. Never upload these directories or `.env`.
Only one API process should use these browser profiles. Actions run on the server laptop's desktop,
even when the dashboard is opened from another device.

Optional paths in `.env`: `JARVIS_FIREFOX_BINARY`, `JARVIS_BRAVE_BINARY`,
`JARVIS_GECKODRIVER_BINARY`, `JARVIS_FIREFOX_PROFILE`, `JARVIS_BRAVE_PROFILE`.
These support browsers installed outside PATH, including on another laptop. Gmail/WhatsApp do not
fall back to a different browser if their selected app cannot start.

The browser integrations need validation with your signed-in accounts. Gmail selectors currently
expect the English web interface. WhatsApp may hide the actual phone number behind an opaque contact
identifier; Jarvis stops if it cannot independently confirm the recipient. Amazon page layouts and
availability can change. A sent record/checkmark confirms submission, not recipient delivery or reading.
Uncertain sends/cart mutations are never blindly repeated; **Verify again** performs read-back only.

On the original development laptop, local services can be restarted with:

```bash
# While the background services installed for this desktop session exist:
systemctl --user restart jarvis-mongodb jarvis-ollama jarvis-api
systemctl --user status jarvis-mongodb jarvis-ollama jarvis-api
journalctl --user -u jarvis-api -n 50
```

These session services run independently of the terminal or chat. After a reboot,
or on another laptop, start the processes directly:

```bash
.local/mongodb/bin/mongod --dbpath .local/mongo-data --bind_ip 127.0.0.1 --port 27017
# In another terminal:
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS="$PWD/.local/ollama-models" \
  OLLAMA_NUM_PARALLEL=1 .local/ollama-native/usr/bin/ollama serve
# In another terminal:
.venv/bin/uvicorn jarvis.main:create_app --factory --host 127.0.0.1 --port 8000
```

## Run with Docker

Compose starts MongoDB and the API/sandbox. For live mode, set `JARVIS_OLLAMA_URL` to an Ollama service
reachable from inside the container; `127.0.0.1` there refers to the container itself. Browser account login requires a desktop. This image does not include graphical Firefox/Brave sessions;
use native Python on your desktop for real browser workflows.

```bash
python3 scripts/setup.py
docker compose up --build
```

Open **http://localhost:8000**. Unlock it using `JARVIS_ACCESS_TOKEN` from the generated `.env` file.
API documentation: http://localhost:8000/docs (use **Authorize** with the same token).

The Compose services bind to loopback. MongoDB data survives container restarts in `mongo-data`.
This Compose configuration is for local development; its internal MongoDB has no authentication.
Do not expose its database port publicly.

## Run with Python and an existing MongoDB

Requires Python 3.11+ and MongoDB 8.x (local or Atlas). Node.js is not required for this first client.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python scripts/setup.py
# Edit JARVIS_MONGODB_URI in .env if needed.
uvicorn jarvis.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

If only a local MongoDB service is needed, run `docker compose up -d mongodb` first.
On the initial development machine, MongoDB binaries and database files are in ignored `.local/`.
To start that downloaded database again (if it is stopped):

```bash
.local/mongodb/bin/mongod --dbpath .local/mongo-data --bind_ip 127.0.0.1 --port 27017
```

The application requires MongoDB and fails startup if it cannot connect. There is no silent in-memory fallback.

## Try the demo

1. Select **Sandbox demo**, then **Make room for your day** → **Start task**.
2. Inspect the fixture data and the explicit UTC calendar time. Approve the plan.
3. Watch Gmail → Notes → Calendar execute. Each step is read back and checked.
4. Jarvis pauses for approval of the fully resolved Slack message. Approve it to finish.
5. Repeat with **Silent write failure**. The connector returns an ID without saving a record;
   independent read-back detects the missing write and stops downstream steps.
6. Try **Rate limit** for bounded automatic retry, **Authorization denied** for `NEEDS_USER`,
   and **Incorrect saved data** for a postcondition mismatch.
7. Open another browser tab with the same token. Both see persisted task state and live updates.

**Disable fault & retry** removes the injected fault but keeps the three-attempt budget and verified steps.
An incorrectly stored record remains for inspection; retries do not silently overwrite it.
Cancelling stops later steps; completed writes remain and a call already in flight may finish.

## What is implemented

- Local Ollama decisions, clarification, structured validation, recipient-source checks, editable
  drafts, live/sandbox separation, and subsequent actions based on verified results.
- Gmail in Brave with browser-account binding, exact draft checks, independent Sent read-back and
  durable duplicate-send protection. The former SMTP adapter remains for protocol regression tests
  but is not in the live email registry.
- WhatsApp Web and Amazon India browser adapters with durable dispatch records and independent read-back.
- Dark futuristic dashboard with animated local core, model setup progress, workflow shortcuts,
  responsive layouts, plan inspection, approvals, step timeline, searchable/paginated history,
  audit inspection, connection status, browser-device heartbeat, workspace lock.
- FastAPI REST API and authenticated WebSocket snapshots; reconnects reload durable state.
- PyMongo `AsyncMongoClient`, task indexes, embedded steps/events, sandbox artifacts, device records.
- Typed tool registry with trusted risk tiers and validated input schemas.
- Output references between steps; dependencies execute only after verified prerequisites.
- Exact-payload approval before every external-facing sandbox step.
- Independent provider-state verification; failed/unknown checks never produce success.
- Transient retry with exponential backoff/jitter, per-call timeout, three attempts per step,
  15-step plan limit, repeated-call guard, explicit failure classes, cancellation.
- Atomic worker claims and versioned writes. An expired lease can be reclaimed after a restart;
  interrupted writes are read back before idempotent re-execution.
- Shared-workspace bearer authentication. REST uses an Authorization header; WebSocket authentication
  uses the first frame, so credentials do not appear in URLs.

## Structure

```text
jarvis/
  config.py       Environment settings
  models.py       Task, step, failure, approval, and audit models
  store.py        MongoDB persistence, version checks, worker leases
  planner.py      Explicit sandbox scenario planner
  agent.py        Local Ollama decisions, clarification, next-action selection
  email_tools.py  SMTP/IMAP email tools and durable dispatch protection
  browser_tools.py Browser action registry
  amazon_tools.py  Amazon search, variants and cart read-back
  whatsapp_tools.py WhatsApp actions in Firefox
  gmail_browser.py Gmail actions in Brave
  browser_runtime.py Persistent Chromium/Brave sessions
  firefox_runtime.py Installed Firefox app and persistent QR session
  live_tools.py   Explicit allowlist of real actions
  model_runtime.py Model readiness and resumable downloads
  tools.py        Tool schemas, references, independent read-back, fault injection
  engine.py       Sequential execution, approval gates, verification, recovery
  main.py         REST, authenticated WebSocket sync, static client
  static/         Responsive browser client (no frontend build dependency)
tests/            Planner tests and real MongoDB API/execution tests
docs/             Architecture decisions and next milestones
```

## Test

```bash
# Run the full suite against MongoDB. Each integration test gets its own disposable database.
JARVIS_TEST_MONGODB_URI=mongodb://127.0.0.1:27017 pytest -q
ruff check jarvis tests scripts
ruff format --check jarvis tests scripts
```

Without `JARVIS_TEST_MONGODB_URI`, only pure unit tests run; database integration tests are reported as skipped.
Never point test configuration at a credential that cannot create/drop isolated `jarvis_test_*` databases.

The suite covers silent/wrong writes, denied authorization, transient retry, approval gates,
stale approvals, concurrent worker claims, cancellation during a write, restart after an uncheckpointed
write, attempt budgets, dependency references, API authentication, history, and device presence.

## Scope and next work

See [the implementation roadmap](docs/ROADMAP.md). Arbitrary website automation, checkout, OAuth,
inbox reading, automatic recovery replanning, per-user authentication, device filesystem actions,
encrypted OAuth token storage, memory, undo, offline/installable PWA support, and production hardening
are **not implemented**. The browser client is responsive but is not yet a PWA.

The initial frontend is framework-free so this slice can run in the current Python-only environment.
The API boundary supports adding the TRD's React/Next.js client later. The initial worker runs inside
FastAPI and polls MongoDB; Redis/change streams are deferred. Real multi-device access needs a reachable
HTTPS host, WSS, and the same workspace token. This starter is a single trusted workspace, not a
multi-user service.

Implementation references: [PyMongo Async](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/)
and [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/).
Live integration references: [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
[Gmail SMTP/IMAP](https://developers.google.com/workspace/gmail/imap/imap-smtp),
and [Playwright persistent contexts](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context).

Firefox runtime reference: [Selenium Firefox documentation](https://www.selenium.dev/documentation/webdriver/browsers/firefox/).
