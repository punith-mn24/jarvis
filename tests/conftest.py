import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from jarvis.config import Settings
from jarvis.engine import Engine
from jarvis.main import create_app
from jarvis.store import MongoStore

TEST_TOKEN = "jarvis-test-token-not-a-real-credential"


@pytest.fixture
async def store():
    uri = os.environ.get("JARVIS_TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set JARVIS_TEST_MONGODB_URI to run real MongoDB integration tests.")
    settings = Settings(
        _env_file=None,
        mongodb_uri=uri,
        mongodb_database=f"jarvis_test_{uuid4().hex}",
        access_token=TEST_TOKEN,
        step_delay=0,
        retry_base=0,
    )
    store = MongoStore(settings)
    await store.start()
    yield store
    await store.client.drop_database(settings.mongodb_database)
    await store.close()


@pytest.fixture
def engine(store):
    return Engine(store, store.settings)


@pytest.fixture
async def client(store):
    app = create_app(store.settings, store=store, run_worker=False)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    ) as client:
        yield client


async def run_queued(store, engine):
    claimed = await store.claim(engine.owner)
    assert claimed is not None
    await engine.execute(claimed)
    return await store.get(claimed.id)


async def create_and_approve(client, scenario="daily_brief", failure_mode="none"):
    response = await client.post(
        "/api/tasks",
        json={
            "goal": "Run demo scenario",
            "scenario": scenario,
            "failure_mode": failure_mode,
        },
    )
    assert response.status_code == 201
    task = response.json()
    approved = await client.post(
        f"/api/tasks/{task['id']}/approve",
        json={
            "revision": task["revision"],
        },
    )
    assert approved.status_code == 200
    return approved.json()
