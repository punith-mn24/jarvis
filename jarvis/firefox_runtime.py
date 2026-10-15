"""The installed Firefox app, a private persistent profile and serialized WebDriver calls."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from pathlib import Path
from threading import Event

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

from jarvis.browser_runtime import browser_binary
from jarvis.models import FailureClass
from jarvis.tools import ToolFailure


class FirefoxSession:
    def __init__(self, settings):
        self.settings = settings
        self.lock = asyncio.Lock()
        self.driver = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-firefox")

    def _driver(self):
        if self.driver:
            try:
                if self.driver.window_handles:
                    return self.driver
            except WebDriverException:
                pass
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            self.driver = None
        profile = Path(self.settings.firefox_profile).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        profile.chmod(0o700)
        options = Options()
        options.binary_location = browser_binary(
            self.settings.firefox_binary, ("firefox",), "Firefox"
        )
        # FirefoxProfile would clone the profile to a temporary directory; use the real path.
        options.add_argument("-profile")
        options.add_argument(str(profile))
        options.add_argument("-no-remote")
        options.set_preference("intl.accept_languages", "en-US,en")
        if self.settings.browser_headless:
            options.add_argument("-headless")
        service = Service(executable_path=self.settings.geckodriver_binary or None)
        try:
            self.driver = webdriver.Firefox(options=options, service=service)
            self.driver.set_page_load_timeout(30)
            self.driver.set_script_timeout(10)
            self.driver.set_window_size(1365, 900)
            return self.driver
        except WebDriverException as exc:
            raise ToolFailure(
                FailureClass.AUTH,
                "Firefox could not start. Check Firefox/geckodriver installation, "
                "desktop access and the dedicated Jarvis Firefox profile.",
            ) from exc

    async def call(self, function, *args):
        loop = asyncio.get_running_loop()
        cancelled = Event()
        work = loop.run_in_executor(self.executor, partial(self._invoke, function, args, cancelled))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            cancelled.set()
            # A thread cannot be killed safely. Drain its current bounded WebDriver
            # command before releasing the browser lock; guard every later command.
            with suppress(Exception):
                await asyncio.shield(work)
            raise

    def _invoke(self, function, args, cancelled):
        if cancelled.is_set():
            raise ToolFailure(FailureClass.INPUT, "Browser operation cancelled.", False)
        driver = self._driver()
        execute = driver.execute

        def guarded_execute(*command_args, **command_kwargs):
            if cancelled.is_set():
                raise ToolFailure(FailureClass.INPUT, "Browser operation cancelled.", False)
            return execute(*command_args, **command_kwargs)

        driver.execute = guarded_execute
        try:
            if cancelled.is_set():
                raise ToolFailure(FailureClass.INPUT, "Browser operation cancelled.", False)
            return function(driver, *args)
        finally:
            driver.execute = execute

    async def connect(self):
        def open_page(driver):
            if not driver.current_url.startswith("https://web.whatsapp.com/"):
                driver.get("https://web.whatsapp.com/")
            driver.switch_to.window(driver.current_window_handle)

        async with self.lock:
            try:
                await self.call(open_page)
            except WebDriverException as exc:
                raise ToolFailure(
                    FailureClass.AUTH, "WhatsApp could not load in Firefox. Retry setup."
                ) from exc
        return {
            "status": "browser_open",
            "provider": "whatsapp",
            "browser": "firefox",
            "message": "Link WhatsApp by scanning the QR code in the Firefox window.",
        }

    async def close(self):
        def quit_driver():
            if self.driver:
                try:
                    self.driver.quit()
                except WebDriverException:
                    pass
                self.driver = None

        await asyncio.get_running_loop().run_in_executor(self.executor, quit_driver)
        self.executor.shutdown(wait=False, cancel_futures=True)


def firefox_session(store):
    if not hasattr(store, "firefox_session"):
        store.firefox_session = FirefoxSession(store.settings)
    return store.firefox_session
