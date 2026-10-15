from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex


class TaskStatus(StrEnum):
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    NEEDS_USER = "NEEDS_USER"
    CANCELLED = "CANCELLED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class FailureClass(StrEnum):
    TRANSIENT = "TRANSIENT"
    AUTH = "AUTH"
    INPUT = "INPUT"
    LOGIC = "LOGIC"
    UNRECOVERABLE = "UNRECOVERABLE"
    LOOP = "LOOP"


class Failure(BaseModel):
    kind: FailureClass
    message: str
    options: list[str] = Field(default_factory=list)
    retry_allowed: bool = True


class Step(BaseModel):
    id: str
    title: str
    tool: str
    args: dict[str, Any]
    expected_outcome: str
    risk: Literal["read", "write_reversible", "write_irreversible"]
    depends_on: list[str] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    result: dict[str, Any] | None = None
    resolved_args: dict[str, Any] | None = None
    verdict: Literal["VERIFIED", "FAILED", "UNKNOWN"] | None = None
    error: Failure | None = None
    approved: bool = False
    approved_args: dict[str, Any] | None = None


class Event(BaseModel):
    id: str = Field(default_factory=new_id)
    type: str
    message: str
    step_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=now)


class Task(BaseModel):
    # Only tasks returned by claim() carry worker ownership. API reads may still
    # cancel or edit a task through the ordinary optimistic revision check.
    _claimed_owner: str | None = PrivateAttr(default=None)
    id: str = Field(default_factory=new_id)
    goal: str
    scenario: str
    mode: Literal["sandbox", "live"] = "sandbox"
    status: TaskStatus = TaskStatus.AWAITING_APPROVAL
    steps: list[Step]
    events: list[Event] = Field(default_factory=list)
    failure_mode: Literal["none", "transient", "auth", "silent", "wrong_data"] = "none"
    fault_step: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    revision: int = 0
    lease_owner: str | None = None
    lease_until: datetime | None = None
    summary: str = "Plan ready for review."
    question: str | None = None
    answers: list[str] = Field(default_factory=list)
    agent_turns: int = 0

    def record(self, type: str, message: str, step_id: str | None = None, **data: Any) -> None:
        self.events.append(Event(type=type, message=message, step_id=step_id, data=data))


class CreateTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=3, max_length=2000)
    scenario: Literal["daily_brief", "meeting", "notes"] | None = None
    failure_mode: Literal["none", "transient", "auth", "silent", "wrong_data"] = "none"
    mode: Literal["sandbox", "live"] = "sandbox"


class Answer(BaseModel):
    revision: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=4000)


class EditEmail(BaseModel):
    revision: int = Field(ge=0)
    recipient: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)


class Approval(BaseModel):
    revision: int = Field(ge=0)


class DeviceInput(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    name: str = Field(min_length=1, max_length=80)
