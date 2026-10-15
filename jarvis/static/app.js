const $ = (selector) => document.querySelector(selector);
const state = {
  tasks: [],
  older: [],
  selected: null,
  scenario: null,
  view: "workspace",
  socket: null,
  retry: null,
  beat: null,
  detailRevision: null,
  runtime: null,
  creating: false,
};
let token = sessionStorage.getItem("jarvis-token") || "";
let deviceId = localStorage.getItem("jarvis-device");
if (!deviceId) {
  deviceId = crypto.randomUUID();
  localStorage.setItem("jarvis-device", deviceId);
}
const names = {
  workspace: "Command center",
  history: "Task history",
  connections: "Connections",
  devices: "My devices",
};
const scenarioGoals = {
  daily_brief:
    "Read the demo inbox, save a brief, block review time, and notify the team.",
  meeting:
    "Create a demo meeting, prepare an email invite, and post a Slack heads-up.",
  notes:
    "Save a demo project checkpoint and create a calendar reminder to review it.",
};
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const label = (status) => status.toLowerCase().replaceAll("_", " ");
const badge = (status) =>
  `<span class="status ${escapeHtml(status)}">${escapeHtml(label(status))}</span>`;
const date = (value) =>
  new Date(value).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
const json = (value) =>
  `<pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("#toast").hidden = true), 6000);
}
async function api(path, method = "GET", body) {
  const response = await fetch("/api" + path, {
    method,
    headers: {
      Authorization: "Bearer " + token,
      "Content-Type": "application/json",
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) lock();
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Check the request and try again.",
    );
  }
  return data;
}
function lock() {
  token = "";
  sessionStorage.removeItem("jarvis-token");
  clearTimeout(state.retry);
  clearInterval(state.beat);
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
    state.socket = null;
  }
  state.tasks = [];
  state.older = [];
  closeDrawer();
  renderTasks();
  $("#connections-list").replaceChildren();
  $("#devices-list").replaceChildren();
  $("#sync-status").innerHTML = "<i></i>Locked";
  if (!$("#auth-dialog").open) $("#auth-dialog").showModal();
}
async function heartbeat() {
  await api("/devices/heartbeat", "POST", {
    id: deviceId,
    name: matchMedia("(max-width: 600px)").matches
      ? "Mobile browser"
      : "Desktop browser",
  });
  if (state.view === "devices") await renderDevices();
}
function connect() {
  if (!token) return;
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
  }
  const socket = new WebSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`,
  );
  state.socket = socket;
  socket.onopen = () => socket.send(JSON.stringify({ token }));
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "tasks.snapshot") {
      state.tasks = message.tasks;
      renderTasks();
      if (state.selected) renderDetail();
      $("#sync-status").innerHTML = "<i></i>Live sync";
      $("#sync-status").classList.remove("offline");
    }
  };
  socket.onclose = (event) => {
    if (event.code === 4401) {
      lock();
      return;
    }
    $("#sync-status").innerHTML = "<i></i>Reconnecting";
    $("#sync-status").classList.add("offline");
    if (token) state.retry = setTimeout(connect, 2500);
  };
}
async function start() {
  state.tasks = await api("/tasks");
  renderTasks();
  connect();
  await heartbeat();
  await refreshRuntime();
  clearInterval(state.beat);
  state.beat = setInterval(() => {
    heartbeat().catch(() => {});
    refreshRuntime().catch(() => {});
  }, 5000);
}
function allTasks() {
  return [
    ...state.tasks,
    ...state.older.filter(
      (old) => !state.tasks.some((task) => task.id === old.id),
    ),
  ];
}
function taskRow(task) {
  const verified = task.steps.filter(
    (step) => step.status === "VERIFIED",
  ).length;
  return `<button class="task-row" data-task="${escapeHtml(task.id)}"><span class="task-row-icon">${task.status === "SUCCEEDED" ? "✓" : "◈"}</span><span class="task-row-content"><span class="task-row-title">${escapeHtml(task.goal)}</span><span class="task-row-sub">${escapeHtml(date(task.created_at))} · ${verified}/${task.steps.length} verified · ${escapeHtml(task.mode)}</span></span>${badge(task.status)}<span aria-hidden="true">›</span></button>`;
}
function renderTasks() {
  const tasks = allTasks();
  $("#history-count").textContent = tasks.length;
  $("#task-count").textContent = state.tasks.length;
  $("#metric-active").textContent = state.tasks.filter((task) =>
    ["RUNNING", "QUEUED"].includes(task.status),
  ).length;
  $("#metric-verified").textContent = state.tasks.filter(
    (task) => task.status === "SUCCEEDED",
  ).length;
  $("#metric-attention").textContent = state.tasks.filter((task) =>
    ["AWAITING_APPROVAL", "NEEDS_USER", "PARTIAL", "FAILED"].includes(
      task.status,
    ),
  ).length;
  $("#task-list").innerHTML =
    state.tasks.slice(0, 4).map(taskRow).join("") ||
    '<div class="empty-state"><strong>Standing by for your first mission.</strong>Describe a goal or choose a connected workflow above.</div>';
  const query = $("#history-search").value.toLowerCase();
  $("#history-list").innerHTML =
    tasks
      .filter((task) =>
        `${task.goal} ${task.status}`.toLowerCase().includes(query),
      )
      .map(taskRow)
      .join("") || '<div class="empty-state">No matching tasks yet.</div>';
  $("#load-more").hidden = state.tasks.length < 50;
}
function openTask(id) {
  state.selected = id;
  state.detailRevision = null;
  $("#task-drawer").hidden = false;
  $("#drawer-backdrop").hidden = false;
  document.body.style.overflow = "hidden";
  renderDetail();
  $("#close-drawer").focus();
}
function closeDrawer() {
  state.selected = null;
  $("#task-drawer").hidden = true;
  $("#drawer-backdrop").hidden = true;
  document.body.style.overflow = "";
}
function renderDetail() {
  const task = allTasks().find((item) => item.id === state.selected);
  if (!task || state.detailRevision === `${task.id}:${task.revision}`) return;
  state.detailRevision = `${task.id}:${task.revision}`;
  const open = new Set(
    [...$("#task-detail").querySelectorAll("details[open]")].map(
      (node) => node.dataset.key,
    ),
  );
  const verified = task.steps.filter(
    (step) => step.status === "VERIFIED",
  ).length;
  const pending = task.steps.find((step) => step.status !== "VERIFIED");
  const failure = task.steps.find((step) => step.status === "FAILED");
  const live = task.mode === "live";
  const needsApproval =
    pending?.risk === "write_irreversible" && pending.resolved_args;
  let actions = "";
  if (task.status === "AWAITING_APPROVAL") {
    if (pending?.tool === "email.send") {
      const draft = pending.resolved_args;
      actions = `<div class="approval-card"><h3>Review before sending.</h3><p>This will send a real email from ${escapeHtml(draft.sender || "your Gmail account in Brave (connect it first)")}.</p><form id="draft-form" class="connection-form"><label for="draft-recipient">To</label><input id="draft-recipient" type="email" value="${escapeHtml(draft.recipient)}" required><label for="draft-subject">Subject</label><input id="draft-subject" value="${escapeHtml(draft.subject)}" maxlength="200" required><label for="draft-body">Message</label><textarea id="draft-body" rows="7" required>${escapeHtml(draft.body)}</textarea><button class="secondary" type="submit">Save draft changes</button></form><p>Approval sends the saved draft above. Editing requires saving before sending.</p><button class="primary" data-action="approve">Approve & send email <span>→</span></button></div>`;
    } else if (live && needsApproval) {
      const cart = pending.tool === "amazon.add_to_cart";
      actions = `<div class="approval-card"><div class="eyebrow">AUTHORIZATION REQUIRED</div><h3>${cart ? "Review your cart item" : "Review your WhatsApp message"}</h3><p>${cart ? "Adds the exact variant to your Amazon India cart. No checkout or payment." : "Sends from the WhatsApp account linked in Firefox."}</p>${json(pending.resolved_args)}<button class="primary" data-action="approve">${cart ? "Approve & add to cart" : "Approve & send message"} →</button></div>`;
    } else {
      actions = `<div class="approval-card"><h3>${needsApproval ? "Your message, your call." : "Review your plan."}</h3><p>Sandbox only. No real message will be sent.</p>${needsApproval ? json(pending.resolved_args) : ""}<button class="primary" data-action="approve">${needsApproval ? "Approve this message" : "Approve & run plan"} <span>→</span></button></div>`;
    }
  }
  if (task.question && task.status === "NEEDS_USER") {
    actions += `<div class="approval-card"><h3>A little more information</h3><p>${escapeHtml(task.question)}</p><form id="answer-form" class="connection-form"><label for="answer-text">Your answer</label><textarea id="answer-text" rows="3" maxlength="4000" required></textarea><button class="primary" type="submit">Continue task →</button></form></div>`;
  } else if (live && task.status === "NEEDS_USER" && !failure) {
    actions +=
      '<button class="secondary" data-action="retry">Retry local AI planning</button>';
  }
  if (failure && ["FAILED", "PARTIAL", "NEEDS_USER"].includes(task.status)) {
    actions += `<div class="failure-card"><h3>${escapeHtml(failure.error?.kind)} · Action stopped</h3><p>${escapeHtml(failure.error?.message)}</p><p>${live ? "Later steps have not run. Check Connections if an account needs attention." : "Later steps have not run. Retrying can disable the injected fault."}</p>${failure.attempts < 3 && failure.error?.retry_allowed !== false ? `<button class="secondary" data-action="retry">${live ? "Retry failed step" : "Disable fault & retry"}</button>` : ""}${["email.send", "whatsapp.send", "amazon.add_to_cart"].includes(failure.tool) ? '<button class="secondary" data-action="recheck">Verify again (no repeated action)</button>' : ""}</div>`;
  }
  $("#task-detail").innerHTML =
    `${badge(task.status)}<h2 class="detail-heading">${escapeHtml(task.goal)}</h2><p class="detail-summary">${escapeHtml(task.summary)}</p><div class="progress-track"><div style="width:${task.steps.length ? (verified / task.steps.length) * 100 : 0}%"></div></div><span class="progress-label">${verified} of ${task.steps.length} actions verified · ${escapeHtml(task.mode)}</span>${actions}<div class="timeline">${task.steps.map((step, index) => `<div class="step"><span class="step-dot ${step.status}">${step.status === "VERIFIED" ? "✓" : step.status === "FAILED" ? "!" : index + 1}</span><h3>${escapeHtml(step.title)}</h3><div class="step-meta">${escapeHtml(step.tool)} · ${escapeHtml(label(step.risk))} · ${step.attempts} attempts</div>${badge(step.status)}<p>${escapeHtml(step.expected_outcome)}</p><details data-key="${step.id}"><summary>Inspect inputs${step.result ? " & result" : ""}</summary>${json(step.resolved_args || step.args)}${step.result ? json(step.result) : ""}</details></div>`).join("")}</div>${!["SUCCEEDED", "CANCELLED"].includes(task.status) ? '<button class="danger-button" data-action="cancel">Cancel this task</button>' : ""}<details class="audit" data-key="audit"><summary>Full audit trail · ${task.events.length} events</summary>${task.events.map((event) => `<div class="audit-row"><span>${escapeHtml(date(event.ts))}</span> <code>${escapeHtml(event.type)}</code><p>${escapeHtml(event.message)}</p>${Object.keys(event.data).length ? json(event.data) : ""}</div>`).join("")}</details>`;
  for (const node of $("#task-detail").querySelectorAll("details"))
    if (open.has(node.dataset.key)) node.open = true;
}
function updateLaunch() {
  $("#create-button").disabled =
    state.creating ||
    ($("#task-mode").value === "live" && !state.runtime?.model_available);
}
async function refreshRuntime() {
  const runtime = await api("/runtime");
  state.runtime = runtime;
  const download = runtime.download;
  const busy = ["downloading", "retrying"].includes(download.state);
  $("#runtime-text").textContent = runtime.model_available
    ? `LOCAL INTELLIGENCE READY / ${runtime.model} · 6 live tools`
    : !runtime.reachable
      ? "Ollama is offline. Start the local Ollama service."
      : busy
        ? `${download.message} · ${download.percent}%`
        : download.state === "failed"
          ? download.message
          : "Model setup required. Download once to start live tasks.";
  $("#runtime-banner").classList.toggle("ready", runtime.model_available);
  $("#install-model").hidden = runtime.model_available;
  $("#install-model").disabled = !runtime.reachable || busy;
  $("#model-progress").hidden = !busy;
  $("#model-progress").value = download.percent;
  updateLaunch();
  return runtime;
}
async function renderConnections() {
  const runtime = await refreshRuntime();
  const apps = await api("/connections");
  const descriptions = {
    gmail: "Fixture emails only.",
    calendar: "Sandbox calendar only.",
    slack: "Sandbox messages only.",
    notes: "Sandbox notes only.",
  };
  $("#connections-list").innerHTML = apps
    .map((app) => {
      let description = descriptions[app.id] || "";
      let status = "SANDBOX";
      if (app.id === "live_gmail") {
        description =
          app.address ||
          "Sign in to Gmail in Brave below, then verify the session.";
        status = app.connected ? "CONFIGURED" : "SETUP REQUIRED";
      }
      if (app.id === "ollama") {
        description = runtime.model + " · runs locally";
        status = runtime.model_available ? "READY" : "SETUP REQUIRED";
      }
      let setup = "";
      if (["whatsapp", "amazon"].includes(app.id)) {
        description =
          app.id === "whatsapp"
            ? "Link your phone by scanning the QR code in Firefox. WhatsApp runs only in that dedicated Firefox session."
            : "Sign in in the Jarvis browser. Search clothing, select a variant and add to cart. Human verification may be required.";
        status = "BROWSER SESSION";
        setup = `<button class="secondary" data-connect="${app.id}">Open ${app.id === "whatsapp" ? "WhatsApp in Firefox" : "Amazon sign-in"} ↗</button>`;
      }
      return `<div class="connection-card"><h2>${escapeHtml(app.name)}</h2><p>${escapeHtml(description)}</p><span class="status">${status}</span>${setup}</div>`;
    })
    .join("");
  if (runtime.email_address)
    $("#gmail-result").textContent =
      "Selected sending account: " +
      runtime.email_address +
      ". Verify the Brave session before sending.";
}
async function renderDevices() {
  const devices = await api("/devices");
  $("#devices-list").innerHTML =
    devices
      .map(
        (device) =>
          `<div class="task-row device-row"><span class="task-row-icon">▣</span><span class="task-row-content"><span class="task-row-title">${escapeHtml(device.name)}${device.id === deviceId ? " · this browser" : ""}</span><span class="task-row-sub">Last seen ${escapeHtml(date(device.last_seen))} · web client</span></span><span class="status ${device.online ? "VERIFIED" : ""}">${device.online ? "online" : "offline"}</span></div>`,
      )
      .join("") || '<div class="empty-state">No browser sessions yet.</div>';
}
async function showView(view) {
  state.view = view;
  document
    .querySelectorAll(".view")
    .forEach((node) => (node.hidden = node.id !== `${view}-view`));
  document
    .querySelectorAll("[data-view]")
    .forEach((node) =>
      node.classList.toggle("active", node.dataset.view === view),
    );
  $("#page-label").textContent = names[view];
  try {
    if (view === "connections") await renderConnections();
    if (view === "devices") await renderDevices();
  } catch (error) {
    toast(error.message);
  }
}
document.addEventListener("click", (event) => {
  const task = event.target.closest("[data-task]");
  if (task) openTask(task.dataset.task);
  const nav = event.target.closest("[data-view]");
  if (nav) showView(nav.dataset.view);
  const example = event.target.closest("[data-example]");
  if (example) {
    const goals = {
      email: "Email someone about our project update",
      whatsapp:
        "Send a WhatsApp message to someone saying I will be 10 minutes late",
      amazon:
        "Find a black T-shirt on Amazon India, size M, under ₹1,000 and add it to my cart",
    };
    $("#goal").value = goals[example.dataset.example];
    state.scenario = null;
    $("#goal").focus();
  }
  const connection = event.target.closest("[data-connect]");
  if (connection) {
    connection.disabled = true;
    api(`/connections/browser/${connection.dataset.connect}`, "POST")
      .then((result) => toast(result.message))
      .catch((error) => toast(error.message))
      .finally(() => (connection.disabled = false));
  }
  const scenario = event.target.closest("[data-scenario]");
  if (scenario) {
    state.scenario = scenario.dataset.scenario;
    $("#goal").value = scenarioGoals[state.scenario];
    $("#goal").focus();
  }
});
$("#goal").addEventListener("input", () => (state.scenario = null));
$("#goal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  state.creating = true;
  updateLaunch();
  try {
    const task = await api("/tasks", "POST", {
      goal: $("#goal").value.trim(),
      mode: $("#task-mode").value,
      scenario: $("#task-mode").value === "sandbox" ? state.scenario : null,
      failure_mode:
        $("#task-mode").value === "sandbox" ? $("#failure-mode").value : "none",
    });
    state.tasks = [task, ...state.tasks.filter((old) => old.id !== task.id)];
    renderTasks();
    openTask(task.id);
    $("#goal").value = "";
    state.scenario = null;
  } catch (error) {
    toast(error.message);
  } finally {
    state.creating = false;
    updateLaunch();
  }
});
$("#task-detail").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const task = allTasks().find((task) => task.id === state.selected);
  if (!task) return;
  button.disabled = true;
  try {
    const updated = await api(
      `/tasks/${task.id}/${button.dataset.action}`,
      "POST",
      {
        revision: task.revision,
        ...(button.dataset.action === "retry"
          ? { clear_fault: task.mode === "sandbox" }
          : {}),
      },
    );
    state.tasks = state.tasks.map((item) =>
      item.id === updated.id ? updated : item,
    );
    state.older = state.older.map((item) =>
      item.id === updated.id ? updated : item,
    );
    renderTasks();
    renderDetail();
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
});
$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  token = $("#access-token").value.trim();
  $("#auth-error").textContent = "";
  try {
    await start();
    sessionStorage.setItem("jarvis-token", token);
    $("#access-token").value = "";
    $("#auth-dialog").close();
  } catch (error) {
    $("#auth-error").textContent = error.message;
  }
});
$("#auth-dialog").addEventListener("cancel", (event) => event.preventDefault());
$("#close-drawer").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.selected) closeDrawer();
});
$("#lock-button").addEventListener("click", lock);
$("#show-history").addEventListener("click", () => showView("history"));
$("#history-search").addEventListener("input", renderTasks);
$("#load-more").addEventListener("click", async () => {
  try {
    const tasks = allTasks();
    const older = await api("/tasks?before=" + tasks[tasks.length - 1].id);
    state.older.push(...older);
    renderTasks();
    if (older.length < 50) $("#load-more").hidden = true;
  } catch (error) {
    toast(error.message);
  }
});
renderTasks();
if (token)
  start().catch((error) => {
    lock();
    $("#auth-error").textContent = error.message;
  });
else lock();

$("#task-mode").addEventListener("change", () => {
  const live = $("#task-mode").value === "live";
  state.scenario = null;
  $("#goal").value = "";
  $("#fault-picker").hidden = live;
  $("#sandbox-scenarios").hidden = live;
  $("#live-capabilities").hidden = !live;
  updateLaunch();
  $("#runtime-banner").hidden = !live;
  $("#goal").placeholder = live
    ? "Email Priya that I’ll send our project report tomorrow…"
    : "Try “Prepare my daily brief”…";
  $("#mode-note").textContent = live
    ? "Gmail, WhatsApp, Amazon India and local notes. Review messages and exact cart items before Jarvis acts."
    : "Sandbox uses fixed sample data. No external messages are sent.";
});
$("#task-detail").addEventListener("input", (event) => {
  if (event.target.closest("#draft-form")) {
    const approve = $('#task-detail [data-action="approve"]');
    if (approve) {
      approve.disabled = true;
      approve.textContent = "Save draft changes before sending";
    }
  }
});
$("#task-detail").addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = allTasks().find((item) => item.id === state.selected);
  if (!task) return;
  const form = event.target;
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const route = form.id === "draft-form" ? "draft" : "answer";
    const data =
      form.id === "draft-form"
        ? {
            recipient: $("#draft-recipient").value,
            subject: $("#draft-subject").value,
            body: $("#draft-body").value,
          }
        : { text: $("#answer-text").value };
    const updated = await api(`/tasks/${task.id}/${route}`, "POST", {
      revision: task.revision,
      ...data,
    });
    state.tasks = state.tasks.map((item) =>
      item.id === updated.id ? updated : item,
    );
    state.older = state.older.map((item) =>
      item.id === updated.id ? updated : item,
    );
    renderTasks();
    renderDetail();
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
});
$("#verify-gmail").addEventListener("click", async () => {
  const button = $("#verify-gmail");
  button.disabled = true;
  $("#gmail-result").textContent =
    "Checking the signed-in Gmail account in Brave…";
  try {
    const result = await api("/connections/gmail/verify", "POST");
    $("#gmail-result").textContent = "Gmail ready in Brave: " + result.address;
    await renderConnections();
  } catch (error) {
    $("#gmail-result").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#install-model").addEventListener("click", async () => {
  $("#install-model").disabled = true;
  try {
    await api("/model/install", "POST");
    await refreshRuntime();
  } catch (error) {
    toast(error.message);
    $("#install-model").disabled = false;
  }
});
