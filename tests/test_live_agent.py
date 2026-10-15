import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from jarvis.agent import Decision, OllamaPlanner, PlannerError, advance
from jarvis.email_tools import LIVE_TOOLS, EmailArgs
from jarvis.models import Task, TaskStatus
from jarvis.tools import ToolFailure
from tests.conftest import run_queued


class ScriptedPlanner:
    def __init__(self, *decisions):
        self.decisions = iter(decisions)

    async def decide(self, task):
        return next(self.decisions)


def email_decision(recipient="priya@example.com"):
    return Decision(
        action="tool",
        tool="email.send",
        message="Email Priya about the report",
        arguments_json=json.dumps(
            {
                "recipient": recipient,
                "subject": "Project report",
                "body": "I will send our project report tomorrow.",
            }
        ),
    )


async def live_task(
    store, goal="Email priya@example.com that I will send our project report tomorrow"
):
    task = Task(goal=goal, scenario="agent", mode="live", steps=[], status=TaskStatus.QUEUED)
    await store.insert(task)
    return await store.claim("test-agent")


async def test_live_draft_waits_for_exact_approval(client, store):
    task = await live_task(store)
    await advance(task, store, ScriptedPlanner(email_decision()))
    saved = await store.get(task.id)
    assert saved.status == TaskStatus.AWAITING_APPROVAL
    assert saved.steps[0].attempts == 0
    assert saved.steps[0].resolved_args["recipient"] == "priya@example.com"
    assert not saved.steps[0].approved
    assert await store.db.email_dispatches.count_documents({}) == 0
    response = await client.post(f"/api/tasks/{task.id}/approve", json={"revision": saved.revision})
    assert response.status_code == 409  # Account configuration is required, never fake success.


async def test_missing_or_hallucinated_recipient_requires_clarification(client, store):
    task = await live_task(store, "Email Priya about the report")
    await advance(task, store, ScriptedPlanner(email_decision("invented@example.com")))
    saved = await store.get(task.id)
    assert saved.status == TaskStatus.NEEDS_USER
    assert saved.question and not saved.steps
    response = await client.post(
        f"/api/tasks/{task.id}/answer",
        json={
            "revision": saved.revision,
            "text": "Use priya@example.com",
        },
    )
    assert response.status_code == 200
    resumed = await store.claim("test-agent")
    await advance(resumed, store, ScriptedPlanner(email_decision()))
    assert (await store.get(task.id)).status == TaskStatus.AWAITING_APPROVAL


async def test_draft_edit_invalidates_approval(client, store):
    task = await live_task(store)
    await advance(task, store, ScriptedPlanner(email_decision()))
    task = await store.get(task.id)
    response = await client.post(
        f"/api/tasks/{task.id}/draft",
        json={
            "revision": task.revision,
            "recipient": "new@example.com",
            "subject": "Updated",
            "body": "Revised message",
        },
    )
    assert response.status_code == 200
    saved = await store.get(task.id)
    assert saved.steps[0].args["recipient"] == "new@example.com"
    assert saved.steps[0].approved is False
    assert saved.steps[0].approved_args is None
    stale = await client.post(f"/api/tasks/{task.id}/approve", json={"revision": task.revision})
    assert stale.status_code == 409


async def test_two_real_actions_then_done_uses_verified_results(client, store, engine):
    from jarvis.models import StepStatus

    task = await live_task(store, "Save a note about the project")
    note = Decision(
        action="tool",
        tool="notes.save",
        message="Save project note",
        arguments_json='{"title":"Project","content":"Review tomorrow"}',
    )
    await advance(task, store, ScriptedPlanner(note))
    executed = await run_queued(store, engine)
    assert executed.status == TaskStatus.QUEUED  # More planning, not premature completion.
    assert executed.steps[0].status == StepStatus.VERIFIED
    task = await store.claim("test-agent")
    await advance(task, store, ScriptedPlanner(Decision(action="done", message="Saved the note")))
    assert (await store.get(task.id)).status == TaskStatus.SUCCEEDED


async def test_done_without_verified_work_is_not_success(store):
    task = await live_task(store)
    await advance(task, store, ScriptedPlanner(Decision(action="done", message="Done!")))
    assert (await store.get(task.id)).status == TaskStatus.NEEDS_USER


async def test_unavailable_capability_does_not_run_fake_tools(store):
    task = await live_task(store, "Send a WhatsApp message")
    await advance(
        task,
        store,
        ScriptedPlanner(
            Decision(
                action="unsupported",
                message="WhatsApp is not connected yet.",
            )
        ),
    )
    saved = await store.get(task.id)
    assert saved.status == TaskStatus.NEEDS_USER
    assert saved.steps == []


async def test_uncertain_send_never_repeats_smtp(store, monkeypatch):
    from jarvis import email_tools

    store.settings.email_address = "me@gmail.com"
    store.settings.email_password = SecretStr("test-password-not-real")
    monkeypatch.setattr(email_tools, "lookup_sent_sync", lambda *args: False)
    sent = []
    monkeypatch.setattr(email_tools, "send_sync", lambda *args: sent.append(args))
    args = EmailArgs(
        sender="me@gmail.com", recipient="priya@example.com", subject="Hello", body="Hi"
    ).model_dump()
    tool = LIVE_TOOLS["email.send"]
    result = await tool.run(args, "stable-task:s1", store)
    assert len(sent) == 1
    with pytest.raises(ToolFailure) as failure:
        await tool.verify(args, result, store)
    assert failure.value.retry_allowed is False
    with pytest.raises(ToolFailure):
        await tool.run(args, "stable-task:s1", store)
    assert len(sent) == 1
    # Independent read-back eventually sees the message: recover without another SMTP call.
    monkeypatch.setattr(email_tools, "lookup_sent_sync", lambda *args: True)
    assert await tool.verify(args, result, store) is True
    assert (await tool.run(args, "stable-task:s1", store))["id"] == result["id"]
    assert len(sent) == 1


async def test_sender_change_forces_new_approval(client, store, engine):
    store.settings.email_address = "first@gmail.com"
    store.settings.email_password = SecretStr("test-password-not-real")
    task = await live_task(store)
    await advance(task, store, ScriptedPlanner(email_decision()))
    task = await store.get(task.id)
    assert (
        await client.post(f"/api/tasks/{task.id}/approve", json={"revision": task.revision})
    ).status_code == 200
    store.settings.email_address = "second@gmail.com"
    task = await run_queued(store, engine)
    assert task.status == TaskStatus.AWAITING_APPROVAL
    assert task.steps[0].resolved_args["sender"] == "second@gmail.com"
    assert task.steps[0].attempts == 0


def test_email_header_injection_and_bad_addresses_rejected():
    with pytest.raises(ValidationError):
        EmailArgs(
            recipient="user@example.com", subject="Hello\r\nBcc: other@example.com", body="Hi"
        )
    with pytest.raises(ValidationError):
        EmailArgs(recipient="user@example.com,other@example.com", subject="Hello", body="Hi")


async def test_ollama_structured_request_and_invalid_tool_retry(store, monkeypatch):
    calls = []
    responses = [Decision(action="tool", tool="shell.run", message="Bad tool"), email_decision()]

    async def response(self, url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(
            200,
            json={"message": {"content": responses.pop(0).model_dump_json()}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", response)
    task = await live_task(store)
    decision = await OllamaPlanner(store.settings).decide(task)
    assert decision.tool == "email.send"
    assert len(calls) == 2
    assert calls[0]["stream"] is False
    assert calls[0]["format"]["type"] == "object"


async def test_model_outage_persists_actionable_status(store, monkeypatch):
    async def broken(self, task):
        raise PlannerError("Local model unavailable")

    monkeypatch.setattr(OllamaPlanner, "decide", broken)
    task = await live_task(store)
    await advance(task, store, OllamaPlanner(store.settings))
    saved = await store.get(task.id)
    assert saved.status == TaskStatus.NEEDS_USER
    assert saved.summary == "Local model unavailable"
    assert not saved.steps


def test_wire_schema_requires_tool_and_all_response_fields():
    from jarvis.agent import decision_schema

    schema = decision_schema()
    assert set(schema["required"]) == {"action", "tool", "arguments_json", "message"}
    assert "email.send" in schema["properties"]["tool"]["enum"]
    assert "shell.run" not in schema["properties"]["tool"]["enum"]


async def test_missing_tool_is_repaired_with_actual_rejected_reply(store, monkeypatch):
    import copy

    replies = [
        json.dumps(
            {
                "action": "tool",
                "message": "email.send",
                "arguments_json": email_decision().arguments_json,
            }
        ),
        email_decision().model_dump_json(),
    ]
    calls = []

    async def response(self, url, **kwargs):
        calls.append(copy.deepcopy(kwargs["json"]))
        return httpx.Response(
            200, json={"message": {"content": replies.pop(0)}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", response)
    task = await live_task(store)
    decision = await OllamaPlanner(store.settings).decide(task)
    assert decision.tool == "email.send"
    messages = calls[1]["messages"]
    assert messages[-2]["role"] == "assistant"
    assert json.loads(messages[-2]["content"])["message"] == "email.send"
    assert "Missing or unknown tool" in messages[-1]["content"]
    assert not task.steps


async def test_empty_subject_feedback_is_specific_and_excludes_message_content(store, monkeypatch):
    import copy

    invalid = email_decision()
    invalid.arguments_json = json.dumps(
        {"recipient": "priya@example.com", "subject": "", "body": "Private message text"}
    )
    replies = [invalid.model_dump_json(), email_decision().model_dump_json()]
    calls = []

    async def response(self, url, **kwargs):
        calls.append(copy.deepcopy(kwargs["json"]))
        return httpx.Response(
            200, json={"message": {"content": replies.pop(0)}}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", response)
    task = await live_task(store)
    decision = await OllamaPlanner(store.settings).decide(task)
    assert decision.tool == "email.send"
    feedback = calls[1]["messages"][-1]["content"]
    assert "subject: string_too_short" in feedback
    assert "Private message text" not in feedback


async def test_invalid_phone_produces_clarification_on_repair(store, monkeypatch):
    invalid = Decision(
        action="tool",
        tool="whatsapp.send",
        message="Send hi",
        arguments_json='{"phone":"9876543210","body":"hi"}',
    )
    replies = [invalid, Decision(action="ask", message="What is the country code?")]

    async def response(self, url, **kwargs):
        return httpx.Response(
            200,
            json={"message": {"content": replies.pop(0).model_dump_json()}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", response)
    task = await live_task(store, "WhatsApp 9876543210 saying hi")
    await advance(task, store, OllamaPlanner(store.settings))
    saved = await store.get(task.id)
    assert saved.status == TaskStatus.NEEDS_USER
    assert saved.question == "What is the country code?"
    assert not saved.steps


async def test_repeated_invalid_action_explains_failure_without_executing(store, monkeypatch):
    invalid = email_decision()
    invalid.arguments_json = "{}"

    async def response(self, url, **kwargs):
        return httpx.Response(
            200,
            json={"message": {"content": invalid.model_dump_json()}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", response)
    task = await live_task(store)
    await advance(task, store, OllamaPlanner(store.settings))
    saved = await store.get(task.id)
    assert saved.status == TaskStatus.NEEDS_USER
    assert "recipient: missing" in saved.summary
    assert "No action was executed" in saved.summary
    assert not saved.steps
