import asyncio

from tests.conftest import create_and_approve


async def test_api_requires_authentication(client):
    client.headers.pop("Authorization")
    assert (await client.get("/api/tasks")).status_code == 401
    assert (await client.post("/api/tasks", json={"goal": "notes"})).status_code == 401


async def test_bad_token_is_rejected(client):
    client.headers["Authorization"] = "Bearer invalid-token"
    assert (await client.get("/api/tasks")).status_code == 401


async def test_input_validation_and_missing_task(client):
    assert (await client.post("/api/tasks", json={"goal": "  "})).status_code == 422
    assert (
        await client.post("/api/tasks", json={"goal": "notes", "failure_mode": "bogus"})
    ).status_code == 422
    assert (await client.get("/api/tasks/unknown")).status_code == 404


async def test_history_pagination(client):
    for _ in range(3):
        await create_and_approve(client, scenario="notes")
        await asyncio.sleep(0.002)
    first = (await client.get("/api/tasks?limit=2")).json()
    second = (await client.get(f"/api/tasks?limit=2&before={first[-1]['id']}")).json()
    assert len(first) == 2
    assert len(second) == 1
    assert {task["id"] for task in first}.isdisjoint(task["id"] for task in second)


async def test_device_heartbeat_is_persisted(client):
    response = await client.post(
        "/api/devices/heartbeat",
        json={
            "id": "test-device-123",
            "name": "Test phone",
        },
    )
    assert response.status_code == 200
    devices = (await client.get("/api/devices")).json()
    assert devices[0]["name"] == "Test phone"
    assert devices[0]["online"] is True


async def test_connections_are_truthfully_labelled(client):
    connections = (await client.get("/api/connections")).json()
    sandbox = [item for item in connections if item["status"] == "sandbox"]
    assert len(sandbox) == 4
    assert all(not item["connected"] for item in sandbox)
    gmail = next(item for item in connections if item["id"] == "live_gmail")
    assert not gmail["connected"]


async def test_retry_planning_after_two_answers_preserves_answers(client, store):
    from jarvis.models import Task, TaskStatus

    task = Task(
        goal="Find a shirt",
        scenario="agent",
        mode="live",
        steps=[],
        answers=["Size M", "Black"],
        status=TaskStatus.NEEDS_USER,
        summary="Planning timed out",
    )
    await store.insert(task)
    result = await client.post(f"/api/tasks/{task.id}/retry", json={"revision": task.revision})
    assert result.status_code == 200
    assert result.json()["answers"] == ["Size M", "Black"]
    assert result.json()["status"] == "QUEUED"
