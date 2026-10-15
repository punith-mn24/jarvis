"""Persistent browser sessions and shared adapter helpers."""

import asyncio
import shutil
from pathlib import Path

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from jarvis.config import Settings
from jarvis.models import FailureClass
from jarvis.tools import Tool, ToolFailure


def browser_binary(configured: str, names: tuple[str, ...], label: str) -> str:
    found = configured or next((path for name in names if (path := shutil.which(name))), "")
    if not found or not Path(found).is_file():
        raise ToolFailure(
            FailureClass.AUTH, f"{label} is not installed or its configured binary path is invalid."
        )
    return found


class BrowserSession:
    """Keep Amazon and Brave Gmail sessions separate; never fall back for Gmail."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.contexts = {}
        self.driver = None
        self.pages = {}
        self.lock = asyncio.Lock()

    async def start(self, provider="amazon"):
        if provider in self.contexts:
            return self.contexts[provider]
        profile = (
            self.settings.brave_profile if provider == "gmail" else self.settings.browser_profile
        )
        await asyncio.to_thread(Path(profile).mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(Path(profile).chmod, 0o700)
        kwargs = {}
        if provider == "gmail":
            kwargs["executable_path"] = browser_binary(
                self.settings.brave_binary,
                ("brave", "brave-browser", "brave-browser-stable"),
                "Brave",
            )
        try:
            if self.driver is None:
                self.driver = await async_playwright().start()
            context = await self.driver.chromium.launch_persistent_context(
                profile,
                headless=self.settings.browser_headless,
                viewport={"width": 1365, "height": 900},
                locale="en-IN",
                accept_downloads=False,
                **kwargs,
            )
            context.set_default_timeout(6000)
            self.contexts[provider] = context
            context.on("close", lambda: self._closed(provider))
            return context
        except BrowserError as exc:
            label = "Brave" if provider == "gmail" else "Amazon's Chromium browser"
            raise ToolFailure(
                FailureClass.AUTH,
                f"Could not start {label}. Check the browser installation, "
                "desktop session and profile access.",
            ) from exc

    def _closed(self, provider):
        self.contexts.pop(provider, None)
        for name in list(self.pages):
            if ("gmail" if name.startswith("gmail") else "amazon") == provider:
                self.pages.pop(name, None)

    async def page(self, name: str):
        if name.startswith("whatsapp"):
            raise ToolFailure(
                FailureClass.INPUT, "WhatsApp must use the dedicated Firefox session."
            )
        context = await self.start("gmail" if name.startswith("gmail") else "amazon")
        if name not in self.pages or self.pages[name].is_closed():
            self.pages[name] = await context.new_page()
        return self.pages[name]

    async def connect(self, provider: str):
        if provider not in ("gmail", "amazon"):
            raise ToolFailure(FailureClass.INPUT, "Use Firefox setup for WhatsApp.")
        async with self.lock:
            page = await self.page(provider + "-setup")
            if page.url == "about:blank":
                await page.goto(
                    "https://mail.google.com/mail/u/0/"
                    if provider == "gmail"
                    else "https://www.amazon.in/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            await page.bring_to_front()
        return {
            "status": "browser_open",
            "provider": provider,
            "browser": "brave" if provider == "gmail" else "chromium",
            "message": "Sign in to Gmail in Brave, then click Verify Gmail session."
            if provider == "gmail"
            else "Sign in to Amazon in the opened browser window.",
        }

    async def close(self):
        for context in list(self.contexts.values()):
            await context.close()
        if self.driver:
            await self.driver.stop()
            self.driver = None


def session(store) -> BrowserSession:
    if not hasattr(store, "browser_session"):
        store.browser_session = BrowserSession(store.settings)
    return store.browser_session


async def first_text(page, selectors: str) -> str:
    items = page.locator(selectors)
    for index in range(await items.count()):
        value = (await items.nth(index).inner_text()).strip()
        if value:
            return value
    return ""


class BrowserTool(Tool):
    async def verify(self, args, result, store):
        try:
            return await self._verify(args, result, store)
        except ToolFailure as exc:
            if not self.retry_safe:
                raise ToolFailure(exc.kind, str(exc), False) from exc
            raise
        except BrowserError as exc:
            raise ToolFailure(
                FailureClass.TRANSIENT,
                "Browser read-back failed. Check the session and verify again.",
                self.retry_safe,
            ) from exc
