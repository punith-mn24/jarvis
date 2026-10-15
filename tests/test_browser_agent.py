import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from playwright.async_api import Error as BrowserError

from jarvis import amazon_tools as browser_tools
from jarvis.agent import Decision, action_question, advance
from jarvis.amazon_tools import CartArgs, price_number
from jarvis.browser_tools import BROWSER_TOOLS
from jarvis.model_runtime import ModelRuntime
from jarvis.models import Failure, FailureClass, Step, StepStatus, Task, TaskStatus
from jarvis.tools import ToolFailure
from jarvis.whatsapp_tools import WhatsAppArgs
from tests.test_live_agent import ScriptedPlanner, live_task


def test_phone_and_product_schema_reject_navigation_inputs():
    with pytest.raises(ValueError):
        WhatsAppArgs(phone="someone", body="Hello")
    with pytest.raises(ValueError):
        CartArgs(asin="https://other.example", title="Shirt", unit_price=500, max_price=600)
    assert price_number("₹1,299.00") == 1299
    assert price_number("Unavailable") is None


def test_phone_must_be_supplied_by_user():
    task = Task(goal="Message Priya", scenario="agent", mode="live", steps=[])
    args = {"phone": "+919876543210", "body": "Hello"}
    assert action_question("whatsapp.send", args, task)
    task.answers.append("Use +91 98765 43210")
    assert action_question("whatsapp.send", args, task) is None


def test_cart_requires_verified_exact_variant():
    task = Task(goal="Buy a shirt", scenario="agent", mode="live", steps=[])
    args = dict(
        asin="B012345678", title="Shirt", size="M", color="Black", unit_price=599, max_price=700
    )
    assert action_question("amazon.inspect", args, task)
    result = dict(asin="B012345678", title="Shirt", size="M", color="Black", price=599)
    step = Step(
        id="s1",
        title="Inspect",
        tool="amazon.inspect",
        args={},
        risk="read",
        expected_outcome="Read variant",
        status=StepStatus.VERIFIED,
        result=result,
    )
    task.steps.append(step)
    assert action_question("amazon.add_to_cart", args, task) is None
    assert action_question("amazon.add_to_cart", {**args, "size": "L"}, task)
    step.status = StepStatus.FAILED
    assert action_question("amazon.add_to_cart", args, task)


async def test_whatsapp_draft_requires_approval_and_cannot_be_edited_as_email(client, store):
    task = await live_task(store, "WhatsApp +919876543210 saying hello")
    decision = Decision(
        action="tool",
        tool="whatsapp.send",
        message="Send hello",
        arguments_json='{"phone":"+919876543210","body":"hello"}',
    )
    await advance(task, store, ScriptedPlanner(decision))
    saved = await store.get(task.id)
    assert saved.status == TaskStatus.AWAITING_APPROVAL
    assert saved.steps[0].attempts == 0
    assert await store.db.browser_dispatches.count_documents({}) == 0
    response = await client.post(
        f"/api/tasks/{task.id}/draft",
        json={
            "revision": saved.revision,
            "recipient": "x@example.com",
            "subject": "x",
            "body": "x",
        },
    )
    assert response.status_code == 409
    response = await client.post(f"/api/tasks/{task.id}/approve", json={"revision": saved.revision})
    assert response.status_code == 200
    assert response.json()["steps"][0]["approved_args"] == saved.steps[0].args


@pytest.mark.parametrize("name", ["whatsapp.send", "amazon.add_to_cart"])
async def test_uncertain_browser_action_only_allows_readback(name, client, store):
    task = await live_task(store)
    task.status = TaskStatus.FAILED
    task.steps = [
        Step(
            id="s1",
            title="External action",
            tool=name,
            args={},
            risk="write_irreversible",
            expected_outcome="Read back",
            status=StepStatus.FAILED,
            attempts=1,
            error=Failure(kind=FailureClass.LOGIC, message="Unknown", retry_allowed=False),
        )
    ]
    task.lease_owner = None
    await store.save(task)
    response = await client.post(f"/api/tasks/{task.id}/retry", json={"revision": task.revision})
    assert response.status_code == 409
    response = await client.post(f"/api/tasks/{task.id}/recheck", json={"revision": task.revision})
    assert response.status_code == 200
    assert response.json()["steps"][0]["status"] == "VERIFYING"


async def test_whatsapp_duplicate_submission_is_blocked(store):
    store.firefox_session = SimpleNamespace(lock=asyncio.Lock(), call=AsyncMock())
    await store.db.browser_dispatches.insert_one({"_id": "task:s1"})
    with pytest.raises(ToolFailure) as error:
        await BROWSER_TOOLS["whatsapp.send"].run(
            {"phone": "+919876543210", "body": "Hello"}, "task:s1", store
        )
    assert not error.value.retry_allowed
    assert "earlier" in str(error.value)


async def test_cart_readback_requires_exact_price_and_variant(store, monkeypatch):
    page = SimpleNamespace(goto=AsyncMock())
    store.browser_session = SimpleNamespace(lock=asyncio.Lock(), page=AsyncMock(return_value=page))
    monkeypatch.setattr(browser_tools, "guard_amazon", AsyncMock())
    monkeypatch.setattr(browser_tools, "open_cart", AsyncMock())
    snapshot = AsyncMock(return_value={"asin": "B012345678", "quantity": 1, "unit_price": 650})
    monkeypatch.setattr(browser_tools, "cart_snapshot", snapshot)
    args = dict(
        asin="B012345678", title="Shirt", size="M", color="Black", unit_price=599, max_price=700
    )
    with pytest.raises(ToolFailure) as error:
        await BROWSER_TOOLS["amazon.add_to_cart"].verify(args, {"id": "task:s1"}, store)
    assert not error.value.retry_allowed
    snapshot.return_value["unit_price"] = 599
    assert await BROWSER_TOOLS["amazon.add_to_cart"].verify(args, {"id": "task:s1"}, store)
    snapshot.return_value = None
    with pytest.raises(ToolFailure):
        await BROWSER_TOOLS["amazon.add_to_cart"].verify(args, {"id": "task:s1"}, store)


async def test_readback_browser_crash_is_actionable_not_a_worker_loop(store):
    store.browser_session = SimpleNamespace(
        lock=asyncio.Lock(), page=AsyncMock(side_effect=BrowserError("closed"))
    )
    with pytest.raises(ToolFailure) as error:
        await BROWSER_TOOLS["amazon.add_to_cart"].verify({}, {"id": "task:s1"}, store)
    assert not error.value.retry_allowed
    assert "verify again" in str(error.value)


async def test_missing_model_blocks_task_before_inserting(client, store, monkeypatch):
    monkeypatch.setattr(ModelRuntime, "status", AsyncMock(return_value={"model_available": False}))
    response = await client.post("/api/tasks", json={"goal": "Save a note", "mode": "live"})
    assert response.status_code == 409
    assert "Download / resume" in response.json()["detail"]
    assert await store.db.tasks.count_documents({}) == 0
    monkeypatch.setattr(ModelRuntime, "status", AsyncMock(return_value={"model_available": True}))
    response = await client.post("/api/tasks", json={"goal": "Save a note", "mode": "live"})
    assert response.status_code == 201


async def test_model_installer_reuses_active_job_and_sanitizes_errors(store, monkeypatch):
    manager = ModelRuntime(store.settings)
    manager.status = AsyncMock(return_value={"model_available": False, "reachable": True})
    hold = asyncio.Event()
    manager._download = AsyncMock(side_effect=lambda: hold.wait())

    # Use a coroutine directly so the job remains pending until cancellation.
    async def waiting():
        await hold.wait()

    manager._download = waiting
    await manager.install()
    first = manager.job
    await manager.install()
    assert manager.job is first
    await manager.close()
    assert first.cancelled()


async def test_runtime_handles_offline_ollama(store, monkeypatch):
    async def offline(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.AsyncClient, "get", offline)
    manager = ModelRuntime(store.settings)
    assert not (await manager.status())["model_available"]
    with pytest.raises(ValueError, match="offline"):
        await manager.install()


@pytest.mark.parametrize("submitted", [False, True])
async def test_browser_navigation_error_only_retryable_before_submission(store, submitted):
    page = SimpleNamespace(goto=AsyncMock(side_effect=BrowserError("network")))
    store.browser_session = SimpleNamespace(lock=asyncio.Lock(), page=AsyncMock(return_value=page))
    if submitted:
        await store.db.browser_dispatches.insert_one({"_id": "task:s1"})
    with pytest.raises(ToolFailure) as error:
        await BROWSER_TOOLS["amazon.add_to_cart"].run({"asin": "B012345678"}, "task:s1", store)
    assert error.value.retry_allowed is not submitted


async def test_amazon_dispatch_fence_prevents_second_add(store, monkeypatch):
    click = AsyncMock()
    locator = SimpleNamespace(click=click, count=AsyncMock(return_value=0))
    locator.first = locator
    page = SimpleNamespace(goto=AsyncMock(), locator=lambda *args: locator)
    store.browser_session = SimpleNamespace(lock=asyncio.Lock(), page=AsyncMock(return_value=page))
    product = dict(
        asin="B012345678",
        title="Shirt",
        size="M",
        color="Black",
        price=599,
        sizes=[{"label": "M"}],
        colors=[{"label": "Black"}],
        can_add=True,
    )
    monkeypatch.setattr(browser_tools, "guard_amazon", AsyncMock())
    monkeypatch.setattr(browser_tools, "open_cart", AsyncMock())
    monkeypatch.setattr(browser_tools, "select_option", AsyncMock())
    monkeypatch.setattr(browser_tools, "wait_product", AsyncMock(return_value=product))
    monkeypatch.setattr(browser_tools, "amazon_product_snapshot", AsyncMock(return_value=product))
    monkeypatch.setattr(browser_tools, "cart_snapshot", AsyncMock(return_value=None))
    await store.db.browser_observations.insert_one({"_id": "task:B012345678", **product})
    args = dict(
        asin="B012345678", title="Shirt", size="M", color="Black", unit_price=599, max_price=700
    )
    result = await BROWSER_TOOLS["amazon.add_to_cart"].run(args, "task:s1", store)
    assert result["id"] == "task:s1"
    assert click.await_count == 1
    with pytest.raises(ToolFailure) as error:
        await BROWSER_TOOLS["amazon.add_to_cart"].run(args, "task:s1", store)
    assert not error.value.retry_allowed
    assert click.await_count == 1
