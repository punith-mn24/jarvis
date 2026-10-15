import asyncio
import logging
import random
from time import monotonic
from uuid import uuid4

from pymongo.errors import PyMongoError

from jarvis.agent import OllamaPlanner, advance
from jarvis.config import Settings
from jarvis.live_tools import LIVE_TOOLS
from jarvis.models import Failure, FailureClass, Step, StepStatus, Task, TaskStatus
from jarvis.store import Conflict, MongoStore
from jarvis.tools import TOOLS, ToolFailure, resolve_args, run_with_fault

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
TOOL_REGISTRY = {**TOOLS, **LIVE_TOOLS}


class Engine:
    def __init__(self, store: MongoStore, settings: Settings):
        self.store = store
        self.settings = settings
        self.owner = uuid4().hex
        self.planner = OllamaPlanner(settings)

    async def serve(self) -> None:
        while True:
            try:
                task = await self.store.claim(self.owner)
                if task:
                    if task.mode == "live" and all(
                        step.status == StepStatus.VERIFIED for step in task.steps
                    ):
                        await advance(task, self.store, self.planner)
                    else:
                        await self.execute(task)
                else:
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not print connector payloads, MongoDB URIs, or credentials.
                logger.error("Worker interrupted; persisted tasks recover after lease expiry.")
                await asyncio.sleep(1)

    async def checkpoint(self, task: Task) -> None:
        await self.store.save(task)

    async def execute(self, task: Task) -> None:
        try:
            await self.store.while_leased(task, self._execute(task))
        except Conflict:
            # Ownership was revoked during long browser work. Its persisted
            # RUNNING/VERIFYING step is recovered with read-back, never a resend.
            return

    async def _execute(self, task: Task) -> None:
        try:
            task.record("task.started", f"Executing {task.mode} actions.")
            await self.checkpoint(task)
            for step in task.steps:
                if step.status == StepStatus.VERIFIED:
                    continue
                if not all(
                    any(
                        item.id == dep and item.status == StepStatus.VERIFIED for item in task.steps
                    )
                    for dep in step.depends_on
                ):
                    await self.fail(
                        task,
                        step,
                        ToolFailure(FailureClass.INPUT, "A prerequisite has not been verified."),
                    )
                    return
                tool = TOOL_REGISTRY.get(step.tool)
                if tool is None:
                    await self.fail(
                        task,
                        step,
                        ToolFailure(
                            FailureClass.INPUT,
                            "This task refers to an unavailable tool. Create a new task.",
                            False,
                        ),
                    )
                    return
                if tool.mode != task.mode:
                    await self.fail(
                        task,
                        step,
                        ToolFailure(
                            FailureClass.UNRECOVERABLE,
                            "Tool does not belong to this task's mode.",
                            False,
                        ),
                    )
                    return
                timeout = max(self.settings.tool_timeout, tool.timeout)
                if step.tool == "email.send" and step.status == StepStatus.PENDING:
                    step.args["sender"] = self.settings.email_address
                try:
                    args = tool.validate(resolve_args(step.args, task.steps))
                except ToolFailure as exc:
                    await self.fail(task, step, exc)
                    return
                step.resolved_args = args
                if tool.risk == "write_irreversible" and (
                    not step.approved or step.approved_args != args
                ):
                    step.approved = False
                    task.status = TaskStatus.AWAITING_APPROVAL
                    task.lease_owner = None
                    task.lease_until = None
                    task.summary = "Approve the exact action below before this step runs."
                    task.record("approval.required", task.summary, step.id, payload=args)
                    await self.checkpoint(task)
                    return

                # Crash recovery reads the provider before repeating a potentially completed write.
                if step.status in (StepStatus.RUNNING, StepStatus.VERIFYING):
                    result = step.result or {"id": f"{task.id}:{step.id}"}
                    try:
                        async with asyncio.timeout(timeout):
                            verified = await tool.verify(args, result, self.store)
                    except TimeoutError:
                        await self.fail(
                            task,
                            step,
                            ToolFailure(
                                FailureClass.TRANSIENT,
                                "Recovery read-back timed out. "
                                "The interrupted action is unverified.",
                                retry_allowed=tool.retry_safe,
                            ),
                        )
                        return
                    except ToolFailure as exc:
                        if not tool.retry_safe:
                            exc.retry_allowed = False
                        await self.fail(task, step, exc)
                        return
                    except (Conflict, PyMongoError):
                        raise
                    except Exception:
                        await self.fail(
                            task,
                            step,
                            ToolFailure(
                                FailureClass.UNRECOVERABLE,
                                "Recovery could not read the provider state. Reconnect and "
                                "verify again; the interrupted action is unverified.",
                                retry_allowed=tool.retry_safe,
                            ),
                        )
                        return
                    if verified:
                        step.result = result
                        await self.verified(task, step, recovered=True)
                        continue
                    if not tool.retry_safe:
                        await self.fail(
                            task,
                            step,
                            ToolFailure(
                                FailureClass.UNRECOVERABLE,
                                "Interrupted action has an uncertain outcome. "
                                "Inspect the provider.",
                                retry_allowed=False,
                            ),
                        )
                        return
                if step.attempts >= MAX_ATTEMPTS:
                    await self.fail(
                        task,
                        step,
                        ToolFailure(
                            FailureClass.LOOP,
                            "Step attempt budget exhausted. No further calls made.",
                        ),
                    )
                    return

                while step.attempts < MAX_ATTEMPTS:
                    step.status = StepStatus.RUNNING
                    step.attempts += 1
                    step.error = None
                    task.record(
                        "step.started",
                        f"Attempt {step.attempts}: {step.title}",
                        step.id,
                        tool=step.tool,
                        request=args,
                    )
                    await self.checkpoint(task)
                    await asyncio.sleep(self.settings.step_delay)
                    # Cancellation/approval changes from another device invalidate this revision.
                    current = await self.store.get(task.id)
                    if current is None or current.revision != task.revision:
                        raise Conflict()
                    try:
                        started = monotonic()
                        async with asyncio.timeout(timeout):
                            step.result = await run_with_fault(tool, args, task, step, self.store)
                            step.status = StepStatus.VERIFYING
                            task.record(
                                "step.result",
                                "Action returned; checking provider state.",
                                step.id,
                                response=step.result,
                                latency_ms=round((monotonic() - started) * 1000),
                            )
                            await self.checkpoint(task)
                            verified = await tool.verify(args, step.result, self.store)
                        if not verified:
                            raise ToolFailure(
                                FailureClass.LOGIC,
                                "Read-back did not match the expected result. "
                                "The action is not verified.",
                                retry_allowed=tool.retry_safe,
                            )
                        await self.verified(task, step)
                        break
                    except TimeoutError:
                        failure = ToolFailure(
                            FailureClass.TRANSIENT,
                            "Tool or verification timed out."
                            + (
                                " Check provider state; do not repeat the action."
                                if not tool.retry_safe
                                else ""
                            ),
                            retry_allowed=tool.retry_safe,
                        )
                    except ToolFailure as exc:
                        failure = exc
                        if step.status == StepStatus.VERIFYING and not tool.retry_safe:
                            failure.retry_allowed = False
                    except (Conflict, PyMongoError):
                        raise
                    except Exception:
                        failure = ToolFailure(
                            FailureClass.UNRECOVERABLE,
                            "Unexpected connector error. Outcome is unverified; "
                            "inspect the action.",
                            retry_allowed=tool.retry_safe,
                        )
                    if (
                        failure.kind == FailureClass.TRANSIENT
                        and step.attempts < MAX_ATTEMPTS
                        and tool.retry_safe
                    ):
                        task.record(
                            "step.retrying",
                            str(failure),
                            step.id,
                            failure_class=failure.kind,
                            attempt=step.attempts,
                        )
                        await self.checkpoint(task)
                        delay = self.settings.retry_base * 2 ** (step.attempts - 1)
                        await asyncio.sleep(delay + random.uniform(0, delay / 4))
                        continue
                    await self.fail(task, step, failure)
                    return

            if all(step.status == StepStatus.VERIFIED for step in task.steps):
                task.status = TaskStatus.QUEUED if task.mode == "live" else TaskStatus.SUCCEEDED
                task.summary = (
                    "Action verified. Ollama will check the next step."
                    if task.mode == "live"
                    else f"All {len(task.steps)} sandbox steps independently verified."
                )
                task.lease_owner = None
                task.lease_until = None
                task.record(
                    "agent.continuing" if task.mode == "live" else "task.finished", task.summary
                )
                await self.checkpoint(task)
        except Conflict:
            # A cancellation or another worker owns the newer revision. Never overwrite it.
            current = await self.store.get(task.id)
            if current and current.status == TaskStatus.CANCELLED:
                for interrupted in task.steps:
                    if interrupted.status == StepStatus.VERIFYING and interrupted.result:
                        current.record(
                            "step.result_after_cancel",
                            "An in-flight action returned after cancellation; "
                            "outcome is unverified.",
                            interrupted.id,
                            response=interrupted.result,
                        )
                try:
                    await self.store.save(current)
                except Conflict:
                    pass
            return

    async def verified(self, task: Task, step: Step, recovered: bool = False) -> None:
        step.status = StepStatus.VERIFIED
        step.verdict = "VERIFIED"
        step.error = None
        task.record(
            "step.verified",
            "Provider read-back matched the expected result.",
            step.id,
            recovered=recovered,
        )
        await self.checkpoint(task)

    async def fail(self, task: Task, step: Step, failure: ToolFailure) -> None:
        step.status = StepStatus.FAILED
        step.verdict = "FAILED"
        options = ["cancel"]
        if step.attempts < MAX_ATTEMPTS and failure.retry_allowed:
            options.insert(0, "retry")
        if failure.kind == FailureClass.AUTH:
            options.insert(0, "reconnect")
        step.error = Failure(
            kind=failure.kind,
            message=str(failure),
            options=options,
            retry_allowed=failure.retry_allowed,
        )
        if failure.kind in (FailureClass.AUTH, FailureClass.INPUT):
            task.status = TaskStatus.NEEDS_USER
        else:
            task.status = (
                TaskStatus.PARTIAL
                if any(item.status == StepStatus.VERIFIED for item in task.steps)
                else TaskStatus.FAILED
            )
        task.summary = f"Stopped at {step.title}: {failure}"
        task.lease_owner = None
        task.lease_until = None
        task.record("step.failed", str(failure), step.id, failure_class=failure.kind)
        task.record("task.finished", task.summary)
        await self.checkpoint(task)
