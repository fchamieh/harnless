"""measure: part of harnless (see harnless.py)."""

import json

from harnless_core import caps


def estimate_context_chars(messages: list, tools: list = None) -> int:
    """Approximate character count of the next request payload.

    Counts the tool schemas the request will advertise (`tools`, see _request_body — the
    caller decides them) plus all message content (text, reasoning, tool-call arguments).
    Dividing by 4 gives a rough token estimate — the same heuristic /status uses.
    """
    total = len(json.dumps(tools or []))
    for m in messages:
        total += caps._content_chars(m.get("content"))
        total += len(m.get("reasoning_content") or "")
        for tc in m.get("tool_calls") or []:
            total += len((tc.get("function") or {}).get("arguments") or "")
    return total

def estimate_context_tokens(messages: list, tools: list = None) -> int:
    """Rough token count of the next request (chars/4)."""
    return estimate_context_chars(messages, tools) // 4
