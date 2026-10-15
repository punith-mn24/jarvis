"""Regression coverage for long local inference and interrupted provider actions."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from jarvis.agent import (
    Decision,
    OllamaPlanner,
    advance,
    prompt_schema,
    shopping_question,
    tools_for_task,
)
from jarvis.engine import TOOL_REGISTRY
from jarvis.model_runtime import ModelRuntime
from jarvis.models import FailureClass, Step, StepStatus, Task, TaskStatus, now
from jarvis.store import Conflict
from jarvis.tools import CreateNote, Tool, ToolFailure
from tests.conftest import create_and_approve, run_queued
from tests.test_live_agent import live_task


async def test_browser_length_execution_keeps_lease(client, store, engine, monkeypatch):
    store.settings.lease_seconds = 0.3
    entered, release = asyncio.Event(), asyncio.Event()
    original = Tool.run

    async def held(self, args, key, store):
        entered.set()
        await release.wait()
        return await original(self, args, key, store)

    monkeypatch.setattr(Tool, "run", held)
    await create_and_approve(client, scenario="notes")
    task = await store.claim(engine.owner)
    work = asyncio.create_task(engine.execute(task))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.sleep(0.7)  # Longer than two complete worker lease periods.
        assert await store.claim("second-worker") is None
        assert (await store.get(task.id)).lease_until > now()
    finally:
        release.set()
        await work
    assert (await store.get(task.id)).status == TaskStatus.SUCCEEDED


async def test_expired_worker_cannot_commit_even_before_reclaim(store):
    task = await live_task(store)
    await store.db.tasks.update_one(
        {"_id": task.id}, {"$set": {"lease_until": now() - timedelta(seconds=1)}}
    )
    task.status = TaskStatus.SUCCEEDED
    task.lease_owner = None
    with pytest.raises(Conflict):
        await store.save(task)
    assert (await store.get(task.id)).status == TaskStatus.RUNNING


async def test_lease_renewal_preserves_revision_and_cancellation_interrupts_work(store):
    store.settings.lease_seconds = 0.3
    task = await live_task(store)
    initial_revision = task.revision
    await store.renew_lease(task)
    assert (await store.get(task.id)).revision == initial_revision
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    work = asyncio.create_task(store.while_leased(task, operation()))
    await asyncio.wait_for(entered.wait(), 3)
    cancelled = await store.get(task.id)
    cancelled.status = TaskStatus.CANCELLED
    cancelled.lease_owner = None
    cancelled.lease_until = None
    await store.save(cancelled)
    with pytest.raises(Conflict):
        await asyncio.wait_for(work, 3)
    assert stopped.is_set()
    assert (await store.get(task.id)).status == TaskStatus.CANCELLED


async def test_recovery_connector_exception_stops_with_actionable_failure(
    client, store, engine, monkeypatch
):
    async def broken(*args):
        raise RuntimeError("secret provider response")

    monkeypatch.setattr(Tool, "verify", broken)
    await create_and_approve(client, scenario="notes")
    task = await store.claim(engine.owner)
    task.steps[0].status = StepStatus.RUNNING
    task.steps[0].attempts = 1
    await store.save(task)
    await engine.execute(task)
    saved = await store.get(task.id)
    assert saved.status == TaskStatus.FAILED
    assert "Recovery could not read" in saved.summary
    assert "secret provider" not in saved.model_dump_json()
    assert await store.db.artifacts.count_documents({}) == 0


async def test_missing_persisted_tool_does_not_crash_worker(client, store, engine):
    await create_and_approve(client, scenario="notes")
    task = await store.claim(engine.owner)
    task.steps[0].tool = "removed.tool"
    await store.save(task)
    await engine.execute(task)
    saved = await store.get(task.id)
    assert saved.status == TaskStatus.NEEDS_USER
    assert "unavailable tool" in saved.summary
    assert saved.steps[0].error.retry_allowed is False


async def test_verification_auth_error_never_offers_unsafe_resend(store, engine, monkeypatch):
    class UnsafeTool(Tool):
        async def run(self, args, key, store):
            return {"id": key}

        async def verify(self, args, result, store):
            raise ToolFailure(FailureClass.AUTH, "Reconnect before checking the result.")

    tool = UnsafeTool(
        "test.send", "test", "Test connector", CreateNote, "write_irreversible", False, "live"
    )
    monkeypatch.setitem(TOOL_REGISTRY, tool.name, tool)
    args = {"title": "Hello", "content": "Message"}
    task = Task(
        goal="Test interrupted send",
        scenario="agent",
        mode="live",
        status=TaskStatus.QUEUED,
        steps=[
            Step(
                id="s1",
                title="Send",
                tool=tool.name,
                args=args,
                approved=True,
                approved_args=args,
                risk=tool.risk,
                expected_outcome="Read-back",
            )
        ],
    )
    await store.insert(task)
    saved = await run_queued(store, engine)
    assert saved.status == TaskStatus.NEEDS_USER
    assert saved.steps[0].error.retry_allowed is False
    assert "retry" not in saved.steps[0].error.options
    assert saved.steps[0].attempts == 1


def search_step():
    return Step(
        id="s1",
        title="Search",
        tool="amazon.search",
        args={"query": "black shirt"},
        risk="read",
        expected_outcome="Search results",
        status=StepStatus.VERIFIED,
        result={"products": [{"asin": "B012345678", "title": "Black shirt", "price": 599}]},
    )


async def test_saved_amazon_search_asks_size_without_another_model_request(store, monkeypatch):
    post = AsyncMock(side_effect=AssertionError("A missing size needs no model inference"))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    task = await live_task(store, "Find a black shirt under 1000 and add it to cart")
    task.steps = [search_step()]
    await store.save(task)
    await advance(task, store, OllamaPlanner(store.settings))
    saved = await store.get(task.id)
    assert saved.status == TaskStatus.NEEDS_USER
    assert "size" in saved.question
    assert saved.steps[0].status == StepStatus.VERIFIED
    assert saved.steps[0].result == search_step().result
    post.assert_not_awaited()
    saved.answers.append("M")
    assert shopping_question(saved) is None


async def test_saved_amazon_search_repairs_repeated_search_into_inspection(store, monkeypatch):
    task = await live_task(store, "Find a black shirt under 1000 and add it to cart")
    task.steps = [search_step()]
    task.answers.append("size M")
    replies = [
        Decision(
            action="tool",
            tool="amazon.search",
            arguments_json='{"query":"black shirt size M under 1000"}',
            message="Search again",
        ),
        Decision(
            action="tool",
            tool="amazon.inspect",
            arguments_json='{"asin":"B012345678"}',
            message="Inspect the saved shirt candidate",
        ),
    ]
    calls = []

    async def response(self, url, **kwargs):
        # Copy the JSON because the planner appends corrective feedback later.
        calls.append(json.loads(json.dumps(kwargs["json"])))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"message": {"content": replies.pop(0).model_dump_json()}},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", response)
    decision = await OllamaPlanner(store.settings).decide(task)
    assert decision.tool == "amazon.inspect"
    assert json.loads(decision.arguments_json) == {
        "asin": "B012345678",
        "size": "",
        "color": "",
    }
    available = calls[0]["format"]["properties"]["tool"]["enum"]
    assert "amazon.inspect" in available
    assert "amazon.search" not in available
    assert "amazon.add_to_cart" not in available
    assert "done" not in calls[0]["format"]["properties"]["action"]["enum"]
    assert "inspect an observed ASIN" in calls[1]["messages"][-1]["content"]
    assert task.steps == [search_step()]


def test_amazon_phase_handles_saved_search_without_results():
    task = Task(goal="Find a shirt", scenario="agent", steps=[search_step()])
    task.steps[0].result = None
    assert "amazon.search" in tools_for_task(task)


async def test_completed_action_is_repaired_before_loop_guard(store, monkeypatch):
    task = await live_task(store, "Save a note")
    args = {"title": "Project", "content": "Review tomorrow"}
    task.steps = [
        Step(
            id="s1",
            title="Save",
            tool="notes.save",
            args=args,
            risk="write_reversible",
            expected_outcome="Saved",
            status=StepStatus.VERIFIED,
            result={"id": "saved"},
        )
    ]
    responses = [
        Decision(action="tool", tool="notes.save", arguments_json=json.dumps(args), message="Save"),
        Decision(action="done", message="Note saved"),
    ]
    calls = []

    async def respond(self, url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"message": {"content": responses.pop(0).model_dump_json()}},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", respond)
    decision = await OllamaPlanner(store.settings).decide(task)
    assert decision.action == "done"
    assert len(calls) == 2
    assert "exact action already exists" in calls[-1]["messages"][-1]["content"]


async def test_slow_model_is_distinguished_from_offline_and_preserves_results(store, monkeypatch):
    async def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("sensitive request")

    monkeypatch.setattr(httpx.AsyncClient, "post", timeout)
    task = await live_task(store, "Find a black shirt in size M under 1000 and add to cart")
    task.steps = [search_step()]
    await store.save(task)
    await advance(task, store, OllamaPlanner(store.settings))
    saved = await store.get(task.id)
    assert "took too long" in saved.summary
    assert "Completed results are saved" in saved.summary
    assert "sensitive request" not in saved.summary
    assert saved.steps[0].status == StepStatus.VERIFIED


def test_compact_schema_preserves_title_argument():
    schema = prompt_schema(CreateNote.model_json_schema())
    assert "title" in schema["properties"]
    assert schema["properties"]["title"]["minLength"] == 1
    assert "title" not in schema


@pytest.mark.parametrize("payload", [[], {"models": None}, {"models": [None, {}]}])
async def test_malformed_model_inventory_does_not_crash_runtime(store, monkeypatch, payload):
    async def response(self, url, **kwargs):
        return httpx.Response(200, request=httpx.Request("GET", url), json=payload)

    monkeypatch.setattr(httpx.AsyncClient, "get", response)
    assert not (await ModelRuntime(store.settings).status())["model_available"]


async def test_untagged_model_matches_ollama_latest_alias(store, monkeypatch):
    store.settings.ollama_model = "qwen3"

    async def response(self, url, **kwargs):
        return httpx.Response(
            200, request=httpx.Request("GET", url), json={"models": [{"name": "qwen3:latest"}]}
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", response)
    assert (await ModelRuntime(store.settings).status())["model_available"]


async def test_malformed_pull_stream_becomes_sanitized_failure(store, monkeypatch):
    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        yield httpx.Response(200, request=httpx.Request(method, url), content=b"[]\n")

    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    runtime = ModelRuntime(store.settings)
    await runtime._download()
    assert runtime.progress["state"] == "failed"
    assert "Resume" in runtime.progress["message"]


def test_cart_budget_is_grounded_in_latest_explicit_user_limit():
    from jarvis.agent import action_question, explicit_budget

    task = Task(goal="Find a shirt under 1000 and add to cart", scenario="agent", steps=[])
    item = {"asin": "B012345678", "title": "Shirt", "size": "M", "color": "Black", "price": 1200}
    task.steps.append(
        Step(
            id="s1",
            title="Inspect",
            tool="amazon.inspect",
            args={},
            risk="read",
            expected_outcome="Variant",
            status=StepStatus.VERIFIED,
            result=item,
        )
    )
    args = {key: value for key, value in item.items() if key != "price"}
    args.update(unit_price=1200, max_price=1500)
    assert "budget" in action_question("amazon.add_to_cart", args, task)
    task.answers.append("Raise my budget to INR 1,500")
    assert explicit_budget(task) == (1500, False)
    assert action_question("amazon.add_to_cart", args, task) is None
    task.answers.append("Keep it under ₹1.2k")
    assert explicit_budget(task) == (1200, True)
    assert "budget" in action_question("amazon.add_to_cart", args, task)


def test_non_tool_json_allows_equivalent_empty_object_whitespace():
    from jarvis.agent import parse_decision

    decision = parse_decision(
        json.dumps(
            {
                "action": "ask",
                "tool": "",
                "arguments_json": "{ }",
                "message": "Which size?",
            }
        )
    )
    assert decision.arguments_json == "{}"
