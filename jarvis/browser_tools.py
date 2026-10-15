"""Public registry of browser actions; implementations live in provider-specific modules."""

from jarvis.amazon_tools import AMAZON_TOOLS
from jarvis.whatsapp_tools import WHATSAPP_TOOL

BROWSER_TOOLS = {**AMAZON_TOOLS, "whatsapp.send": WHATSAPP_TOOL}
