import pytest

from jarvis.models import CreateTask, StepStatus
from jarvis.planner import plan
from jarvis.tools import ToolFailure, resolve_args, validate_plan


def test_demo_plan_has_four_providers_and_trusted_risk():
    task = plan(CreateTask(goal="Prepare my daily brief"))
    assert {step.tool.split(".")[0] for step in task.steps} == {
        "gmail",
        "notes",
        "calendar",
        "slack",
    }
    assert task.steps[-1].risk == "write_irreversible"
    assert task.mode == "sandbox"


def test_unknown_goal_does_not_pretend_to_be_ai():
    with pytest.raises(ValueError, match="not connected yet"):
        plan(CreateTask(goal="Book a flight to Delhi"))


def test_references_require_verified_outputs():
    task = plan(CreateTask(goal="daily brief"))
    with pytest.raises(ToolFailure, match="verified output"):
        resolve_args(task.steps[1].args, task.steps)
    task.steps[0].status = StepStatus.VERIFIED
    task.steps[0].result = {"summary": "Real output content"}
    assert resolve_args(task.steps[1].args, task.steps)["content"] == "Real output content"


def test_risk_downgrade_rejected():
    task = plan(CreateTask(goal="daily brief"))
    task.steps[-1].risk = "read"
    with pytest.raises(ValueError, match="trusted tool registry"):
        validate_plan(task.steps)


def test_loop_guard_rejects_repeated_calls():
    task = plan(CreateTask(goal="notes"))
    repeated = task.steps[0].model_copy(deep=True)
    repeated.id = "repeat"
    with pytest.raises(ValueError, match="loop guard"):
        validate_plan([task.steps[0], repeated])


def test_step_budget_and_forward_dependencies_rejected():
    task = plan(CreateTask(goal="notes"))
    with pytest.raises(ValueError, match="1–15"):
        validate_plan(task.steps * 8)
    task.steps[0].depends_on = ["s2"]
    with pytest.raises(ValueError, match="earlier"):
        validate_plan(task.steps)
