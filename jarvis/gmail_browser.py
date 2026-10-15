"""Send through Gmail in Brave, with a durable dispatch fence and Sent read-back.

The adapter uses the English Gmail web interface. Unknown layouts fail closed; it never
falls back to SMTP or accesses a user's ordinary Brave profile.
"""

import asyncio
import re
from urllib.parse import quote, urlparse

from playwright.async_api import Error as BrowserError
from pymongo.errors import DuplicateKeyError

from jarvis.browser_runtime import BrowserTool, session
from jarvis.email_tools import EmailArgs
from jarvis.models import FailureClass, now
from jarvis.tools import ToolFailure

GMAIL_URL = "https://mail.google.com/mail/u/0/"
ACCOUNT_SELECTOR = (
    'a[aria-label*="Google Account"], button[aria-label*="Google Account"], '
    'a[href^="https://accounts.google.com/SignOutOptions"]'
)
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAX_SENT_THREADS = 8


def normalize_body(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def account_from_labels(labels: list[str]) -> str:
    addresses = {address.lower() for label in labels for address in EMAIL_PATTERN.findall(label)}
    if len(addresses) != 1:
        raise ToolFailure(
            FailureClass.AUTH,
            "Could not identify the Gmail account in Brave. Sign in to Gmail, use the English "
            "Gmail interface, then check the connection again.",
        )
    return addresses.pop()


async def gmail_account(page) -> str:
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.hostname != "mail.google.com":
        raise ToolFailure(
            FailureClass.AUTH, "Sign in to Gmail in the Jarvis Brave window, then retry."
        )
    controls = page.locator(ACCOUNT_SELECTOR)
    try:
        await controls.first.wait_for(state="attached", timeout=12000)
    except BrowserError as exc:
        raise ToolFailure(
            FailureClass.AUTH,
            "Gmail is not ready in Brave. Complete sign-in and open the English Gmail inbox.",
        ) from exc
    labels = await controls.evaluate_all(
        "nodes => nodes.map(node => [node.getAttribute('aria-label'), "
        "node.getAttribute('title')].filter(Boolean).join(' '))"
    )
    return account_from_labels(labels)


async def inspect_gmail_account(store) -> str:
    """Read the account visible in the dedicated Brave window, without sending anything."""
    browser = session(store)
    try:
        async with browser.lock:
            page = await browser.page("gmail-setup")
            if urlparse(page.url).hostname != "mail.google.com":
                await page.goto(GMAIL_URL, wait_until="domcontentloaded", timeout=25000)
            await page.bring_to_front()
            return await gmail_account(page)
    except BrowserError as exc:
        raise ToolFailure(
            FailureClass.AUTH,
            "Could not open Gmail in Brave. Open the Gmail connection, complete sign-in, "
            "then verify again.",
        ) from exc


async def require_gmail_account(page, sender: str) -> None:
    if (await gmail_account(page)).casefold() != sender.casefold():
        raise ToolFailure(
            FailureClass.AUTH,
            "The Gmail account open in Brave differs from the approved sender. Switch back "
            "to the connected account or reconnect Gmail and review a new draft.",
        )


async def message_snapshot(page) -> list[dict]:
    """Read Gmail message IDs even for collapsed messages; only expanded bodies can match."""
    return await page.evaluate("""() => {
      const main = Array.from(document.querySelectorAll('[role="main"]'))
        .find(node => node.getClientRects().length);
      if (!main) return [];
      const subject = main.querySelector('h2.hP')?.textContent || '';
      const records = new Map();
      for (const message of main.querySelectorAll(
          '.adn[data-message-id], .adn[data-legacy-message-id]')) {
        if (!message.getClientRects().length) continue;
        const id = message.getAttribute('data-legacy-message-id')
          || message.getAttribute('data-message-id');
        const recipients = [...new Set(Array.from(message.querySelectorAll(
          '.hb [email], .g2[email]')).map(node => node.getAttribute('email').toLowerCase()))];
        const body = message.querySelector('.a3s');
        records.set(id, {id, subject,
          sender:message.querySelector('.gD[email]')?.getAttribute('email')?.toLowerCase() || '',
          recipients, body:body?.getClientRects().length ? body.innerText : null});
      }
      return Array.from(records.values());
    }""")


async def sent_snapshot(page, args: dict) -> list[dict]:
    # Keep the query bounded and escape the subject as a quoted Gmail search phrase.
    # Exact matching below is authoritative; search results alone never prove a send.
    subject = args["subject"].replace("\\", "\\\\").replace('"', '\\"')
    query = f'in:sent newer_than:2d to:{args["recipient"]} subject:"{subject}"'
    await page.goto(
        GMAIL_URL + "#search/" + quote(query, safe=""), wait_until="domcontentloaded", timeout=20000
    )
    # Hash navigation can finish while the old inbox/thread is still in the DOM.
    # Reload to ensure this independent read observes the requested view.
    await page.reload(wait_until="domcontentloaded", timeout=20000)
    await require_gmail_account(page, args["sender"])
    await page.wait_for_function(
        """() => {
      const main = Array.from(document.querySelectorAll('[role="main"]'))
        .find(node => node.getClientRects().length);
      return main && (main.querySelector('tr[role="row"] [data-legacy-thread-id]')
        || /No conversations found|No results found/.test(main.innerText));
    }""",
        timeout=12000,
    )
    threads = await page.locator('[role="main"]:visible').evaluate("""main => Array.from(new Set(
      Array.from(main.querySelectorAll('tr[role="row"] [data-legacy-thread-id]'))
        .map(node => node.getAttribute('data-legacy-thread-id'))))""")
    if len(threads) > MAX_SENT_THREADS:
        raise ToolFailure(
            FailureClass.INPUT,
            "Too many recent Gmail conversations match this subject to verify a new send "
            "safely. Use a more specific subject and review the updated draft.",
        )
    records = []
    for thread in threads:
        if not re.fullmatch(r"[a-fA-F0-9]+", thread or ""):
            raise ToolFailure(FailureClass.LOGIC, "Gmail returned an unsupported conversation ID.")
        await page.goto(GMAIL_URL + "#sent/" + thread, wait_until="domcontentloaded", timeout=15000)
        await page.reload(wait_until="domcontentloaded", timeout=15000)
        await require_gmail_account(page, args["sender"])
        await page.locator(
            '[role="main"]:visible .adn[data-message-id]:visible, '
            '[role="main"]:visible .adn[data-legacy-message-id]:visible'
        ).first.wait_for(state="visible", timeout=10000)
        messages = await message_snapshot(page)
        records.extend(messages)
    return records


async def draft_snapshot(dialog, account: str) -> dict:
    return await dialog.evaluate(
        """(dialog, account) => {
      const fromRow = Array.from(dialog.querySelectorAll('tr')).find(row =>
        /^From:?$/.test(row.querySelector('td')?.textContent?.trim() || ''));
      const explicitFrom = fromRow?.innerText.match(
        /[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}/g) || [];
      const recipients = new Set();
      for (const chip of dialog.querySelectorAll('[email], [data-hovercard-id]')) {
        if (fromRow?.contains(chip)) continue;
        const value = chip.getAttribute('email') || chip.getAttribute('data-hovercard-id');
        if (value?.includes('@')) recipients.add(value.toLowerCase());
      }
      const inputs = Array.from(dialog.querySelectorAll(
        'input[aria-label="To recipients"], input[aria-label="Cc recipients"], '
        + 'input[aria-label="Bcc recipients"], textarea[name="to"], input[name="to"], '
        + 'textarea[name="cc"], input[name="cc"], textarea[name="bcc"], input[name="bcc"]'));
      const body = dialog.querySelector('[contenteditable="true"][aria-label="Message Body"]');
      return {sender:explicitFrom.length === 1 ? explicitFrom[0].toLowerCase()
          : (fromRow ? '' : account), recipients:Array.from(recipients),
        uncommitted:inputs.some(node => node.value.trim()),
        subject:dialog.querySelector('input[name="subjectbox"]')?.value || '',
        body:body ? body.innerText : null};
    }""",
        account,
    )


def exact_message(record: dict, args: dict) -> bool:
    return bool(
        record.get("sender", "").casefold() == args["sender"].casefold()
        and sorted(address.casefold() for address in record.get("recipients", []))
        == [args["recipient"].casefold()]
        and record.get("subject") == args["subject"]
        and isinstance(record.get("body"), str)
        and normalize_body(record["body"]) == normalize_body(args["body"])
    )


async def compose_exact(page, args: dict):
    await page.goto(GMAIL_URL + "#inbox", wait_until="domcontentloaded", timeout=20000)
    await require_gmail_account(page, args["sender"])
    dialogs = page.locator('[role="dialog"]').filter(has=page.locator('input[name="subjectbox"]'))
    if await dialogs.count():
        raise ToolFailure(
            FailureClass.INPUT,
            "Close or save the open compose window in Jarvis Brave before retrying. "
            "Jarvis will not overwrite an existing draft.",
        )
    await page.get_by_role("button", name="Compose", exact=True).click(timeout=10000)
    await dialogs.first.wait_for(state="visible", timeout=10000)
    if await dialogs.count() != 1:
        raise ToolFailure(FailureClass.LOGIC, "Could not identify one Gmail compose window.")
    dialog = dialogs.first
    recipient = dialog.locator(
        'input[aria-label="To recipients"]:visible, textarea[name="to"]:visible, '
        'input[name="to"]:visible'
    )
    if await recipient.count() != 1:
        raise ToolFailure(FailureClass.INPUT, "Gmail's recipient field has an unsupported layout.")
    await recipient.fill(args["recipient"])
    await recipient.press("Enter")
    await dialog.locator('input[name="subjectbox"]').fill(args["subject"])
    await dialog.locator('[contenteditable="true"][aria-label="Message Body"]').fill(args["body"])
    draft = await draft_snapshot(dialog, args["sender"])
    if draft.get("uncommitted") or not exact_message(draft, args):
        raise ToolFailure(
            FailureClass.LOGIC,
            "The Gmail draft differs from the approved sender, recipient, subject or body. "
            "Nothing was sent. Review the compose window in Brave.",
        )
    send = dialog.get_by_role(
        "button",
        name=re.compile(
            r"^Send(?:[\s\u202a-\u202e\u2066-\u2069]*\([^)]*\)[\u202a-\u202e\u2066-\u2069]*)?$"
        ),
    )
    if await send.count() != 1 or not await send.is_enabled():
        raise ToolFailure(FailureClass.LOGIC, "Gmail's Send button could not be safely identified.")
    return dialog, send


class GmailBrowserTool(BrowserTool):
    @property
    def timeout(self) -> float:
        return 150

    async def run(self, args: dict, key: str, store) -> dict:
        sender = store.settings.email_address
        if not sender or args.get("sender", "").casefold() != sender.casefold():
            raise ToolFailure(
                FailureClass.AUTH,
                "Connect Gmail in Brave, then review a draft for the connected sending account.",
            )
        submitted = False
        browser = session(store)
        try:
            async with browser.lock:
                if await store.db.browser_dispatches.find_one({"_id": key}):
                    raise ToolFailure(
                        FailureClass.UNRECOVERABLE,
                        "An earlier Gmail send may have completed. Use Verify again; "
                        "resending this action is blocked.",
                        False,
                    )
                page = await browser.page("gmail-compose")
                await page.goto(GMAIL_URL + "#inbox", wait_until="domcontentloaded", timeout=20000)
                await require_gmail_account(page, sender)
                sent_page = await browser.page("gmail-sent")
                before = await sent_snapshot(sent_page, args)
                dialog, send = await compose_exact(page, args)
                # Recheck identity immediately before creating the irreversible dispatch intent.
                await require_gmail_account(page, sender)
                try:
                    await store.db.browser_dispatches.insert_one(
                        {
                            "_id": key,
                            "provider": "gmail",
                            "state": "dispatching",
                            "args": args,
                            "before_ids": [record["id"] for record in before if record.get("id")],
                            "at": now(),
                        }
                    )
                except DuplicateKeyError as exc:
                    raise ToolFailure(
                        FailureClass.UNRECOVERABLE, "This Gmail send is already in progress.", False
                    ) from exc
                submitted = True
                final_draft = await draft_snapshot(dialog, args["sender"])
                if final_draft.get("uncommitted") or not exact_message(final_draft, args):
                    await store.db.browser_dispatches.update_one(
                        {"_id": key}, {"$set": {"state": "aborted_before_send"}}
                    )
                    raise ToolFailure(
                        FailureClass.LOGIC,
                        "The Gmail draft changed before Send. Nothing was sent. "
                        "Close this draft and start a new task to review the updated email.",
                        False,
                    )
                await send.click(timeout=10000)
                await store.db.browser_dispatches.update_one(
                    {"_id": key}, {"$set": {"state": "submitted"}}
                )
                return {
                    "id": key,
                    "recipient": args["recipient"],
                    "sender": sender,
                    "submission": "Send clicked in Brave; awaiting Gmail Sent verification.",
                }
        except BrowserError as exc:
            raise ToolFailure(
                FailureClass.TRANSIENT,
                "Gmail in Brave did not finish. "
                + (
                    "The send may have completed; use Verify again without resending."
                    if submitted
                    else "No send was submitted. Check the Gmail window and retry."
                ),
                retry_allowed=not submitted,
            ) from exc

    async def _verify(self, args: dict, result: dict, store) -> bool:
        dispatch = await store.db.browser_dispatches.find_one({"_id": result.get("id", "")})
        if not dispatch or dispatch.get("provider") != "gmail" or dispatch.get("args") != args:
            raise ToolFailure(
                FailureClass.LOGIC,
                "No matching Brave dispatch record exists for this email. Check Gmail Sent "
                "manually; Jarvis will not resend it.",
                False,
            )
        if dispatch.get("state") == "aborted_before_send":
            raise ToolFailure(
                FailureClass.LOGIC,
                "This Gmail draft changed before Send, so Jarvis did not send it. "
                "Close the draft and create a new task to review the updated email.",
                False,
            )
        before = set(dispatch.get("before_ids", []))
        browser = session(store)
        async with browser.lock:
            page = await browser.page("gmail-sent")
            for attempt in range(3):
                records = await sent_snapshot(page, args)
                for record in records:
                    if (
                        record.get("id")
                        and record["id"] not in before
                        and exact_message(record, args)
                    ):
                        await store.db.browser_dispatches.update_one(
                            {"_id": dispatch["_id"]},
                            {"$set": {"state": "verified", "message_id": record["id"]}},
                        )
                        result["message_id"] = record["id"]
                        return True
                if attempt < 2:
                    await asyncio.sleep(1)
        raise ToolFailure(
            FailureClass.LOGIC,
            "A new exact copy is not confirmed in Gmail Sent yet. Check the Brave Gmail window "
            "and use Verify again. Jarvis will not automatically resend.",
            False,
        )


GMAIL_BROWSER_TOOL = GmailBrowserTool(
    "email.send",
    "gmail",
    "Send an approved plain-text email through Gmail in the Brave browser. Requires Gmail "
    "sign-in and an explicit recipient, subject and body. Verifies a new exact message in "
    "Gmail Sent; does not prove recipient delivery.",
    EmailArgs,
    "write_irreversible",
    retry_safe=False,
    mode="live",
)
