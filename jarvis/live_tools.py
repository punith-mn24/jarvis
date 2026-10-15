"""Explicit allowlist of real actions; email uses Brave, WhatsApp uses Firefox."""

from jarvis.browser_tools import BROWSER_TOOLS
from jarvis.email_tools import LIVE_TOOLS as LOCAL_TOOLS
from jarvis.gmail_browser import GMAIL_BROWSER_TOOL

LIVE_TOOLS = {
    **BROWSER_TOOLS,
    "email.send": GMAIL_BROWSER_TOOL,
    "notes.save": LOCAL_TOOLS["notes.save"],
}
