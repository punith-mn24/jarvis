import asyncio
from datetime import timedelta

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument

from jarvis.config import Settings
from jarvis.models import Task, TaskStatus, now


class Conflict(Exception):
    """Another client or worker already changed this task."""


class MongoStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncMongoClient(
            settings.mongodb_uri.get_secret_value(),
            serverSelectionTimeoutMS=3000,
            tz_aware=True,
        )
        self.db = self.client[settings.mongodb_database]

    async def start(self) -> None:
        await self.client.admin.command("ping")
        await self.db.tasks.create_index([("created_at", DESCENDING)])
        await self.db.tasks.create_index([("status", ASCENDING), ("lease_until", ASCENDING)])
        await self.db.artifacts.create_index([("provider", ASCENDING)])
        await self.db.devices.create_index("last_seen", expireAfterSeconds=86400)

    async def close(self) -> None:
        await self.client.close()

    async def insert(self, task: Task) -> None:
        await self.db.tasks.insert_one({"_id": task.id, **task.model_dump(mode="python")})

    async def get(self, task_id: str) -> Task | None:
        doc = await self.db.tasks.find_one({"_id": task_id})
        return Task.model_validate(doc) if doc else None

    async def list(self, before: str | None = None, limit: int = 50) -> list[Task]:
        # IDs are unique; use (created_at, id) as a stable pagination cursor.
        query = {}
        if before:
            anchor = await self.get(before)
            if anchor:
                query = {
                    "$or": [
                        {"created_at": {"$lt": anchor.created_at}},
                        {"created_at": anchor.created_at, "_id": {"$lt": anchor.id}},
                    ]
                }
        docs = (
            await self.db.tasks.find(query)
            .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
            .limit(limit)
            .to_list(length=limit)
        )
        return [Task.model_validate(doc) for doc in docs]

    async def save(self, task: Task) -> None:
        revision = task.revision
        query = {"_id": task.id, "revision": revision}
        if task._claimed_owner:
            query.update(
                lease_owner=task._claimed_owner,
                lease_until={"$gt": now()},
                status=TaskStatus.RUNNING,
            )
        task.updated_at = now()
        if task.lease_owner:
            task.lease_until = now() + timedelta(seconds=self.settings.lease_seconds)
        document = task.model_dump(mode="python")
        document["revision"] = revision + 1
        result = await self.db.tasks.replace_one(query, {"_id": task.id, **document})
        if not result.matched_count:
            raise Conflict("Task changed. Reload and try again.")
        task.revision = revision + 1
        if not task.lease_owner:
            task._claimed_owner = None

    async def renew_lease(self, task: Task) -> None:
        """Renew ownership without replacing task state or racing a checkpoint revision."""
        if not task._claimed_owner or not task.lease_owner:
            return
        until = now() + timedelta(seconds=self.settings.lease_seconds)
        result = await self.db.tasks.update_one(
            {
                "_id": task.id,
                "lease_owner": task._claimed_owner,
                "status": TaskStatus.RUNNING,
                "lease_until": {"$gt": now()},
            },
            {"$set": {"lease_until": until}},
        )
        if not result.matched_count:
            raise Conflict("Worker lease ended. Reload the task before continuing.")
        task.lease_until = until

    async def while_leased(self, task: Task, operation):
        """Keep long planning/browser work owned; stop it when ownership is lost."""

        async def renew():
            while True:
                await asyncio.sleep(min(10, self.settings.lease_seconds / 3))
                await self.renew_lease(task)

        work = asyncio.create_task(operation)
        heartbeat = asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait((work, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat  # A conflict/database outage stops the in-flight work.
            return await work
        finally:
            for pending in (work, heartbeat):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)

    async def claim(self, owner: str) -> Task | None:
        doc = await self.db.tasks.find_one_and_update(
            {
                "$or": [
                    {"status": TaskStatus.QUEUED},
                    {"status": TaskStatus.RUNNING, "lease_until": {"$lt": now()}},
                ]
            },
            {
                "$set": {
                    "status": TaskStatus.RUNNING,
                    "lease_owner": owner,
                    "lease_until": now() + timedelta(seconds=self.settings.lease_seconds),
                    "updated_at": now(),
                },
                "$inc": {"revision": 1},
            },
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        if not doc:
            return None
        task = Task.model_validate(doc)
        task._claimed_owner = owner
        return task

    async def put_artifact(self, key: str, provider: str, payload: dict) -> dict:
        # A stable task/step key makes retries and recovery idempotent in the sandbox.
        await self.db.artifacts.update_one(
            {"_id": key},
            {"$setOnInsert": {"provider": provider, "payload": payload, "created_at": now()}},
            upsert=True,
        )
        return {"id": key}

    async def get_artifact(self, key: str) -> dict | None:
        return await self.db.artifacts.find_one({"_id": key})

    async def heartbeat(self, device_id: str, name: str) -> None:
        await self.db.devices.update_one(
            {"_id": device_id},
            {"$set": {"name": name, "last_seen": now(), "capabilities": ["web_client"]}},
            upsert=True,
        )

    async def devices(self) -> list[dict]:
        docs = await self.db.devices.find().sort("last_seen", DESCENDING).limit(100).to_list(100)
        return [
            {
                "id": doc["_id"],
                "name": doc["name"],
                "last_seen": doc["last_seen"],
                "online": (now() - doc["last_seen"]).total_seconds() < 45,
                "capabilities": doc["capabilities"],
            }
            for doc in docs
        ]
