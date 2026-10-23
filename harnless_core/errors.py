"""The harness's control-flow signals, and how a server's 'context is full'
rejection is recognised.

These are signals rather than error strings: execute_tool re-raises them so the agent
loop can act on them instead of feeding them back to the model as a tool failure."""

from harnless_core import config


class ExitSignal(Exception):
    def __init__(self, code: int, message: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
class ContextOverflow(Exception):
    """The server refused a request because the context is full.

    Raised by `chat`/`stream_chat` in place of the bare HTTP error so the agent loop
    can tell "compact and retry" from "the API is unhappy about something else"."""
def _context_overflow_reason(exc) -> str:
    """Why a request was rejected, when it looks like the context being full
    ('' when it does not). Status plus a snippet: the wording is the server's own,
    so this is the only place that has to know the shapes the message comes in."""
    status = getattr(exc, "code", None) or getattr(exc, "status", None) or 0
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0
    if status not in config._CONTEXT_OVERFLOW_STATUS:
        return ""
    try:
        body = exc.read().decode("utf-8", "replace")  # an HTTPError is its own response
    except (OSError, AttributeError, ValueError):
        body = ""
    text = " ".join((body or str(exc)).split())
    if not text or not config._CONTEXT_OVERFLOW_RE.search(text):
        return ""
    return f"HTTP {status}: {text[:300]}{'…' if len(text) > 300 else ''}"
