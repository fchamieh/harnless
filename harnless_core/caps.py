"""Output caps: the ceiling on what a tool may hand back.

Two layers: a tool's `limit`-style argument is model-facing (the agent can ask for
less), the constants in harnless_core/config.py are the ceiling it cannot exceed, and
these helpers apply both while keeping the truncation note the agent needs to know it
happened."""

def _bool_arg(args: dict, key: str, default: bool) -> bool:
    """Read a boolean-style tool argument, tolerating what models often send.

    A model frequently sends "true"/"yes"/"1" instead of a JSON true, so those
    strings are accepted; a missing or unrecognisable value falls back to
    `default`.
    """
    raw = args.get(key)
    if raw is None:
        return default
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("true", "t", "yes", "y", "on", "1"):
            return True
        if text in ("false", "f", "no", "n", "off", "0", ""):
            return False
        return default
    return bool(raw)
def _limit_arg(args: dict, key: str, default: int, ceiling: int) -> int:
    """Read an integer limit-style argument, clamped to [1, ceiling].

    Model-facing: the agent can ask for a smaller (or bigger) result, but
    never past the harness-side ceiling. A missing or bogus value falls back
    to the default.
    """
    raw = args.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, ceiling))
def _depth_arg(args: dict, key: str = "depth") -> int | None:
    """Read an optional "how deep may this search go" tool argument.

    Returns the depth the walk must obey, or None for no limit — the tool's
    default, and also the fallback for a blank or unparseable value, exactly as
    `_limit_arg` falls back to its default. Levels count the directory searched
    as 1, so a depth of 1 means "do not descend at all"; a zero or negative
    value is raised to 1, the shallowest search that can still find something.
    There is no ceiling: the knob can only ask for *less* than the default walk,
    so a huge depth costs nothing the unlimited one does not already pay.
    """
    raw = args.get(key)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None  # "a few", true: the default walk, not a shallow one
    return max(1, value)
def _clamp_int(value: int, low: int, high: int) -> int:
    """Clamp a CLI-supplied integer into [low, high].

    Guards the harness-side knobs (`--max-subagents`, `--max-subagent-steps`)
    so a nonsensical value can't produce a negative limit or an unbounded loop.
    """
    return max(low, min(value, high))
def _cap_note(stat: str, hint: str) -> str:
    """The marker appended to a capped result: how much was dropped + how to recover."""
    return f"\n... [truncated: {stat}{'; ' + hint if hint else ''}]"
def _cap_note_above(stat: str, hint: str) -> str:
    """The same marker placed *above* kept output, for a cap that kept the end.

    Below a kept tail the usual wording lies: it reads as "the end is missing",
    which is the opposite of what happened, and the model re-runs a command whose
    answer it is already holding.
    """
    return f"[truncated: {stat}{'; ' + hint if hint else ''}]\n"
def _tail_window(text: str, limit: int) -> tuple:
    """The last `limit` characters of `text`, as (kept, dropped_from_the_front).

    The cut moves forward to the next line start, so a kept tail never begins
    mid-line and the window holds slightly less than `limit`. When the cut cannot
    be snapped — the final line alone is wider than the window, or snapping would
    keep nothing — it stays a plain character cut, which the returned pair reports
    by dropping less. `text` itself is returned untouched when it already fits.
    """
    if len(text) <= limit:
        return text, 0
    cut = len(text) - limit
    nl = text.find("\n", cut)
    start = cut if nl == -1 else nl + 1
    if start >= len(text):  # a snap that would keep nothing keeps characters instead
        start = cut
    return text[start:], start
def _truncate(text: str, limit: int, *, unit: str = "chars", hint: str = "") -> str:
    """Cap `text` at `limit` characters, appending what was dropped and a recovery hint.

    The beginning is what survives — the one tool that wants the other end is
    run_shell's `tail`, which reads the kept tail out of `_tail_window` instead.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + _cap_note(
        f"{len(text)} {unit} total, showing first {limit}", hint
    )
def _cap_count(items: list, limit: int, *, unit: str, hint: str = "") -> str:
    """Join at most `limit` items, appending how many exist and a recovery hint."""
    if len(items) <= limit:
        return "\n".join(str(i) for i in items)
    return "\n".join(str(i) for i in items[:limit]) + _cap_note(
        f"{len(items)} {unit}, showing first {limit}", hint
    )
def _clip_line(line: str, limit: int) -> str:
    """Clip one over-long line (e.g. minified JS) so a single hit can't dump megabytes."""
    if len(line) <= limit:
        return line
    return line[:limit] + f" …[+{len(line) - limit} chars on this line]"
def _content_chars(content) -> int:
    """Approximate character length of a message content value.

    Handles both plain strings and OpenAI multimodal content (a list of
    parts) so /status can estimate context usage for either.
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                total += len(part.get("text", ""))
            elif ptype == "image_url":
                total += len((part.get("image_url") or {}).get("url", ""))
        return total
    return 0
