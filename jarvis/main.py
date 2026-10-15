import asyncio
import hmac
import os
import tempfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal

from dotenv import set_key
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from playwright.async_api import Error as BrowserError
from pymongo.errors import PyMongoError

from jarvis.browser_runtime import session as browser_session
from jarvis.config import Settings, get_settings
from jarvis.email_tools import EmailArgs
from jarvis.engine import MAX_ATTEMPTS, TOOL_REGISTRY, Engine
from jarvis.firefox_runtime import firefox_session
from jarvis.gmail_browser import inspect_gmail_account
from jarvis.model_runtime import ModelRuntime
from jarvis.models import (
    Answer,
    Approval,
    CreateTask,
    DeviceInput,
    EditEmail,
    StepStatus,
    Task,
    TaskStatus,
)
from jarvis.planner import SCENARIOS, plan
from jarvis.store import Conflict, MongoStore
from jarvis.tools import ToolFailure, resolve_args

STATIC = Path(__file__).parent / "static"
bearer = HTTPBearer(auto_error=False)


class Retry(Approval):
    clear_fault: bool = False


def persist_gmail(address: str) -> None:
    path = Path(".env").resolve()
    original = path.read_text() if path.exists() else ""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env-")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(original)
        set_key(temporary, "JARVIS_EMAIL_ADDRESS", address)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def public_task(task: Task) -> dict:
    return task.model_dump(mode="json", exclude={"lease_owner", "lease_until"})


def create_app(
    settings: Settings | None = None, store: MongoStore | None = None, run_worker: bool = True
) -> FastAPI:
    settings = settings or get_settings()
    store = store or MongoStore(settings)
    model_runtime = ModelRuntime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.start()
        worker = asyncio.create_task(Engine(store, settings).serve()) if run_worker else None
        yield
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        await model_runtime.close()
        if hasattr(store, "firefox_session"):
            await store.firefox_session.close()
        if hasattr(store, "browser_session"):
            await store.browser_session.close()
        await store.close()

    app = FastAPI(title="Jarvis", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.model_runtime = model_runtime
    app.state.gmail_session_verified = False

    def valid_token(token: str) -> bool:
        return hmac.compare_digest(
            token.encode(), settings.access_token.get_secret_value().encode()
        )

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is None or not valid_token(credentials.credentials):
            raise HTTPException(
                401, "Enter the workspace access token.", headers={"WWW-Authenticate": "Bearer"}
            )

    auth = [Depends(authenticate)]

    async def get_task(task_id: str) -> Task:
        task = await store.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found.")
        return task

    async def save(task: Task) -> None:
        try:
            await store.save(task)
        except Conflict as exc:
            raise HTTPException(
                409, "Task changed on another device. Reload and try again."
            ) from exc

    @app.get("/api/health")
    async def health():
        try:
            await store.client.admin.command("ping")
        except PyMongoError as exc:
            raise HTTPException(503, "MongoDB unavailable.") from exc
        return {"status": "ok", "database": "mongodb", "mode": "live_and_sandbox"}

    @app.get("/api/runtime", dependencies=auth)
    async def runtime():
        return {
            **await model_runtime.status(),
            "email_configured": bool(settings.email_address),
            "email_address": settings.email_address,
            "browser_routes": {"whatsapp": "firefox", "gmail": "brave", "amazon": "chromium"},
            "live_tools": [name for name, tool in TOOL_REGISTRY.items() if tool.mode == "live"],
        }

    @app.post("/api/model/install", dependencies=auth)
    async def install_model():
        try:
            return await model_runtime.install()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/scenarios", dependencies=auth)
    async def scenarios():
        return SCENARIOS

    @app.get("/api/tools", dependencies=auth)
    async def tools():
        return [
            {
                "name": tool.name,
                "risk": tool.risk,
                "description": tool.description,
                "mode": tool.mode,
                "input_schema": tool.schema.model_json_schema(),
            }
            for tool in TOOL_REGISTRY.values()
        ]

    @app.post("/api/tasks", dependencies=auth, status_code=201)
    async def create(request: CreateTask):
        if not request.goal.strip():
            raise HTTPException(422, "Enter a goal.")
        try:
            if request.mode == "live":
                if request.failure_mode != "none" or request.scenario:
                    raise ValueError(
                        "Demo scenarios and injected failures only apply in sandbox mode."
                    )
                if not (await model_runtime.status())["model_available"]:
                    raise HTTPException(
                        409, "Local model is not ready. Use Download / resume model first."
                    )
                task = Task(
                    goal=request.goal.strip(),
                    scenario="agent",
                    mode="live",
                    steps=[],
                    status=TaskStatus.QUEUED,
                    summary="Queued for local AI planning.",
                )
                task.record("agent.created", task.summary)
            else:
                task = plan(request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        await store.insert(task)
        return public_task(task)

    @app.get("/api/tasks", dependencies=auth)
    async def history(before: str | None = None, limit: int = Query(default=50, ge=1, le=100)):
        return [public_task(task) for task in await store.list(before, limit)]

    @app.get("/api/tasks/{task_id}", dependencies=auth)
    async def detail(task_id: str):
        return public_task(await get_task(task_id))

    @app.post("/api/tasks/{task_id}/approve", dependencies=auth)
    async def approve(task_id: str, request: Approval):
        task = await get_task(task_id)
        if task.revision != request.revision or task.status != TaskStatus.AWAITING_APPROVAL:
            raise HTTPException(409, "This approval is stale. Review the current task.")
        pending = next((step for step in task.steps if step.status != StepStatus.VERIFIED), None)
        if pending and pending.resolved_args is not None and pending.risk == "write_irreversible":
            if pending.tool == "email.send" and not bool(settings.email_address):
                raise HTTPException(
                    409,
                    "Open Gmail in Brave and verify its session in Connections before approving.",
                )
            if (
                pending.tool == "email.send"
                and pending.args.get("sender") != settings.email_address
            ):
                pending.args["sender"] = settings.email_address
                pending.resolved_args = dict(pending.args)
                task.summary = "Sending account updated. Review the sender and approve again."
                await save(task)
                return public_task(task)
            tool = TOOL_REGISTRY.get(pending.tool)
            if tool is None or tool.mode != task.mode:
                raise HTTPException(409, "This task uses an unavailable tool. Create a new task.")
            try:
                args = tool.validate(resolve_args(pending.args, task.steps))
            except ToolFailure as exc:
                raise HTTPException(409, str(exc)) from exc
            if args != pending.resolved_args:
                raise HTTPException(409, "The message changed. Review it again.")
            pending.approved = True
            pending.approved_args = args
            task.record(
                "approval.granted", f"Exact {task.mode} message approved.", pending.id, payload=args
            )
        else:
            task.record(
                "plan.approved", "Plan approved. External-facing steps need separate approval."
            )
        task.status = TaskStatus.QUEUED
        task.summary = "Queued for execution."
        await save(task)
        return public_task(task)

    @app.post("/api/tasks/{task_id}/cancel", dependencies=auth)
    async def cancel(task_id: str):
        task = await get_task(task_id)
        if task.status in (TaskStatus.SUCCEEDED, TaskStatus.CANCELLED):
            raise HTTPException(409, "This task is already finished.")
        task.status = TaskStatus.CANCELLED
        task.lease_owner = None
        task.lease_until = None
        task.summary = "Cancelled. Completed writes remain; an in-flight action may still finish."
        task.record("task.cancelled", task.summary)
        await save(task)
        return public_task(task)

    @app.post("/api/tasks/{task_id}/retry", dependencies=auth)
    async def retry(task_id: str, request: Retry):
        task = await get_task(task_id)
        if task.revision != request.revision or task.status not in (
            TaskStatus.FAILED,
            TaskStatus.PARTIAL,
            TaskStatus.NEEDS_USER,
        ):
            raise HTTPException(409, "This task cannot be retried in its current state.")
        step = next((step for step in task.steps if step.status == StepStatus.FAILED), None)
        if task.mode == "live" and step is None:
            if task.agent_turns >= 15 or task.question:
                raise HTTPException(409, "Answer the question or create a new task.")
            task.status = TaskStatus.QUEUED
            task.summary = "Local AI planning queued again."
            task.record("agent.retry", task.summary)
            await save(task)
            return public_task(task)
        if step is None or step.attempts >= MAX_ATTEMPTS:
            raise HTTPException(409, "The step's attempt budget has been exhausted.")
        if step.error and not step.error.retry_allowed:
            raise HTTPException(
                409,
                "Action outcome is uncertain. Use read-back verification; repeating it is blocked.",
            )
        step.status = StepStatus.PENDING
        step.verdict = None
        step.error = None
        if request.clear_fault:
            task.failure_mode = "none"
        task.status = TaskStatus.QUEUED
        task.summary = "Retry queued; previously verified steps will not run again."
        task.record("task.retry_requested", task.summary, step.id, clear_fault=request.clear_fault)
        await save(task)
        return public_task(task)

    @app.post("/api/tasks/{task_id}/answer", dependencies=auth)
    async def answer(task_id: str, request: Answer):
        task = await get_task(task_id)
        if (
            task.mode != "live"
            or task.status != TaskStatus.NEEDS_USER
            or not task.question
            or task.revision != request.revision
            or len(task.answers) >= 2
        ):
            raise HTTPException(409, "There is no current question to answer.")
        if not request.text.strip():
            raise HTTPException(422, "Enter an answer.")
        task.answers.append(request.text.strip())
        task.question = None
        task.status = TaskStatus.QUEUED
        task.record("agent.answer", "Clarification received.", answer=request.text.strip())
        await save(task)
        return public_task(task)

    @app.post("/api/tasks/{task_id}/draft", dependencies=auth)
    async def edit_draft(task_id: str, request: EditEmail):
        task = await get_task(task_id)
        step = next((item for item in task.steps if item.status != StepStatus.VERIFIED), None)
        if (
            task.revision != request.revision
            or task.status != TaskStatus.AWAITING_APPROVAL
            or not step
            or step.tool != "email.send"
            or step.attempts
        ):
            raise HTTPException(409, "Only an unsent email draft can be edited.")
        try:
            args = EmailArgs.model_validate(request.model_dump(exclude={"revision"})).model_dump()
        except ValueError as exc:
            raise HTTPException(422, "Enter a valid email address, subject, and body.") from exc
        step.args = args
        args["sender"] = settings.email_address
        step.resolved_args = args
        step.approved = False
        step.approved_args = None
        task.record(
            "draft.edited", "Draft updated; approval is still required.", step.id, payload=args
        )
        await save(task)
        return public_task(task)

    @app.post("/api/tasks/{task_id}/recheck", dependencies=auth)
    async def recheck(task_id: str, request: Approval):
        task = await get_task(task_id)
        step = next((item for item in task.steps if item.status == StepStatus.FAILED), None)
        if (
            task.revision != request.revision
            or not step
            or step.tool not in ("email.send", "whatsapp.send", "amazon.add_to_cart")
            or task.status not in (TaskStatus.NEEDS_USER, TaskStatus.PARTIAL, TaskStatus.FAILED)
        ):
            raise HTTPException(409, "No failed external action is available for read-back.")
        step.status = StepStatus.VERIFYING
        step.result = step.result or {"id": f"{task.id}:{step.id}"}
        task.status = TaskStatus.QUEUED
        task.record(
            "action.recheck",
            "Read-only provider verification queued; the action will not be repeated.",
            step.id,
        )
        await save(task)
        return public_task(task)

    @app.post("/api/connections/gmail/verify", dependencies=auth)
    async def connect_gmail():
        try:
            async with asyncio.timeout(45):
                address = await inspect_gmail_account(store)
            await asyncio.to_thread(persist_gmail, address)
        except (ToolFailure, TimeoutError) as exc:
            raise HTTPException(
                409, str(exc) or "Gmail verification timed out. Sign in in Brave and retry."
            ) from exc
        except BrowserError as exc:
            raise HTTPException(
                409, "Gmail could not be read in Brave. Reopen it and verify again."
            ) from exc
        except OSError as exc:
            raise HTTPException(503, "Cannot save the verified Gmail address to .env.") from exc
        settings.email_address = address
        app.state.gmail_session_verified = True
        return {"status": "connected", "address": address, "browser": "brave"}

    @app.post("/api/connections/browser/{provider}", dependencies=auth)
    async def connect_browser(provider: Literal["whatsapp", "amazon", "gmail"]):
        try:
            async with asyncio.timeout(40):
                if provider == "whatsapp":
                    return await firefox_session(store).connect()
                return await browser_session(store).connect(provider)
        except (ToolFailure, TimeoutError) as exc:
            raise HTTPException(
                409, str(exc) or "Browser setup timed out. Try opening it again."
            ) from exc
        except BrowserError as exc:
            raise HTTPException(
                409, "The account page could not open. Check your network and try setup again."
            ) from exc

    @app.get("/api/connections", dependencies=auth)
    async def connections():
        return [
            {
                "id": "live_gmail",
                "name": "Gmail · Brave browser",
                "browser": "brave",
                "status": "configured" if app.state.gmail_session_verified else "verify_session",
                "connected": app.state.gmail_session_verified,
                "address": settings.email_address,
            },
            {
                "id": "whatsapp",
                "name": "WhatsApp · Firefox",
                "browser": "firefox",
                "status": "browser_setup",
                "connected": False,
            },
            {
                "id": "amazon",
                "name": "Amazon India",
                "browser": "chromium",
                "status": "browser_setup",
                "connected": False,
            },
            {
                "id": "ollama",
                "name": "Ollama · local AI",
                "status": "local",
                "connected": False,
                "model": settings.ollama_model,
            },
        ] + [
            {"id": key, "name": name, "status": "sandbox", "connected": False}
            for key, name in [
                ("gmail", "Gmail"),
                ("calendar", "Google Calendar"),
                ("slack", "Slack"),
                ("notes", "Notes"),
            ]
        ]

    @app.post("/api/devices/heartbeat", dependencies=auth)
    async def heartbeat(request: DeviceInput):
        await store.heartbeat(request.id, request.name)
        return {"ok": True}

    @app.get("/api/devices", dependencies=auth)
    async def devices():
        return jsonable_encoder(await store.devices())

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        # Authenticate in the first frame: tokens never appear in URL/query logs.
        await ws.accept()
        try:
            async with asyncio.timeout(5):
                frame = await ws.receive_json()
            if not isinstance(frame, dict) or not isinstance(frame.get("token"), str):
                await ws.close(code=4401)
                return
            if not valid_token(frame["token"]):
                await ws.close(code=4401)
                return
            last_signature = None
            while True:
                tasks = await store.list(limit=50)
                signature = [(task.id, task.revision) for task in tasks]
                if signature != last_signature:
                    await ws.send_json(
                        {"type": "tasks.snapshot", "tasks": [public_task(task) for task in tasks]}
                    )
                    last_signature = signature
                # Receive timeout also detects closed, idle clients promptly.
                with suppress(TimeoutError):
                    async with asyncio.timeout(0.5):
                        await ws.receive_text()
        except (WebSocketDisconnect, TimeoutError, ValueError):
            with suppress(RuntimeError):
                await ws.close(code=4401)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
