"""Real Gmail SMTP sends, independently verified through the IMAP Sent mailbox."""

import asyncio
import hashlib
import imaplib
import smtplib
import ssl
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses

from pydantic import EmailStr, Field, field_validator
from pymongo.errors import DuplicateKeyError

from jarvis.config import Settings
from jarvis.models import FailureClass, now
from jarvis.store import MongoStore
from jarvis.tools import Args, CreateNote, Tool, ToolFailure


class EmailArgs(Args):
    sender: str = ""
    recipient: EmailStr
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)

    @field_validator("subject")
    @classmethod
    def no_header_injection(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Subject must be a single line.")
        return value


def message_id(key: str) -> str:
    return f"<jarvis.{hashlib.sha256(key.encode()).hexdigest()}@jarvis.local>"


def require_account(settings: Settings) -> None:
    if not settings.email_configured:
        raise ToolFailure(FailureClass.AUTH, "Connect Gmail in Connections before sending email.")


def smtp_session(settings: Settings):
    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        session = smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.email_timeout,
            context=context,
        )
    else:
        session = smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.email_timeout
        )
        session.starttls(context=context)
    try:
        session.login(settings.email_address, settings.email_password.get_secret_value())
    except BaseException:
        session.close()
        raise
    return session


def imap_session(settings: Settings):
    session = imaplib.IMAP4_SSL(
        settings.imap_host,
        settings.imap_port,
        ssl_context=ssl.create_default_context(),
        timeout=settings.email_timeout,
    )
    try:
        session.login(settings.email_address, settings.email_password.get_secret_value())
        status, _ = session.select('"' + settings.sent_folder.replace('"', "") + '"', readonly=True)
        if status != "OK":
            raise ToolFailure(
                FailureClass.INPUT, "Sent folder unavailable. Check JARVIS_SENT_FOLDER."
            )
    except BaseException:
        session.shutdown()
        raise
    return session


def check_account_sync(settings: Settings) -> None:
    require_account(settings)
    try:
        with imap_session(settings):
            pass
        with smtp_session(settings):
            pass
    except ToolFailure:
        raise
    except (smtplib.SMTPAuthenticationError, imaplib.IMAP4.error) as exc:
        raise ToolFailure(
            FailureClass.AUTH,
            "Gmail sign-in failed. Use a Google app password, not your normal password.",
        ) from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise ToolFailure(
            FailureClass.TRANSIENT, "Cannot reach Gmail securely. Try again."
        ) from exc


def lookup_sent_sync(settings: Settings, key: str, args: dict) -> bool:
    require_account(settings)
    try:
        with imap_session(settings) as session:
            status, ids = session.uid(
                "search", None, "HEADER", "Message-ID", '"' + message_id(key) + '"'
            )
            if status != "OK":
                raise ToolFailure(FailureClass.TRANSIENT, "Could not search the Sent folder.")
            for uid in (ids[0] or b"").split()[-5:]:
                status, data = session.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                raw = next((part[1] for part in data if isinstance(part, tuple)), None)
                if not raw:
                    continue
                email = BytesParser(policy=policy.default).parsebytes(raw)
                body = email.get_body(preferencelist=("plain",))
                if body is None:
                    continue
                recipients = [
                    address.lower() for _, address in getaddresses(email.get_all("To", []))
                ]
                senders = [
                    address.lower() for _, address in getaddresses(email.get_all("From", []))
                ]

                def normalize(text):
                    return text.replace("\r\n", "\n").rstrip("\n")

                if (
                    str(email.get("Message-ID", "")) == message_id(key)
                    and recipients == [args["recipient"].lower()]
                    and senders == [settings.email_address.lower()]
                    and str(email.get("Subject", "")) == args["subject"]
                    and normalize(body.get_content()) == normalize(args["body"])
                ):
                    return True
            return False
    except ToolFailure:
        raise
    except imaplib.IMAP4.error as exc:
        raise ToolFailure(
            FailureClass.AUTH, "Cannot read Gmail Sent. Check the app password."
        ) from exc
    except OSError as exc:
        raise ToolFailure(FailureClass.TRANSIENT, "Gmail Sent read-back could not finish.") from exc


def send_sync(settings: Settings, key: str, args: dict) -> None:
    email = EmailMessage()
    email["From"] = settings.email_address
    email["To"] = args["recipient"]
    email["Subject"] = args["subject"]
    email["Message-ID"] = message_id(key)
    email["Date"] = format_datetime(now())
    email.set_content(args["body"])
    submitting = False
    try:
        with smtp_session(settings) as session:
            submitting = True
            refused = session.send_message(email)
            if refused:
                raise ToolFailure(FailureClass.INPUT, "Gmail refused the recipient.")
    except smtplib.SMTPAuthenticationError as exc:
        raise ToolFailure(
            FailureClass.AUTH, "Gmail authentication failed. Reconnect your account."
        ) from exc
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
        raise ToolFailure(
            FailureClass.INPUT, "Gmail explicitly rejected this message. Review the draft."
        ) from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise ToolFailure(
            FailureClass.TRANSIENT,
            "Gmail connection interrupted. "
            + (
                "The message may have been sent. Check Sent; resending is blocked."
                if submitting
                else "No message was submitted."
            ),
            retry_allowed=not submitting,
        ) from exc


class EmailTool(Tool):
    @property
    def timeout(self) -> float:
        return 45

    async def run(self, args: dict, key: str, store: MongoStore) -> dict:
        settings = store.settings
        require_account(settings)
        if args.get("sender") != settings.email_address:
            raise ToolFailure(
                FailureClass.INPUT, "The sending account changed. Review a new draft."
            )
        result = {"id": key, "message_id": message_id(key), "recipient": args["recipient"]}
        if await asyncio.to_thread(lookup_sent_sync, settings, key, args):
            return result
        # Durable dispatch intent blocks a second SMTP send after crashes/timeouts.
        dispatch = await store.db.email_dispatches.find_one({"_id": key})
        if dispatch:
            if dispatch["state"] != "not_sent":
                raise ToolFailure(
                    FailureClass.UNRECOVERABLE,
                    "An earlier send may have completed. Check Sent; resending is blocked.",
                    retry_allowed=False,
                )
            claim = await store.db.email_dispatches.update_one(
                {"_id": key, "state": "not_sent"}, {"$set": {"state": "sending", "at": now()}}
            )
            if not claim.modified_count:
                raise ToolFailure(
                    FailureClass.UNRECOVERABLE, "This send is already in progress.", False
                )
        else:
            try:
                await store.db.email_dispatches.insert_one(
                    {"_id": key, "state": "sending", "at": now()}
                )
            except DuplicateKeyError as exc:
                raise ToolFailure(
                    FailureClass.UNRECOVERABLE, "This send is already in progress.", False
                ) from exc
        try:
            await asyncio.to_thread(send_sync, settings, key, args)
        except ToolFailure as exc:
            if exc.retry_allowed:
                await store.db.email_dispatches.update_one(
                    {"_id": key}, {"$set": {"state": "not_sent"}}
                )
            raise
        await store.db.email_dispatches.update_one({"_id": key}, {"$set": {"state": "accepted"}})
        return result

    async def verify(self, args: dict, result: dict, store: MongoStore) -> bool:
        for attempt in range(3):
            try:
                if await asyncio.to_thread(lookup_sent_sync, store.settings, result["id"], args):
                    return True
            except ToolFailure as exc:
                exc.retry_allowed = False
                raise
            if attempt < 2:
                await asyncio.sleep(0.5)
        raise ToolFailure(
            FailureClass.LOGIC,
            "SMTP submission is not confirmed in Sent yet. Delivery is unknown. "
            "Use Check Sent again; Jarvis will not automatically resend.",
            retry_allowed=False,
        )


LIVE_TOOLS = {
    "email.send": EmailTool(
        "email.send",
        "gmail",
        "Send a real plain-text email from the connected Gmail account. "
        "Requires an explicit recipient email address, subject, body, and user approval. "
        "Verified by matching Message-ID and exact content in Gmail Sent, not recipient delivery.",
        EmailArgs,
        "write_irreversible",
        retry_safe=False,
        mode="live",
    ),
    "notes.save": Tool(
        "notes.save",
        "local_notes",
        "Save a real note in this workspace's MongoDB database. "
        "Returns an id and verifies the saved title/content.",
        CreateNote,
        "write_reversible",
        mode="live",
    ),
}
