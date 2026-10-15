"""Observable, resumable local model setup; never enqueue work against a missing model."""

import asyncio
import json
from contextlib import suppress

import httpx

from jarvis.config import Settings


class ModelRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.job: asyncio.Task | None = None
        self.progress = {"state": "idle", "message": "Model setup has not started.", "percent": 0}

    async def status(self) -> dict:
        reachable = False
        models = []
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                response = await client.get(self.settings.ollama_url.rstrip("/") + "/api/tags")
                response.raise_for_status()
                reachable = True
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("models"), list):
                    raise ValueError("Invalid model inventory")
                models = [
                    item["name"]
                    for item in data["models"]
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        configured = self.settings.ollama_model
        canonical = configured if ":" in configured.rsplit("/", 1)[-1] else configured + ":latest"
        ready = configured in models or canonical in models
        return {
            "provider": "ollama",
            "model": self.settings.ollama_model,
            "reachable": reachable,
            "model_available": ready,
            "installed_models": models,
            "download": (
                {"state": "ready", "message": "Local model ready", "percent": 100}
                if ready
                else dict(self.progress)
            ),
        }

    async def install(self) -> dict:
        status = await self.status()
        if status["model_available"]:
            return status
        if not status["reachable"]:
            raise ValueError("Ollama is offline. Start the local Ollama service first.")
        if self.job is None or self.job.done():
            self.progress = {
                "state": "downloading",
                "message": "Resuming model download…",
                "percent": 0,
            }
            self.job = asyncio.create_task(self._download())
        return await self.status()

    async def _download(self) -> None:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(connect=10, read=180, write=10, pool=10),
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        self.settings.ollama_url.rstrip("/") + "/api/pull",
                        json={"model": self.settings.ollama_model, "stream": True},
                    ) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                raise ValueError("Invalid download progress")
                            if event.get("error"):
                                raise ValueError("Model download interrupted")
                            total = event.get("total", 0)
                            completed = event.get("completed", 0)
                            if not isinstance(total, int) or not isinstance(completed, int):
                                raise ValueError("Invalid download counters")
                            percent = (
                                max(0, min(99, round(100 * completed / total)))
                                if total > 0
                                else self.progress["percent"]
                            )
                            self.progress = {
                                "state": "downloading",
                                "message": event.get("status", "Downloading"),
                                "percent": percent,
                            }
                if (await self.status())["model_available"]:
                    self.progress = {
                        "state": "ready",
                        "message": "Local model ready",
                        "percent": 100,
                    }
                    return
                raise ValueError("Model did not finish installing")
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                self.progress.update(
                    state="retrying", message=f"Network interrupted. Resuming ({attempt + 1}/3)…"
                )
                if attempt < 2:
                    await asyncio.sleep(2)
        self.progress.update(
            state="failed", message="Download interrupted. Resume to keep downloaded data."
        )

    async def close(self):
        if self.job and not self.job.done():
            self.job.cancel()
            with suppress(asyncio.CancelledError):
                await self.job
