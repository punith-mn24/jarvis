import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis import browser_runtime, firefox_runtime, whatsapp_tools
from jarvis.browser_runtime import BrowserSession
from jarvis.firefox_runtime import FirefoxSession
from jarvis.tools import ToolFailure


async def test_gmail_launches_brave_and_amazon_uses_separate_profile(store, monkeypatch, tmp_path):
    context = MagicMock()
    launch = AsyncMock(return_value=context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(
        browser_runtime,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=playwright)),
    )
    monkeypatch.setattr(browser_runtime, "browser_binary", lambda *args: "/opt/brave/brave")
    store.settings.brave_profile = str(tmp_path / "brave")
    store.settings.browser_profile = str(tmp_path / "amazon")
    session = BrowserSession(store.settings)
    await session.start("gmail")
    await session.start("amazon")
    gmail, amazon = launch.await_args_list
    assert gmail.kwargs["executable_path"] == "/opt/brave/brave"
    assert "executable_path" not in amazon.kwargs
    assert gmail.args[0] != amazon.args[0]
    with pytest.raises(ToolFailure, match="Firefox"):
        await session.page("whatsapp")


async def test_missing_brave_never_falls_back_to_chromium(store, monkeypatch, tmp_path):
    store.settings.brave_profile = str(tmp_path / "brave")

    def missing(*args):
        raise ToolFailure("AUTH", "Brave is missing")

    monkeypatch.setattr(browser_runtime, "browser_binary", missing)
    start = AsyncMock()
    monkeypatch.setattr(browser_runtime, "async_playwright", start)
    with pytest.raises(ToolFailure, match="Brave"):
        await BrowserSession(store.settings).page("gmail-setup")
    start.assert_not_called()


def test_whatsapp_uses_installed_firefox_persistent_profile(store, monkeypatch, tmp_path):
    store.settings.firefox_profile = str(tmp_path / "firefox")
    monkeypatch.setattr(firefox_runtime, "browser_binary", lambda *args: "/usr/bin/firefox")
    create = MagicMock()
    monkeypatch.setattr(firefox_runtime.webdriver, "Firefox", create)
    session = FirefoxSession(store.settings)
    session._driver()
    options = create.call_args.kwargs["options"]
    assert options.binary_location == "/usr/bin/firefox"
    assert options.arguments[options.arguments.index("-profile") + 1] == str(tmp_path / "firefox")
    assert options.profile is None  # No temporary profile clone losing QR login.
    session.executor.shutdown(wait=False)


async def test_connection_routes_whatsapp_to_firefox_only(client, store, monkeypatch):
    firefox = SimpleNamespace(connect=AsyncMock(return_value={"browser": "firefox"}))
    other = SimpleNamespace(connect=AsyncMock(return_value={"browser": "brave"}))
    store.firefox_session = firefox
    store.browser_session = other
    result = await client.post("/api/connections/browser/whatsapp")
    assert result.status_code == 200 and result.json()["browser"] == "firefox"
    firefox.connect.assert_awaited_once()
    other.connect.assert_not_called()
    result = await client.post("/api/connections/browser/gmail")
    assert result.status_code == 200 and result.json()["browser"] == "brave"
    other.connect.assert_awaited_once_with("gmail")


async def test_gmail_browser_verification_saves_only_observed_address(client, store, monkeypatch):
    from jarvis import main

    inspect = AsyncMock(return_value="verified@example.com")
    persist = MagicMock()
    monkeypatch.setattr(main, "inspect_gmail_account", inspect)
    monkeypatch.setattr(main, "persist_gmail", persist)
    result = await client.post("/api/connections/gmail/verify")
    assert result.status_code == 200
    assert result.json()["browser"] == "brave"
    assert store.settings.email_address == "verified@example.com"
    persist.assert_called_once_with("verified@example.com")
    # The old password-based endpoint is gone.
    assert (await client.post("/api/connections/gmail", json={})).status_code == 404


async def test_whatsapp_send_and_readback_stay_on_firefox(store, monkeypatch):
    calls = []

    async def call(function, *args):
        calls.append(function)
        return ["old-message"] if function is whatsapp_tools.open_chat else "new-message"

    browser = SimpleNamespace(lock=asyncio.Lock(), call=AsyncMock(side_effect=call))
    store.firefox_session = browser
    args = {"phone": "+919876543210", "body": "hello"}
    result = await whatsapp_tools.WHATSAPP_TOOL.run(args, "task:s1", store)
    assert result["browser"] == "firefox"
    assert calls == [whatsapp_tools.open_chat, whatsapp_tools.send_draft]
    assert not hasattr(store, "browser_session")
    with pytest.raises(ToolFailure):
        await whatsapp_tools.WHATSAPP_TOOL.run(args, "task:s1", store)
    assert len(calls) == 2
