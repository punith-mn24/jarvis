import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jarvis.config import Settings
from jarvis.main import create_app
from tests.conftest import TEST_TOKEN


@pytest.fixture
def live_client():
    uri = os.environ.get("JARVIS_TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set JARVIS_TEST_MONGODB_URI for live-worker WebSocket tests.")
    settings = Settings(
        _env_file=None,
        mongodb_uri=uri,
        mongodb_database=f"jarvis_test_{uuid4().hex}",
        access_token=TEST_TOKEN,
        step_delay=0,
        retry_base=0,
    )
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = f"Bearer {TEST_TOKEN}"
        yield client
    from pymongo import MongoClient

    with MongoClient(uri) as cleanup:
        cleanup.drop_database(settings.mongodb_database)


def test_two_devices_receive_live_task_and_verified_completion(live_client):
    with live_client.websocket_connect("/ws") as first:
        first.send_json({"token": TEST_TOKEN})
        assert first.receive_json()["tasks"] == []
        with live_client.websocket_connect("/ws") as second:
            second.send_json({"token": TEST_TOKEN})
            assert second.receive_json()["tasks"] == []
            task = live_client.post("/api/tasks", json={"goal": "notes"}).json()
            assert first.receive_json()["tasks"][0]["id"] == task["id"]
            assert second.receive_json()["tasks"][0]["id"] == task["id"]
            response = live_client.post(
                f"/api/tasks/{task['id']}/approve",
                json={
                    "revision": task["revision"],
                },
            )
            assert response.status_code == 200
            for device in (first, second):
                for _ in range(20):
                    snapshot = device.receive_json()["tasks"][0]
                    if snapshot["status"] == "SUCCEEDED":
                        break
                else:
                    pytest.fail("Device did not see a successful terminal state.")
                assert all(step["verdict"] == "VERIFIED" for step in snapshot["steps"])
            # A new browser replays the current durable snapshot with its audit trail.
            with live_client.websocket_connect("/ws") as reconnected:
                reconnected.send_json({"token": TEST_TOKEN})
                snapshot = reconnected.receive_json()["tasks"][0]
                assert snapshot["status"] == "SUCCEEDED"
                assert any(event["type"] == "task.finished" for event in snapshot["events"])


def test_websocket_rejects_wrong_and_malformed_credentials(live_client):
    for payload in ({"token": "wrong"}, {"token": "非ASCII"}, ["invalid"]):
        with live_client.websocket_connect("/ws") as websocket:
            websocket.send_json(payload)
            with pytest.raises(WebSocketDisconnect) as failure:
                websocket.receive_json()
            assert failure.value.code == 4401
