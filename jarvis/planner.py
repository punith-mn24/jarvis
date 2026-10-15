"""Deterministic, labelled demo scenarios; deliberately not an LLM impersonation."""

from datetime import timedelta

from jarvis.models import CreateTask, Step, Task, now
from jarvis.tools import TOOLS, validate_plan

SCENARIOS = {
    "daily_brief": {
        "title": "Prepare my daily brief",
        "goal": "Read the demo inbox, save a brief, block review time, and notify the team.",
        "apps": ["gmail", "notes", "calendar", "slack"],
    },
    "meeting": {
        "title": "Coordinate a team meeting",
        "goal": "Create a demo meeting, prepare an email invite, and post a Slack heads-up.",
        "apps": ["calendar", "gmail", "slack"],
    },
    "notes": {
        "title": "Save a project checkpoint",
        "goal": "Save a demo project checkpoint and create a calendar reminder to review it.",
        "apps": ["notes", "calendar"],
    },
}


def make_step(id: str, title: str, tool: str, args: dict, expected: str, deps=()) -> Step:
    return Step(
        id=id,
        title=title,
        tool=tool,
        args=args,
        expected_outcome=expected,
        risk=TOOLS[tool].risk,
        depends_on=list(deps),
    )


def plan(request: CreateTask) -> Task:
    scenario = request.scenario
    if scenario is None:
        goal = request.goal.lower()
        if any(word in goal for word in ("brief", "inbox", "unread")):
            scenario = "daily_brief"
        elif any(word in goal for word in ("meeting", "invite")):
            scenario = "meeting"
        elif any(word in goal for word in ("note", "checkpoint")):
            scenario = "notes"
        else:
            raise ValueError(
                "The sandbox planner supports daily brief, team meeting, or project checkpoint. "
                "Choose a demo scenario. Free-form AI planning is not connected yet."
            )
    tomorrow = (now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    when = tomorrow.isoformat()
    if scenario == "daily_brief":
        steps = [
            make_step(
                "s1",
                "Read the demo inbox",
                "gmail.read",
                {"folder": "demo_inbox"},
                "Returned messages match the sandbox inbox.",
            ),
            make_step(
                "s2",
                "Save the daily brief",
                "notes.create",
                {"title": "Daily brief · Jarvis", "content": "{{s1.output.summary}}"},
                "The saved note's title and content match exactly.",
                ("s1",),
            ),
            make_step(
                "s3",
                "Block 30 minutes for review",
                "calendar.create",
                {"title": "Review Jarvis prototype", "starts_at": when, "duration_minutes": 30},
                "The event exists with the planned title, start time, and duration.",
                ("s2",),
            ),
            make_step(
                "s4",
                "Notify the demo team",
                "slack.send",
                {
                    "recipient": "#jarvis-demo",
                    "body": "Daily brief saved: {{s2.output.id}}. Review: " + when + ". "
                    "{{s1.output.summary}}",
                },
                "The stored message matches the approved recipient and text.",
                ("s1", "s2", "s3"),
            ),
        ]
    elif scenario == "meeting":
        steps = [
            make_step(
                "s1",
                "Create the demo meeting",
                "calendar.create",
                {"title": "Jarvis team check-in", "starts_at": when, "duration_minutes": 30},
                "Read-back matches the exact event details.",
            ),
            make_step(
                "s2",
                "Send the sandbox invite",
                "gmail.send",
                {
                    "recipient": "priya@example.test",
                    "body": "Demo check-in at " + when + ". Event: {{s1.output.id}}",
                },
                "The saved sandbox email matches the approved payload.",
                ("s1",),
            ),
            make_step(
                "s3",
                "Post a sandbox heads-up",
                "slack.send",
                {
                    "recipient": "#jarvis-demo",
                    "body": "Team check-in at " + when + ". Event: {{s1.output.id}}",
                },
                "The saved sandbox Slack message matches the approved payload.",
                ("s1", "s2"),
            ),
        ]
    else:
        steps = [
            make_step(
                "s1",
                "Save a project checkpoint",
                "notes.create",
                {
                    "title": "Jarvis checkpoint",
                    "content": "Next: connect real accounts, add an LLM planner, "
                    "and verify end-to-end.",
                },
                "The saved note matches its planned content.",
            ),
            make_step(
                "s2",
                "Schedule a checkpoint review",
                "calendar.create",
                {"title": "Review Jarvis checkpoint", "starts_at": when, "duration_minutes": 30},
                "The event can be read back with matching fields.",
                ("s1",),
            ),
        ]
    validate_plan(steps)
    task = Task(
        goal=request.goal.strip(),
        scenario=scenario,
        steps=steps,
        failure_mode=request.failure_mode,
        fault_step=next(step.id for step in steps if step.risk == "write_reversible"),
    )
    task.record("task.plan_ready", "Sandbox plan ready. Review the fixture data and UTC times.")
    return task
