import asyncio
from datetime import timedelta

import pytest

from jarvis.models import StepStatus, TaskStatus, now
from jarvis.store import Conflict, MongoStore
from jarvis.tools import TOOLS, Tool, ToolFailure
from tests.conftest import create_and_approve, run_queued


async def test_four_app_execution_requires_exact_message_approval(client, store, engine):
    created = await create_and_approve(client)
    task = await run_queued(store, engine)
    assert task.id == created["id"]
    assert task.status == TaskStatus.AWAITING_APPROVAL
    assert all(step.status == StepStatus.VERIFIED for step in task.steps[:3])
    assert task.steps[3].attempts == 0
    assert await store.get_artifact(f"{task.id}:s4") is None
    assert "Priya requested" in task.steps[3].resolved_args["body"]
    response = await client.post(f"/api/tasks/{task.id}/approve", json={"revision": task.revision})
    assert response.status_code == 200
    task = await run_queued(store, engine)
    assert task.status == TaskStatus.SUCCEEDED
    assert all(step.verdict == "VERIFIED" for step in task.steps)
    assert task.steps[3].approved_args == task.steps[3].resolved_args
    assert len([event for event in task.events if event.type == "step.verified"]) == 4


@pytest.mark.parametrize(
    "fault,expected,kind",
    [
        ("auth", TaskStatus.NEEDS_USER, "AUTH"),
        ("silent", TaskStatus.PARTIAL, "LOGIC"),
        ("wrong_data", TaskStatus.PARTIAL, "LOGIC"),
    ],
)
async def test_injected_failures_never_report_success(client, store, engine, fault, expected, kind):
    await create_and_approve(client, failure_mode=fault)
    task = await run_queued(store, engine)
    assert task.status == expected
    assert task.steps[1].verdict == "FAILED"
    assert task.steps[1].error.kind == kind
    assert task.steps[2].attempts == 0
    assert task.steps[3].attempts == 0


async def test_transient_failure_retries_once_without_duplicate_writes(client, store, engine):
    await create_and_approve(client, scenario="notes", failure_mode="transient")
    task = await run_queued(store, engine)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.steps[0].attempts == 2
    assert await store.db.artifacts.count_documents({}) == 2
    assert any(event.type == "step.retrying" for event in task.events)


async def test_retry_preserves_verified_steps(client, store, engine):
    await create_and_approve(client, failure_mode="auth")
    task = await run_queued(store, engine)
    response = await client.post(
        f"/api/tasks/{task.id}/retry",
        json={
            "revision": task.revision,
            "clear_fault": True,
        },
    )
    assert response.status_code == 200
    task = await run_queued(store, engine)
    assert task.steps[0].attempts == 1
    assert task.steps[1].attempts == 2
    assert task.status == TaskStatus.AWAITING_APPROVAL


async def test_stale_approval_and_write_are_rejected(client, store):
    created = await create_and_approve(client)
    response = await client.post(f"/api/tasks/{created['id']}/approve", json={"revision": 0})
    assert response.status_code == 409
    first = await store.get(created["id"])
    second = await store.get(created["id"])
    first.summary = "First writer"
    await store.save(first)
    with pytest.raises(Conflict):
        await store.save(second)


async def test_two_workers_cannot_claim_same_task(client, store):
    await create_and_approve(client)
    claims = await asyncio.gather(store.claim("worker-a"), store.claim("worker-b"))
    assert sum(task is not None for task in claims) == 1


async def test_restart_recovers_write_completed_before_checkpoint(client, store, engine):
    created = await create_and_approve(client, scenario="notes")
    task = await store.claim("dead-worker")
    step = task.steps[0]
    step.status = StepStatus.RUNNING
    step.attempts = 1
    args = TOOLS[step.tool].validate(step.args)
    await TOOLS[step.tool].run(args, f"{task.id}:{step.id}", store)
    await store.save(task)
    await store.db.tasks.update_one(
        {"_id": task.id},
        {
            "$set": {
                "lease_until": now() - timedelta(seconds=1),
            }
        },
    )
    # A fresh client represents a new API process; no Python memory is shared.
    restarted = MongoStore(store.settings)
    try:
        assert (await restarted.get(created["id"])).steps[0].status == StepStatus.RUNNING
        recovered = await restarted.claim(engine.owner)
        await engine.execute(recovered)
    finally:
        await restarted.close()
    task = await store.get(created["id"])
    assert task.status == TaskStatus.SUCCEEDED
    assert task.steps[0].attempts == 1
    assert await store.db.artifacts.count_documents({}) == 2
    assert any(event.data.get("recovered") for event in task.events)


async def test_cancellation_stops_later_steps_and_survives_worker_save(
    client, store, engine, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()
    original = Tool.run

    async def slow_run(self, args, key, store):
        entered.set()
        await release.wait()
        return await original(self, args, key, store)

    monkeypatch.setattr(Tool, "run", slow_run)
    created = await create_and_approve(client, scenario="notes")
    task = await store.claim(engine.owner)
    running = asyncio.create_task(engine.execute(task))
    await asyncio.wait_for(entered.wait(), timeout=3)
    response = await client.post(f"/api/tasks/{created['id']}/cancel")
    assert response.status_code == 200
    release.set()
    await running
    persisted = await store.get(task.id)
    assert persisted.status == TaskStatus.CANCELLED
    assert persisted.steps[1].attempts == 0
    # The cancellation explicitly warns that already in-flight actions may complete.
    assert await store.get_artifact(f"{task.id}:s1") is not None
    assert any(event.type == "step.result_after_cancel" for event in persisted.events)


async def test_retry_budget_exhaustion_halts(client, store, engine, monkeypatch):
    from jarvis.models import FailureClass

    async def always_timeout(self, args, key, store):
        raise ToolFailure(FailureClass.TRANSIENT, "Persistent timeout")

    monkeypatch.setattr(Tool, "run", always_timeout)
    await create_and_approve(client, scenario="notes")
    task = await run_queued(store, engine)
    assert task.status == TaskStatus.FAILED
    assert task.steps[0].attempts == 3
    response = await client.post(f"/api/tasks/{task.id}/retry", json={"revision": task.revision})
    assert response.status_code == 409


async def test_unknown_verification_is_not_success(client, store, engine, monkeypatch):
    async def unknown(self, args, result, store):
        return None

    monkeypatch.setattr(Tool, "verify", unknown)
    await create_and_approve(client, scenario="notes")
    task = await run_queued(store, engine)
    assert task.status == TaskStatus.FAILED
    assert task.steps[0].verdict == "FAILED"


async def test_unexpected_connector_exception_is_reported(client, store, engine, monkeypatch):
    async def broken(self, args, key, store):
        raise RuntimeError("A sensitive provider error that must not leak")

    monkeypatch.setattr(Tool, "run", broken)
    await create_and_approve(client, scenario="notes")
    task = await run_queued(store, engine)
    assert task.status == TaskStatus.FAILED
    assert task.steps[0].error.kind == "UNRECOVERABLE"
    assert "sensitive" not in task.model_dump_json()
