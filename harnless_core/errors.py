"""The harness's control-flow signals, and how a server's 'context is full'
rejection is recognised.

These are signals rather than error strings: execute_tool re-raises them so the agent
loop can act on them instead of feeding them back to the model as a tool failure."""

from harnless_core import config, content


class ExitSignal(Exception):
    def __init__(self, code: int, message: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
class ContextOverflow(Exception):
    """The server refused a request because the context is full.

    Raised by `chat`/`stream_chat` in place of the bare HTTP error so the agent loop
    can tell "compact and retry" from "the API is unhappy about something else"."""
def _http_error_body(exc) -> str:
    """The response body of an HTTP error, read once and cached on the exception.

    urllib's HTTPError is its own response object: reading it consumes the body, so a
    caller that reads it to classify the failure (context overflow) would leave nothing
    for the next caller (the model-not-found hint). Caching keeps both answers.

    The body is read through content.py like every other response: a proxy that gzips
    an error leaves the server's own wording unreadable, and both patterns below match
    against that wording — the failure would be silent, with compaction never firing."""
    cached = getattr(exc, "_harnless_body", None)
    if cached is not None:
        return cached
    try:
        body = content.decode(exc).decode("utf-8", "replace")  # an HTTPError is its own response
    except (OSError, AttributeError, ValueError):  # no body at all, or a coding nothing here reads
        body = ""
    try:
        exc._harnless_body = body
    except AttributeError:
        pass
    return body

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
    text = " ".join((_http_error_body(exc) or str(exc)).split())
    if not text or not config._CONTEXT_OVERFLOW_RE.search(text):
        return ""
    return f"HTTP {status}: {text[:300]}{'…' if len(text) > 300 else ''}"

def _model_not_found_reason(exc) -> str:
    """Why a request was rejected because the server could not find the model it named
    ('' when it does not look like that). Router-mode llama-server answers a request for
    a model it has no preset for with HTTP 400 "model '<name>' not found"."""
    status = getattr(exc, "code", None) or getattr(exc, "status", None) or 0
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 0
    if status not in config._MODEL_NOT_FOUND_STATUS:
        return ""
    text = " ".join((_http_error_body(exc) or str(exc)).split())
    if not text or not config._MODEL_NOT_FOUND_RE.search(text):
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
