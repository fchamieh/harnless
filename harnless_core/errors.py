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

class StreamInterrupted(Exception):
    """The user interrupted a streaming response (double ESC)."""

    def __init__(self, message: dict):
        super().__init__("stream interrupted")
        self.message = message  # the partial assistant message

class SubagentInterrupted(StreamInterrupted):
    """A `task` tool call was interrupted, so the stop must bubble up.

    Raised by `tool_task` when the user interrupts a sub-agent: every agent in
    the delegation chain stops too, instead of the parent carrying on with the
    half-finished sub-agent as if it had succeeded. `message["content"]` carries
    the note (and any partial output) the delegating agent gets as the tool
    result.
    """

class SubagentStepLimit(Exception):
    """A sub-agent used up its allowed model turns (`SUBAGENT_STEP_LIMIT`).

    A signal rather than a return code: an exit code would be indistinguishable
    from a code the sub-agent's own `exit` call chose. Only `tool_task` ever
    sees it — sub-agents are the only capped runs.
    """
