"""Typed sandbox tools. No real Gmail, Calendar, Slack, or Notion requests are made."""

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.models import FailureClass, Step, Task
from jarvis.store import MongoStore


class ToolFailure(Exception):
    def __init__(self, kind: FailureClass, message: str, retry_allowed: bool = True):
        super().__init__(message)
        self.kind = kind
        self.retry_allowed = retry_allowed


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadMail(Args):
    folder: Literal["demo_inbox"] = "demo_inbox"


class CreateEvent(Args):
    title: str = Field(min_length=1, max_length=200)
    starts_at: str = Field(min_length=1, max_length=100)
    duration_minutes: int = Field(default=30, ge=5, le=480)


class CreateNote(Args):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=10000)


class SendMessage(Args):
    recipient: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)


DEMO_MAIL = {
    "messages": [
        {
            "id": "demo-message-1",
            "from": "priya@example.test",
            "subject": "Hackathon project review",
            "body": "Review the Jarvis prototype and failure demo at the team check-in.",
        }
    ],
    "summary": "Priya requested a review of the Jarvis prototype and failure demo.",
}


@dataclass(frozen=True)
class Tool:
    name: str
    provider: str
    description: str
    schema: type[Args]
    risk: Literal["read", "write_reversible", "write_irreversible"]
    retry_safe: bool = True
    mode: Literal["sandbox", "live"] = "sandbox"

    @property
    def timeout(self) -> float:
        return 8

    def validate(self, args: dict) -> dict:
        try:
            return self.schema.model_validate(args).model_dump()
        except ValidationError as exc:
            raise ToolFailure(FailureClass.INPUT, f"Invalid arguments for {self.name}.") from exc

    async def run(self, args: dict, key: str, store: MongoStore) -> dict:
        if self.name == "gmail.read":
            return deepcopy(DEMO_MAIL)
        return await store.put_artifact(key, self.provider, args)

    async def verify(self, args: dict, result: dict, store: MongoStore) -> bool:
        if self.name == "gmail.read":
            # The fixture is the sandbox source of truth, separate from the returned copy.
            return result == DEMO_MAIL
        record = await store.get_artifact(result.get("id", ""))
        return bool(record and record["provider"] == self.provider and record["payload"] == args)


TOOLS = {
    tool.name: tool
    for tool in [
        Tool("gmail.read", "gmail", "Read the fixture inbox", ReadMail, "read"),
        Tool("gmail.send", "gmail", "Store a simulated email", SendMessage, "write_irreversible"),
        Tool(
            "calendar.create", "calendar", "Create a sandbox event", CreateEvent, "write_reversible"
        ),
        Tool("notes.create", "notes", "Save a sandbox note", CreateNote, "write_reversible"),
        Tool(
            "slack.send",
            "slack",
            "Store a simulated Slack message",
            SendMessage,
            "write_irreversible",
        ),
    ]
}

REFERENCE = re.compile(r"\{\{([a-zA-Z0-9_-]+)\.output\.([a-zA-Z0-9_.]+)\}\}")


def resolve_args(args: dict, steps: list[Step]) -> dict:
    def resolve(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if not isinstance(value, str):
            return value

        def lookup(match: re.Match) -> Any:
            step = next((step for step in steps if step.id == match[1]), None)
            if step is None or step.status != "VERIFIED" or step.result is None:
                raise ToolFailure(FailureClass.INPUT, "A required step has no verified output.")
            current = step.result
            for part in match[2].split("."):
                if not isinstance(current, dict) or part not in current:
                    raise ToolFailure(FailureClass.INPUT, "A required output field is missing.")
                current = current[part]
            return current

        full = REFERENCE.fullmatch(value)
        if full:
            return lookup(full)
        return REFERENCE.sub(lambda match: str(lookup(match)), value)

    return resolve(args)


def validate_plan(steps: list[Step]) -> None:
    if not 1 <= len(steps) <= 15:
        raise ValueError("Plans must contain 1–15 steps.")
    seen: set[str] = set()
    calls: set[str] = set()
    for step in steps:
        if step.id in seen or step.tool not in TOOLS:
            raise ValueError("A plan contains a duplicate step ID or unknown tool.")
        if not set(step.depends_on) <= seen:
            raise ValueError("Dependencies must refer to earlier steps.")
        tool = TOOLS[step.tool]
        if step.risk != tool.risk:
            raise ValueError("Risk must match the trusted tool registry.")
        tool.validate(step.args)
        fingerprint = step.tool + json.dumps(step.args, sort_keys=True)
        if fingerprint in calls:
            raise ValueError("Repeated identical calls are blocked by the loop guard.")
        calls.add(fingerprint)
        for value in step.args.values():
            if isinstance(value, str):
                for match in REFERENCE.finditer(value):
                    if match[1] not in step.depends_on:
                        raise ValueError("Output references require an explicit dependency.")
        seen.add(step.id)


async def run_with_fault(tool: Tool, args: dict, task: Task, step: Step, store: MongoStore):
    fault = task.failure_mode if task.mode == "sandbox" and task.fault_step == step.id else "none"
    if fault == "auth":
        raise ToolFailure(FailureClass.AUTH, "Sandbox authorization denied (simulated HTTP 403).")
    if fault == "transient" and step.attempts == 1:
        raise ToolFailure(FailureClass.TRANSIENT, "Sandbox rate limit (simulated HTTP 429).")
    key = f"{task.id}:{step.id}"
    if fault == "silent":
        return {"id": key}  # A 200-shaped response without any write.
    if fault == "wrong_data":
        incorrect = {**args, "title": "WRONG TITLE — injected fault"}
        return await tool.run(incorrect, key, store)
    return await tool.run(args, key, store)
