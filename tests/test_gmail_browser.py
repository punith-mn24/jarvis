import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from jarvis import gmail_browser
from jarvis.gmail_browser import (
    GMAIL_BROWSER_TOOL,
    account_from_labels,
    exact_message,
    inspect_gmail_account,
)
from jarvis.models import FailureClass
from jarvis.tools import ToolFailure

ARGS = {
    "sender": "sender@example.com",
    "recipient": "recipient@example.com",
    "subject": "Review tomorrow",
    "body": "Hello\nPlease review tomorrow.",
}
RECORD = {
    "id": "new-message",
    "sender": ARGS["sender"],
    "recipients": [ARGS["recipient"]],
    "subject": ARGS["subject"],
    "body": ARGS["body"],
}


def test_account_identity_comes_from_one_google_account_control():
    assert account_from_labels(["Google Account: Sender (Sender@example.com)"]) == ARGS["sender"]
    with pytest.raises(ToolFailure):
        account_from_labels(["Sign in"])
    with pytest.raises(ToolFailure):
        account_from_labels(["Google Account: sender@example.com, other@example.com"])


@pytest.mark.parametrize(
    "changed",
    [
        {"sender": "alias@example.com"},
        {"recipients": ["other@example.com"]},
        {"recipients": [ARGS["recipient"], "extra@example.com"]},
        {"subject": "Other subject"},
        {"body": ARGS["body"] + "\nSignature"},
        {"body": None},
    ],
)
def test_sent_match_requires_every_approved_field(changed):
    assert not exact_message({**RECORD, **changed}, ARGS)


def test_only_transport_newlines_are_normalized():
    assert exact_message({**RECORD, "body": ARGS["body"].replace("\n", "\r\n") + "\r\n"}, ARGS)
    assert not exact_message({**RECORD, "body": ARGS["body"] + " "}, ARGS)


async def test_connection_inspection_maps_browser_errors_to_actionable_auth():
    store = SimpleNamespace(
        browser_session=SimpleNamespace(
            lock=asyncio.Lock(), page=AsyncMock(side_effect=BrowserError("closed"))
        )
    )
    with pytest.raises(ToolFailure) as error:
        await inspect_gmail_account(store)
    assert error.value.kind == FailureClass.AUTH
    assert "Brave" in str(error.value)


def setup_browser(store, monkeypatch):
    store.settings.email_address = ARGS["sender"]
    page = SimpleNamespace(goto=AsyncMock())
    browser = SimpleNamespace(lock=asyncio.Lock(), page=AsyncMock(return_value=page))
    store.browser_session = browser
    check = AsyncMock()
    before = AsyncMock(return_value=[{**RECORD, "id": "old-message"}])
    send = SimpleNamespace(click=AsyncMock())
    compose = AsyncMock(return_value=(object(), send))
    monkeypatch.setattr(gmail_browser, "require_gmail_account", check)
    monkeypatch.setattr(gmail_browser, "sent_snapshot", before)
    monkeypatch.setattr(gmail_browser, "compose_exact", compose)
    monkeypatch.setattr(gmail_browser, "draft_snapshot", AsyncMock(return_value=RECORD))
    return browser, check, before, compose, send


async def test_send_fences_before_click_and_requires_new_independent_sent_message(
    store, monkeypatch
):
    browser, check, snapshots, compose, send = setup_browser(store, monkeypatch)

    async def click(**kwargs):
        dispatch = await store.db.browser_dispatches.find_one({"_id": "task:s1"})
        assert dispatch["state"] == "dispatching"
        assert dispatch["args"] == ARGS
        assert dispatch["before_ids"] == ["old-message"]

    send.click.side_effect = click
    result = await GMAIL_BROWSER_TOOL.run(ARGS, "task:s1", store)
    assert send.click.await_count == 1
    assert result["recipient"] == ARGS["recipient"]
    assert browser.page.call_args_list[0].args == ("gmail-compose",)
    snapshots.return_value = [{**RECORD, "id": "old-message"}, RECORD]
    assert await GMAIL_BROWSER_TOOL.verify(ARGS, result, store)
    assert result["message_id"] == "new-message"
    dispatch = await store.db.browser_dispatches.find_one({"_id": "task:s1"})
    assert dispatch["state"] == "verified"
    with pytest.raises(ToolFailure) as error:
        await GMAIL_BROWSER_TOOL.run(ARGS, "task:s1", store)
    assert not error.value.retry_allowed
    assert send.click.await_count == 1


async def test_wrong_signed_in_account_stops_before_composing(store, monkeypatch):
    _, check, snapshots, compose, send = setup_browser(store, monkeypatch)
    check.side_effect = ToolFailure(FailureClass.AUTH, "Wrong account")
    with pytest.raises(ToolFailure):
        await GMAIL_BROWSER_TOOL.run(ARGS, "task:s1", store)
    compose.assert_not_awaited()
    send.click.assert_not_awaited()
    snapshots.assert_not_awaited()
    assert await store.db.browser_dispatches.count_documents({}) == 0


async def test_final_draft_change_never_clicks_send(store, monkeypatch):
    *_, send = setup_browser(store, monkeypatch)
    monkeypatch.setattr(
        gmail_browser,
        "draft_snapshot",
        AsyncMock(return_value={**RECORD, "recipients": ["unexpected@example.com"]}),
    )
    with pytest.raises(ToolFailure) as error:
        await GMAIL_BROWSER_TOOL.run(ARGS, "task:s1", store)
    assert not error.value.retry_allowed
    send.click.assert_not_awaited()
    dispatch = await store.db.browser_dispatches.find_one({"_id": "task:s1"})
    assert dispatch["state"] == "aborted_before_send"


async def test_explicitly_aborted_send_cannot_be_verified_from_another_sent_copy(
    store, monkeypatch
):
    browser, _, snapshots, _, _ = setup_browser(store, monkeypatch)
    await store.db.browser_dispatches.insert_one(
        {
            "_id": "task:s1",
            "provider": "gmail",
            "state": "aborted_before_send",
            "args": ARGS,
            "before_ids": [],
        }
    )
    snapshots.return_value = [RECORD]
    with pytest.raises(ToolFailure) as error:
        await GMAIL_BROWSER_TOOL.verify(ARGS, {"id": "task:s1"}, store)
    assert not error.value.retry_allowed
    assert "did not send" in str(error.value)
    browser.page.assert_not_awaited()


async def test_uncertain_click_blocks_resend_but_allows_sent_readback(store, monkeypatch):
    _, _, snapshots, _, send = setup_browser(store, monkeypatch)
    send.click.side_effect = BrowserError("page closed after click")
    with pytest.raises(ToolFailure) as error:
        await GMAIL_BROWSER_TOOL.run(ARGS, "task:s1", store)
    assert not error.value.retry_allowed
    with pytest.raises(ToolFailure):
        await GMAIL_BROWSER_TOOL.run(ARGS, "task:s1", store)
    assert send.click.await_count == 1
    snapshots.return_value = [RECORD]
    assert await GMAIL_BROWSER_TOOL.verify(ARGS, {"id": "task:s1"}, store)


async def test_existing_sent_copy_does_not_verify_a_new_send(store, monkeypatch):
    _, _, snapshots, _, _ = setup_browser(store, monkeypatch)
    result = await GMAIL_BROWSER_TOOL.run(ARGS, "task:s1", store)
    # This existing message has identical content but was present before Send.
    snapshots.return_value = [{**RECORD, "id": "old-message"}]
    monkeypatch.setattr(gmail_browser, "asyncio", SimpleNamespace(sleep=AsyncMock()))
    with pytest.raises(ToolFailure) as error:
        await GMAIL_BROWSER_TOOL.verify(ARGS, result, store)
    assert not error.value.retry_allowed
    assert "not confirmed" in str(error.value)


async def test_readback_rejects_changed_arguments(store, monkeypatch):
    browser, _, _, _, _ = setup_browser(store, monkeypatch)
    result = await GMAIL_BROWSER_TOOL.run(ARGS, "task:s1", store)
    browser.page.reset_mock()
    with pytest.raises(ToolFailure) as error:
        await GMAIL_BROWSER_TOOL.verify({**ARGS, "body": "different"}, result, store)
    assert not error.value.retry_allowed
    browser.page.assert_not_awaited()


async def test_sent_views_reload_after_hash_navigation(monkeypatch):
    message_locator = SimpleNamespace(first=SimpleNamespace(wait_for=AsyncMock()))
    main_locator = SimpleNamespace(evaluate=AsyncMock(return_value=["abc123", "abc456"]))
    page = SimpleNamespace(
        goto=AsyncMock(),
        reload=AsyncMock(),
        wait_for_function=AsyncMock(),
        locator=lambda selector: (
            main_locator if selector == '[role="main"]:visible' else message_locator
        ),
    )
    monkeypatch.setattr(gmail_browser, "require_gmail_account", AsyncMock())
    monkeypatch.setattr(gmail_browser, "message_snapshot", AsyncMock(return_value=[RECORD]))
    assert len(await gmail_browser.sent_snapshot(page, ARGS)) == 2
    # Search plus each separate thread is a fresh document, preventing stale baselines.
    assert page.goto.await_count == page.reload.await_count == 3


@pytest.fixture
async def gmail_dom_page():
    """Local HTML fixtures only. No request is made to Gmail or any other external service."""
    driver = await async_playwright().start()
    try:
        try:
            browser = await driver.chromium.launch(headless=True)
        except BrowserError:
            pytest.skip("Install Playwright Chromium to run local Gmail DOM fixtures.")
        page = await browser.new_page()
        await page.route("**/*", lambda route: route.abort())
        yield page
        await browser.close()
    finally:
        await driver.stop()


async def test_sent_dom_reads_ids_exact_headers_and_visible_body(gmail_dom_page):
    page = gmail_dom_page
    await page.set_content("""
      <main role="main">
        <h2 class="hP">Review tomorrow</h2>
        <div class="adn" data-legacy-message-id="old-message">
          <span class="gD" email="sender@example.com">Sender</span>
          <div class="hb"><span email="recipient@example.com">Recipient</span></div>
          <div class="a3s" style="display:none">Collapsed old message</div>
        </div>
        <div class="adn" data-message-id="#msg-f:123" data-legacy-message-id="new-message">
          <span class="gD" email="sender@example.com">Sender</span>
          <div class="hb"><span email="recipient@example.com">Recipient</span></div>
          <div class="a3s">Hello<br>Please review tomorrow.</div>
        </div>
        <div class="adn" data-legacy-message-id="hidden-cache" style="display:none">Cached</div>
      </main>
    """)
    messages = await gmail_browser.message_snapshot(page)
    assert [message["id"] for message in messages] == ["old-message", "new-message"]
    assert messages[0]["body"] is None
    assert messages[1] == RECORD


async def test_draft_dom_detects_extra_recipient_and_sender_alias(gmail_dom_page):
    page = gmail_dom_page
    await page.set_content("""
      <div role="dialog">
        <table><tr><td>From</td><td><span email="alias@example.com">alias@example.com</span></td>
        </tr></table>
        <span email="recipient@example.com">Recipient</span>
        <span data-hovercard-id="extra@example.com">Extra</span>
        <input name="to" value="">
        <input name="subjectbox" value="Review tomorrow">
        <div contenteditable="true" aria-label="Message Body">Hello<br>Please review tomorrow.</div>
      </div>
    """)
    draft = await gmail_browser.draft_snapshot(page.locator('[role="dialog"]'), ARGS["sender"])
    assert draft["sender"] == "alias@example.com"
    assert draft["recipients"] == ["recipient@example.com", "extra@example.com"]
    assert not exact_message(draft, ARGS)


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
async def test_draft_dom_detects_pending_addresses_in_every_recipient_field(gmail_dom_page, field):
    page = gmail_dom_page
    await page.set_content(f"""
      <div role="dialog">
        <span email="recipient@example.com">Recipient</span>
        <input name="{field}" value="unapproved@example.com">
        <input name="subjectbox" value="Review tomorrow">
        <div contenteditable="true" aria-label="Message Body">Hello<br>Please review tomorrow.</div>
      </div>
    """)
    draft = await gmail_browser.draft_snapshot(page.locator('[role="dialog"]'), ARGS["sender"])
    # Pending Cc/Bcc entries are committed by Gmail when Send is clicked.
    assert draft["uncommitted"]
