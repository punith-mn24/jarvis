# Jarvis — Personal AI Agent
## Product Requirements Document (PRD)

| | |
|---|---|
| Version | 0.1 (Draft) |
| Date | 15 Oct 2026 |
| Status | For team review |

---

## 1. Overview

Jarvis is a personal AI agent that takes a goal in natural language ("Plan my Saturday and tell Rahul"), breaks it into steps, executes those steps across multiple apps, works across the user's devices, and **detects when a step or task has failed** — then retries, replans, or asks the user.

### Problem
- Assistants today answer questions but rarely *finish* work that spans several apps.
- Agents that do act often fail silently: they claim success when nothing happened.
- Context is lost when switching devices (phone → laptop).

### Vision
> Say it once, from any device. Jarvis gets it done, and tells you honestly if it didn't.

---

## 2. Goals & Non-Goals

### Goals
1. Complete real multi-step tasks across ≥ 4 apps.
2. Same agent, same state, on ≥ 2 devices (phone/web + laptop).
3. Verify every step's outcome; detect and handle failure explicitly.
4. Keep the user in control: confirmation for risky actions, full action log.

### Non-Goals (v1)
- Fully autonomous, unsupervised operation.
- Voice-first UX (optional stretch).
- Support for every app — only a curated set of connectors.
- Multi-user / team features.

---

## 3. Target Users & Personas

| Persona | Need |
|---|---|
| **Student / early professional** | Manage mail, calendar, notes, reminders without app-hopping |
| **Busy professional** | Delegate routine multi-app chores (scheduling, summaries, follow-ups) |

---

## 4. Core Use Cases

| # | Scenario | Apps involved |
|---|---|---|
| UC1 | "Summarise unread emails from today and add any deadlines to my calendar." | Gmail → LLM → Google Calendar |
| UC2 | "Schedule a meeting with Priya tomorrow afternoon and send her the invite + a Slack heads-up." | Calendar, Gmail, Slack |
| UC3 | "Research X, save notes to my notes app, and set a reminder to review Friday." | Browser/Search, Notion/Notes, Reminders |
| UC4 | Start on phone: "Download the report from my mail and put it in my laptop's Documents folder." | Gmail, Device agent (laptop filesystem) |
| UC5 | A task fails mid-way (e.g., calendar API 403) → Jarvis reports exactly which step failed, what it tried, and options. | Any |

---

## 5. User Stories

- As a user, I give a goal in plain language and see a **plan** before execution starts.
- As a user, I see **live progress** (step 2/5: creating event…).
- As a user, I'm **asked for confirmation** before sending messages, deleting data, or spending money.
- As a user, I'm **told clearly when something failed** and why, never a false "done".
- As a user, I can **start a task on my phone and see/continue it on my laptop**.
- As a user, I can **review history** of everything Jarvis did and **undo** where possible.
- As a user, I can **connect/disconnect apps** and control permissions per app.

---

## 6. Functional Requirements

### 6.1 Task Understanding & Planning
| ID | Requirement | Priority |
|---|---|---|
| FR-1 | Accept text input (voice optional) | P0 |
| FR-2 | Convert goal into an ordered plan of steps with named tools and expected outcomes | P0 |
| FR-3 | Ask a clarifying question when required info is missing (max 2 per task) | P1 |
| FR-4 | Show plan to user; allow edit/approve | P1 |

### 6.2 Multi-Step, Multi-App Execution
| ID | Requirement | Priority |
|---|---|---|
| FR-5 | Execute steps sequentially, passing outputs between steps | P0 |
| FR-6 | Connectors for: Gmail, Google Calendar, Slack (or Telegram), Notes/Notion, Web search, local filesystem | P0 (≥4) |
| FR-7 | Run independent steps in parallel | P2 |
| FR-8 | Persist task state so execution survives crashes/restarts | P0 |

### 6.3 Cross-Device
| ID | Requirement | Priority |
|---|---|---|
| FR-9 | Web/PWA client usable on phone and laptop | P0 |
| FR-10 | Real-time task state sync between devices | P0 |
| FR-11 | Laptop **device agent** that can execute local actions (files, open app, clipboard) when invoked from another device | P1 |
| FR-12 | Conversation + memory shared across devices | P0 |

### 6.4 Failure Detection & Recovery (key differentiator)
| ID | Requirement | Priority |
|---|---|---|
| FR-13 | Every step defines a **success check** (postcondition), e.g. "event exists in calendar with correct time" | P0 |
| FR-14 | After each action, independently **verify** result by reading back from the target app | P0 |
| FR-15 | Classify failures: *transient* (timeout/rate limit), *auth* (expired/denied), *input* (bad/missing data), *logic* (wrong result), *unrecoverable* | P0 |
| FR-16 | Recovery policy: retry w/ backoff (transient) → replan alternate route (logic) → ask user (auth/input) → abort safely (unrecoverable) | P0 |
| FR-17 | Detect loops / no-progress (same action repeated, step budget exceeded) and halt | P0 |
| FR-18 | Final report states per-step status: ✅ verified / ⚠️ partial / ❌ failed, with reason | P0 |
| FR-19 | Never report success unless all required steps are verified | P0 |

### 6.5 Safety & Control
| ID | Requirement | Priority |
|---|---|---|
| FR-20 | Risk tiers: *read* (auto), *write-reversible* (auto/optional confirm), *write-irreversible / external-facing* (always confirm) | P0 |
| FR-21 | Per-app permission scopes; least-privilege OAuth | P0 |
| FR-22 | Complete audit log of every tool call (input, output, time) | P0 |
| FR-23 | Undo for supported actions (delete created event, etc.) | P2 |
| FR-24 | Kill switch: user can cancel a running task anytime | P0 |

### 6.6 Memory & Personalisation
| ID | Requirement | Priority |
|---|---|---|
| FR-25 | Short-term memory within a task | P0 |
| FR-26 | Long-term memory (contacts' nicknames, preferences like "afternoon = 2–5pm") | P1 |
| FR-27 | User can view/delete stored memory | P1 |

---

## 7. Non-Functional Requirements

| Area | Target |
|---|---|
| Latency | Plan shown < 5 s; each simple step < 10 s |
| Reliability | ≥ 90 % task success on benchmark set; **0 false-success reports** on failure-injection tests |
| Failure detection | ≥ 95 % of injected failures detected |
| Security | Tokens encrypted at rest; no secrets in logs; HTTPS/WSS only |
| Privacy | User data isolated; deletion on request |
| Availability | Demo-grade for v1 (best effort) |
| Observability | Trace for every task (plan → steps → verifications) |

---

## 8. UX Overview

1. **Chat + Task view** — input box, plan card, live step timeline with status icons.
2. **Confirmation sheet** — shows exactly what will be sent/changed; Approve / Edit / Cancel.
3. **Failure card** — what failed, what was tried, buttons: *Retry · Fix connection · Skip step · Cancel*.
4. **Devices** — list of connected devices, online status.
5. **Connections** — connect/disconnect apps, view scopes.
6. **History** — searchable log with per-step details.

---

## 9. Success Metrics

| Metric | Target |
|---|---|
| Task completion rate (benchmark of ~30 tasks) | ≥ 90 % |
| Failure-detection recall (injected faults) | ≥ 95 % |
| False-success rate | 0 % |
| Avg. user interventions per task | ≤ 1 |
| Cross-device handoff success | ≥ 95 % |
| Median time to complete 3-step task | < 45 s |

---

## 10. Scope & Milestones

### MVP (P0)
Text input → plan → execute across 4 apps → step verification → failure classification + recovery → web client with live sync → audit log.

### Phase 2
Laptop device agent, long-term memory, undo, parallel steps.

### Stretch
Voice input, proactive suggestions, mobile push notifications, more connectors.

| Week | Deliverable |
|---|---|
| 1 | Planner + executor skeleton, 2 connectors (Calendar, Gmail) |
| 2 | Verification layer + failure taxonomy + recovery policies |
| 3 | Web client, real-time sync, confirmation flow, 2 more connectors |
| 4 | Device agent, failure-injection test suite, polish, demo |

---

## 11. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| LLM hallucinates tool args / wrong steps | Wrong actions | Schema-validated tool calls; confirm risky actions |
| False success claims | Trust loss | Independent read-back verification (FR-14, FR-19) |
| OAuth / API changes or limits | Broken connectors | Graceful auth-failure path; mock connectors for demo |
| Infinite loops / runaway cost | Cost, bad UX | Step & token budgets, loop detection |
| Privacy of email/calendar data | Security | Least-privilege scopes, encryption, minimal retention |
| Scope creep across apps | Missed deadline | Fixed connector list for MVP |

---

## 12. Assumptions & Dependencies

- Access to an LLM API with tool/function calling.
- Google OAuth, Slack/Telegram app credentials available.
- User has at least one phone/browser and one laptop.

## 13. Open Questions

1. Which 4 connectors are final for the demo?
2. Voice input in v1 or not?
3. Hosting: local-first vs cloud-hosted backend?
4. Which LLM provider / model budget?
5. How will judges/evaluators test failure detection (live injection)?
