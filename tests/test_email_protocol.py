import smtplib
from email.message import EmailMessage

import pytest

from jarvis import email_tools
from jarvis.config import Settings
from jarvis.email_tools import lookup_sent_sync, message_id, send_sync
from jarvis.tools import ToolFailure
from tests.conftest import TEST_TOKEN


@pytest.fixture
def mail_settings():
    return Settings(
        _env_file=None,
        access_token=TEST_TOKEN,
        email_address="me@gmail.com",
        email_password="test-password-not-real",
    )


@pytest.mark.parametrize("changed", [None, "subject", "body", "recipient", "sender", "message_id"])
def test_sent_verification_compares_actual_message_fields(mail_settings, monkeypatch, changed):
    args = {"recipient": "priya@example.com", "subject": "Project report", "body": "Ready tomorrow"}
    message = EmailMessage()
    message["From"] = "other@gmail.com" if changed == "sender" else "me@gmail.com"
    message["To"] = "wrong@example.com" if changed == "recipient" else args["recipient"]
    message["Subject"] = "Wrong subject" if changed == "subject" else args["subject"]
    message["Message-ID"] = (
        "<other@example.com>" if changed == "message_id" else message_id("task:s1")
    )
    message.set_content("Wrong body" if changed == "body" else args["body"])

    class Mailbox:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def uid(self, command, *values):
            if command == "search":
                assert message_id("task:s1") in values[-1]
                return "OK", [b"42"]
            assert values[-1] == "(BODY.PEEK[])"
            return "OK", [(b"metadata", message.as_bytes())]

    monkeypatch.setattr(email_tools, "imap_session", lambda settings: Mailbox())
    assert lookup_sent_sync(mail_settings, "task:s1", args) is (changed is None)


def test_smtp_acceptance_is_not_treated_as_delivery(mail_settings, monkeypatch):
    captured = []

    class SMTP:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send_message(self, message):
            captured.append(message)
            return {}

    monkeypatch.setattr(email_tools, "smtp_session", lambda settings: SMTP())
    args = {"recipient": "priya@example.com", "subject": "Report", "body": "Hello"}
    assert send_sync(mail_settings, "task:s1", args) is None
    assert captured[0]["Message-ID"] == message_id("task:s1")
    assert str(captured[0]["To"]) == args["recipient"]
    assert captured[0].get_content().rstrip("\n") == args["body"]


def test_disconnect_during_submission_blocks_resend(mail_settings, monkeypatch):
    class SMTP:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send_message(self, message):
            raise smtplib.SMTPServerDisconnected("connection lost")

    monkeypatch.setattr(email_tools, "smtp_session", lambda settings: SMTP())
    with pytest.raises(ToolFailure) as failure:
        send_sync(
            mail_settings,
            "task:s1",
            {"recipient": "priya@example.com", "subject": "Report", "body": "Hello"},
        )
    assert failure.value.retry_allowed is False


def test_explicit_auth_rejection_can_be_retried(mail_settings, monkeypatch):
    def denied(settings):
        raise smtplib.SMTPAuthenticationError(535, b"denied")

    monkeypatch.setattr(email_tools, "smtp_session", denied)
    with pytest.raises(ToolFailure) as failure:
        send_sync(
            mail_settings,
            "task:s1",
            {"recipient": "priya@example.com", "subject": "Report", "body": "Hello"},
        )
    assert failure.value.kind == "AUTH"
    assert failure.value.retry_allowed is True
