import asyncio
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from selenium.webdriver.common.keys import Keys

from jarvis import whatsapp_tools as whatsapp
from jarvis.firefox_runtime import FirefoxSession
from jarvis.tools import ToolFailure


@pytest.mark.parametrize("body", ["hello" + Keys.ENTER, "hello\tthere", "\r", Keys.CONTROL + "a"])
def test_webdriver_control_tokens_cannot_be_message_text(body):
    with pytest.raises(ValidationError):
        whatsapp.WhatsAppArgs(phone="+919876543210", body=body)
    assert whatsapp.WhatsAppArgs(phone="+919876543210", body="hello\nthere").body == "hello\nthere"


async def test_invalid_message_is_rejected_before_opening_firefox(store):
    browser = SimpleNamespace(lock=asyncio.Lock(), call=AsyncMock())
    store.firefox_session = browser
    with pytest.raises(ToolFailure):
        await whatsapp.WHATSAPP_TOOL.run(
            {"phone": "+919876543210", "body": Keys.ENTER}, "task:s1", store
        )
    browser.call.assert_not_called()
    assert await store.db.browser_dispatches.count_documents({}) == 0


async def test_cancellation_drains_thread_and_blocks_later_send_command(store, monkeypatch):
    session = FirefoxSession(store.settings)
    entered, released = Event(), Event()
    commands = []
    driver = SimpleNamespace(execute=lambda command: commands.append(command))
    monkeypatch.setattr(session, "_driver", lambda: driver)

    def operation(browser):
        entered.set()
        released.wait(3)
        browser.execute("send-message")

    task = asyncio.create_task(session.call(operation))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()  # Caller still owns browser until the thread drains.
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert commands == []
        await session.call(lambda browser: browser.execute("read-state"))
        assert commands == ["read-state"]
    finally:
        released.set()
        session.executor.shutdown(wait=True)


def message(identifier, body="hi", acknowledged=True):
    item = MagicMock()
    item.get_attribute.side_effect = lambda attr: (
        "[17:42, 15/10/2026] Me: " if attr == "data-pre-plain-text" else identifier
    )
    item.find_elements.side_effect = lambda by, selector: (
        [SimpleNamespace(text=body)]
        if selector == ".selectable-text"
        else []
        if selector == "[data-pre-plain-text]"
        else [object()]
        if acknowledged
        else []
    )
    return item


def test_old_message_rendered_before_anchor_is_not_captured(monkeypatch):
    old, anchor, current = message("old-history"), message("anchor"), message("this-send")
    monkeypatch.setattr(whatsapp, "chat_identity", lambda *args: True)
    nodes = [old, anchor]
    monkeypatch.setattr(whatsapp, "elements", lambda *args: nodes)
    args = {"phone": "+919876543210", "body": "hi"}
    assert not whatsapp.capture_submission(
        object(), args, ["anchor"], datetime(2026, 10, 15, 17, 42)
    )
    nodes.append(current)
    assert (
        whatsapp.capture_submission(object(), args, ["anchor"], datetime(2026, 10, 15, 17, 42))
        == "this-send"
    )
    assert whatsapp.find_sent(object(), args, "this-send") == "this-send"
    nodes.pop()
    assert not whatsapp.find_sent(object(), args, "this-send")


async def test_crash_before_submission_id_never_matches_old_messages(store):
    args = {"phone": "+919876543210", "body": "hi"}
    await store.db.browser_dispatches.insert_one(
        {"_id": "task:s1", "provider": "whatsapp", "args": args, "before": []}
    )
    with pytest.raises(ToolFailure, match="exact Firefox submission ID") as failure:
        await whatsapp.WHATSAPP_TOOL.verify(args, {"id": "task:s1"}, store)
    assert not failure.value.retry_allowed
    assert not hasattr(store, "firefox_session")


def test_stale_message_timestamp_cannot_be_captured():
    item = message("history")
    assert whatsapp.recent_message(item, datetime(2026, 10, 15, 17, 42))
    assert not whatsapp.recent_message(item, datetime(2026, 10, 15, 17, 43))
    assert not whatsapp.recent_message(item, datetime(2026, 10, 16, 17, 42))
