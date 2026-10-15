"""WhatsApp Web messages exclusively through the installed Firefox app."""

import re
from datetime import datetime
from urllib.parse import urlencode

from pydantic import Field, field_validator
from pymongo.errors import DuplicateKeyError
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from jarvis.firefox_runtime import firefox_session
from jarvis.models import FailureClass, now
from jarvis.tools import Args, Tool, ToolFailure


class WhatsAppArgs(Args):
    phone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    body: str = Field(min_length=1, max_length=4000)

    @field_validator("body")
    @classmethod
    def literal_message_only(cls, value):
        if any(0xE000 <= ord(c) <= 0xF8FF or (ord(c) < 32 and c != "\n") for c in value):
            raise ValueError("Message contains unsupported keyboard control characters.")
        return value


EDITOR = '#main footer [contenteditable="true"][role="textbox"]'
OUTGOING = (
    "#main .message-out [data-id], #main [data-id].message-out, #main [data-id]:has(.message-out)"
)


def elements(driver, selector):
    return driver.find_elements(By.CSS_SELECTOR, selector)


def chat_identity(driver, phone):
    digits = phone.lstrip("+")
    for item in elements(driver, "#main [data-id]")[-30:]:
        identifier = item.get_attribute("data-id") or ""
        if re.search(rf"(?:true|false)_{re.escape(digits)}@c\.us_", identifier):
            return True
    for item in elements(driver, "#main header"):
        if re.sub(r"\D", "", item.text) == digits:
            return True
    # Modern WhatsApp can use opaque @lid IDs. Never treat those as a phone match.
    return False


def open_chat(driver, args):
    args = WhatsAppArgs.model_validate(args).model_dump()
    driver.get("https://web.whatsapp.com/send?" + urlencode({"phone": args["phone"].lstrip("+")}))
    try:
        WebDriverWait(driver, 25).until(
            lambda d: elements(d, EDITOR) or elements(d, 'canvas[aria-label*="QR"]')
        )
    except TimeoutException as exc:
        raise ToolFailure(
            FailureClass.AUTH, "WhatsApp chat did not load in Firefox. Link your phone and retry."
        ) from exc
    if not elements(driver, EDITOR):
        raise ToolFailure(FailureClass.AUTH, "Scan the WhatsApp QR code in Firefox, then retry.")
    if not chat_identity(driver, args["phone"]):
        raise ToolFailure(
            FailureClass.INPUT,
            "WhatsApp did not expose a matching phone number for this chat. "
            "Open the contact with its full number visible in Firefox; nothing was sent.",
        )
    before = [item.get_attribute("data-id") for item in elements(driver, "#main [data-id]")]
    editor = elements(driver, EDITOR)[0]
    editor.click()
    editor.send_keys(Keys.CONTROL, "a")
    editor.send_keys(Keys.BACKSPACE)
    # Newlines use Shift+Enter. Plain Enter would send an unapproved partial message.
    lines = args["body"].split("\n")
    for index, line in enumerate(lines):
        if index:
            editor.send_keys(Keys.SHIFT, Keys.ENTER)
        editor.send_keys(line)
    draft = editor.get_attribute("innerText").replace("\r\n", "\n")
    if draft != args["body"]:
        raise ToolFailure(
            FailureClass.INPUT,
            "WhatsApp draft text did not match the approved message. Nothing was sent.",
        )
    return [identifier for identifier in before if identifier]


def send_draft(driver, args, before):
    if not chat_identity(driver, args["phone"]):
        raise ToolFailure(
            FailureClass.INPUT, "The chat changed before sending. Submission stopped.", False
        )
    editors = elements(driver, EDITOR)
    if not editors or editors[0].get_attribute("innerText").replace("\r\n", "\n") != args["body"]:
        raise ToolFailure(
            FailureClass.INPUT, "The draft changed before sending. Submission stopped.", False
        )
    buttons = elements(driver, '#main footer [data-icon="send"]')
    if len(buttons) != 1:
        raise ToolFailure(
            FailureClass.INPUT, "The WhatsApp Send control could not be identified.", False
        )
    # Refresh the baseline after typing: older history can load while the draft is entered.
    before = [item.get_attribute("data-id") for item in elements(driver, "#main [data-id]")]
    local_time = driver.execute_script(
        "const d = new Date(); return [d.getFullYear(), d.getMonth()+1, d.getDate(), "
        "d.getHours(), d.getMinutes()];"
    )
    started = datetime(*local_time)
    buttons[0].click()
    # Capture this submission's new outgoing ID in the same open chat. Verification
    # later requires that exact ID, never an arbitrary identical message from history.
    return WebDriverWait(driver, 10).until(lambda d: capture_submission(d, args, before, started))


def recent_message(item, started):
    metadata = [item, *item.find_elements(By.CSS_SELECTOR, "[data-pre-plain-text]")]
    for node in metadata:
        text = node.get_attribute("data-pre-plain-text") or ""
        match = re.match(
            r"\[(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM)?[,]?\s+"
            r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\]",
            text,
            re.I,
        )
        if not match:
            continue
        hour, minute, period, first, second, year = match.groups()
        hour, minute, first, second, year = map(int, (hour, minute, first, second, year))
        if period:
            hour = hour % 12 + (12 if period.upper() == "PM" else 0)
        if year < 100:
            year += 2000
        # WhatsApp uses either day/month or month/day depending on interface locale.
        for month, day in ((first, second), (second, first)):
            try:
                value = datetime(year, month, day, hour, minute)
            except ValueError:
                continue
            if 0 <= (value - started).total_seconds() <= 120:
                return True
    return False


def capture_submission(driver, args, before, started):
    if not chat_identity(driver, args["phone"]):
        return False
    ids = [item.get_attribute("data-id") for item in elements(driver, "#main [data-id]")]
    anchor = before[-1] if before else None
    if anchor and anchor not in ids:
        return False
    candidates = []
    for item in reversed(elements(driver, OUTGOING)):
        identifier = item.get_attribute("data-id")
        if not identifier or identifier in before:
            continue
        if anchor and (identifier not in ids or ids.index(identifier) <= ids.index(anchor)):
            continue
        bodies = [node.text for node in item.find_elements(By.CSS_SELECTOR, ".selectable-text")]
        if args["body"] in bodies and recent_message(item, started):
            candidates.append(identifier)
    candidates = set(candidates)
    return candidates.pop() if len(candidates) == 1 else False


def find_sent(driver, args, message_id):
    if not chat_identity(driver, args["phone"]):
        return False
    for item in elements(driver, OUTGOING):
        if item.get_attribute("data-id") != message_id:
            continue
        bodies = [node.text for node in item.find_elements(By.CSS_SELECTOR, ".selectable-text")]
        marks = item.find_elements(
            By.CSS_SELECTOR, '[data-icon="msg-check"], [data-icon="msg-dblcheck"]'
        )
        if args["body"] in bodies and marks:
            return message_id
    return False


class WhatsAppTool(Tool):
    @property
    def timeout(self):
        return 120

    async def run(self, args, key, store):
        args = self.validate(args)
        browser = firefox_session(store)
        async with browser.lock:
            previous = await store.db.browser_dispatches.find_one({"_id": key})
            if previous:
                raise ToolFailure(
                    FailureClass.UNRECOVERABLE,
                    "An earlier WhatsApp send is uncertain. Verify again; resending is blocked.",
                    False,
                )
            try:
                before = await browser.call(open_chat, args)
                try:
                    await store.db.browser_dispatches.insert_one(
                        {
                            "_id": key,
                            "provider": "whatsapp",
                            "browser": "firefox",
                            "before": before,
                            "phone": args["phone"],
                            "args": args,
                            "at": now(),
                        }
                    )
                except DuplicateKeyError as exc:
                    raise ToolFailure(
                        FailureClass.UNRECOVERABLE, "Message submission already started.", False
                    ) from exc
                identifier = await browser.call(send_draft, args, before)
                if not identifier:
                    raise ToolFailure(FailureClass.LOGIC, "Submission ID was not confirmed.", False)
                await store.db.browser_dispatches.update_one(
                    {"_id": key}, {"$set": {"message_id": identifier, "state": "submitted"}}
                )
                return {
                    "id": key,
                    "phone": args["phone"],
                    "browser": "firefox",
                    "message_id": identifier,
                }
            except WebDriverException as exc:
                dispatched = await store.db.browser_dispatches.find_one({"_id": key})
                raise ToolFailure(
                    FailureClass.TRANSIENT,
                    "WhatsApp in Firefox did not respond. Reconnect or verify again.",
                    dispatched is None,
                ) from exc

    async def verify(self, args, result, store):
        dispatch = await store.db.browser_dispatches.find_one({"_id": result["id"]})
        if (
            not dispatch
            or dispatch.get("provider") != "whatsapp"
            or dispatch.get("args") != args
            or not dispatch.get("message_id")
        ):
            raise ToolFailure(
                FailureClass.LOGIC,
                "No exact Firefox submission ID was captured. Check the chat manually; "
                "Jarvis will not resend or match an older message.",
                False,
            )
        browser = firefox_session(store)
        async with browser.lock:

            def check(driver):
                # Read-only navigation also supports recovery after Firefox restarts.
                if not elements(driver, EDITOR) or not chat_identity(driver, args["phone"]):
                    driver.get(
                        "https://web.whatsapp.com/send?"
                        + urlencode({"phone": args["phone"].lstrip("+")})
                    )
                return WebDriverWait(driver, 15).until(
                    lambda d: find_sent(d, args, dispatch["message_id"])
                )

            try:
                return bool(await browser.call(check))
            except (WebDriverException, ToolFailure) as exc:
                raise ToolFailure(
                    FailureClass.LOGIC,
                    "No new matching sent message was confirmed in Firefox. "
                    "Outcome is unknown; use Verify again, not resend.",
                    False,
                ) from exc


WHATSAPP_TOOL = WhatsAppTool(
    "whatsapp.send",
    "whatsapp",
    "Send WhatsApp through Firefox only. Requires linked phone, exact international number, "
    "approved text, and a new matching outgoing message with a sent checkmark.",
    WhatsAppArgs,
    "write_irreversible",
    retry_safe=False,
    mode="live",
)
