import urllib.error
"""Context compaction: measuring what a request costs, cutting the oldest turns,
and writing the handoff note the model is left with.

The tail (the turns kept verbatim) is chosen in the server's own token units when the
server reports usage, so a cut means what the server says it means. Everything a run that
journals replaces stays in its journal (see harnless_core/journal.py) and the `compact`
record names the seqs it replaced; a run with the journal off says so in the note instead
of promising a transcript it is not writing."""

import urllib

from harnless_core import caps, config, errors, journal, measure, render, request, state


def _prune_text(text: str, keep: int) -> tuple:
    """Shrink a message body to its first and last `keep` characters.

    Returns (text, chars_dropped), where 0 dropped means the body was already small
    enough to keep whole — pruning what does not need it only adds a confusing note.
    """
    if keep <= 0 or len(text) <= keep:
        return text, 0
    head = keep // 2
    tail = keep - head
    return (
        text[:head] + journal._elision_note(len(text) - keep) + (text[-tail:] if tail else ""),
        len(text) - keep,
    )

def _compact_prune(msg: dict) -> tuple:
    """A copy of a turn about to be replaced, shrunk for the summarising request.

    Two switchable savings: the middle of an oversized tool result (the megabyte
    carriers of a session) and the thinking a note replaces instead of replaying. The
    originals stay in the log — and in the conversation — until this compaction drops
    them. Returns (message, chars_saved).
    """
    out = msg
    saved = 0
    if state.COMPACT_PRUNE_TOOL_RESULTS and msg.get("role") == "tool":
        text = msg.get("content")
        if isinstance(text, str):
            pruned, dropped = _prune_text(text, config.COMPACT_TOOL_RESULT_KEEP)
            if dropped:
                out = dict(msg)
                out["content"] = pruned
                saved += dropped
    if state.COMPACT_DROP_REASONING and out.get("reasoning_content"):
        if out is msg:
            out = dict(out)
        saved += len(out.get("reasoning_content") or "")
        out.pop("reasoning_content", None)
    return out, saved

def _compact_tail_prune(messages: list) -> int:
    """Stop replaying the thinking of turns that are no longer the current one.

    Only the last assistant reply's reasoning can still shape what the agent does next;
    the rest produced its tool calls and its answer already, and the log has it. Returns
    the chars taken out of every request from here on.
    """
    if not state.COMPACT_DROP_REASONING or not messages:
        return 0
    last_assistant = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            last_assistant = i
    saved = 0
    for i, msg in enumerate(messages):
        if i == last_assistant or not msg.get("reasoning_content"):
            continue
        saved += len(msg.get("reasoning_content") or "")
        msg.pop("reasoning_content", None)
    return saved

def _system_prefix(messages: list) -> int:
    """How many leading messages are system prompts — never compacted away."""
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        i += 1
    return i

def _msg_tokens(msg: dict) -> int:
    """What one message costs the next request: content + reasoning + tool arguments."""
    total = caps._content_chars(msg.get("content"))
    total += len(msg.get("reasoning_content") or "")
    for tc in msg.get("tool_calls") or []:
        total += len((tc.get("function") or {}).get("arguments") or "")
    return max(1, total // 4)

def _turn_tokens(messages: list) -> int:
    """What a conversation's *turns* cost (chars/4) — the part compaction can act on.

    Tool schemas and the system prompt are deliberately left out: they ride along with
    every request no matter how short the conversation is, so measuring them here would
    make the proactive trigger compact a conversation that is not what filled the
    context — and then have nothing left to drop. What a request really costs (fixed
    overhead included) is /status's estimate_context_tokens; a prompt too big for the
    overhead's sake is the reactive trigger's business, and it says plainly that
    compaction cannot help."""
    return sum(_msg_tokens(msg) for msg in messages)

def _context_tokens_used(messages: list, conv_key) -> int:
    """How full this conversation is: what the server last reported for it, or the
    estimate of its turns (see _turn_tokens)."""
    usage = state.USAGE_BY_CONV.get(conv_key)
    if isinstance(usage, dict) and usage.get("prompt_tokens"):
        try:
            return int(usage["prompt_tokens"])
        except (TypeError, ValueError):
            pass
    return _turn_tokens(messages)

def _token_scale(used: int, turn_tokens: int) -> float:
    """How to read our chars/4 costs when the server has its own number for this
    conversation: (reported / estimated).

    A server counts the chat template, the tool schemas and its own tokenizer, so it
    routinely reports more than the turns look like they cost. Compaction's tail budget
    is in the server's units, so the budgets and the per-turn costs have to be compared
    in one unit — this is the conversion. 1.0 when nothing was reported (then both
    numbers are the same estimate)."""
    if used <= 0 or turn_tokens <= 0:
        return 1.0
    return used / turn_tokens

def _compact_cut(messages: list, keep_tokens: int, scale: float = 1.0) -> int:
    """Index where the verbatim tail begins (everything after the system prefix that
    comes before it is replaced by the handoff note).

    Sized by the tail's token budget, floored at COMPACT_KEEP_MIN_MESSAGES so the model
    always gets some real recent turns rather than only a summary, and nudged off a tool
    answer: cutting there would keep an answer whose call was dropped — and a server
    rejects that.
    """
    start = _system_prefix(messages)
    cut = start
    used = 0.0
    for i in range(len(messages) - 1, start - 1, -1):
        used += _msg_tokens(messages[i]) * scale
        if used > keep_tokens:
            cut = i + 1
            break
    floor = len(messages) - config.COMPACT_KEEP_MIN_MESSAGES
    if floor < cut:
        cut = floor  # never a tail smaller than the floor, however tight the budget
    if cut < start:
        cut = start
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut += 1
    return cut

def _fit_head(head: list, budget: int, scale: float = 1.0) -> list:
    """The newest slice of the head that fits `budget` tokens.

    Trimmed from the old end — a handoff lives on the recent turns — and never starting
    on a tool answer, whose call would then be missing from the request."""
    kept = []
    used = 0.0
    for msg in reversed(head):
        cost = _msg_tokens(msg) * scale
        if kept and used + cost > budget:
            break
        kept.append(msg)
        used += cost
    kept.reverse()
    while len(kept) > 1 and kept[0].get("role") == "tool":
        kept.pop(0)
    return kept

def _compact_summary(head: list, budget: int, model: str, scale: float = 1.0) -> tuple:
    """Ask the model for the note that replaces the turns about to be dropped.

    The head is what filled the context, so the request is trimmed to `budget` tokens —
    in the same units the budget was set in (`scale`, see _token_scale) — and trimmed
    again if even that was refused. No tools are sent (the note is written, not acted
    on) and the call is keyed to a throwaway conversation id, so this request never
    masquerades as the agent's own usage and nothing of its scaffolding is journaled
    into the transcript. Returns (note, error); an empty note means fall back.
    """
    conv_id = state._NEXT_CONV_ID
    state._NEXT_CONV_ID += 1
    room = max(512, budget)
    error = ""
    for _ in range(max(1, state.COMPACT_SUMMARY_TRIMS)):
        try:
            data = request.chat(
                [{"role": "system", "content": config.COMPACT_SUMMARY_PROMPT}]
                + _fit_head(head, room, scale),
                model or state.MODEL,
                temperature=state.TEMPERATURE,
                conv_id=conv_id,  # tools stay None: this request only writes the note
            )
        except errors.ContextOverflow as e:
            # The head we are trying to summarise is what overflowed: read less of it.
            error = f"the summarising request was itself too large ({e})"
            room = max(512, room // 2)
            continue
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            return "", f"the summarising request failed ({e})"
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            return "", f"the summarising request returned nothing usable ({e})"
        finally:
            state.USAGE_BY_CONV.pop(conv_id, None)  # not the agent's own usage
        choices = data.get("choices") or [{}]
        note = (choices[0].get("message") or {}).get("content") or ""
        if isinstance(note, str) and note.strip():
            return note.strip(), ""
        return "", "the model replied with no note"
    return "", error or "the summarising request kept being rejected"

def compact_messages(messages: list, conv_key, model: str = "", reason: str = "manual",
                     tools: list = None) -> tuple:
    """Replace a conversation's oldest turns with one handoff note the model wrote.

    The head — everything above the verbatim tail — is pruned first (an oversized tool
    result keeps only its head and tail, old thinking is dropped): that is what makes
    room for the summarising request, and the tail stops paying for it on every later
    request too. Then the model rewrites the head (COMPACT_SUMMARY_PROMPT) and the
    conversation gives it up for that note, marked with COMPACT_SUMMARY_HEADER so the
    model knows the harness did the cutting rather than the user.

    Nothing is lost for good — in a run that journals: every turn was journaled as it
    entered the conversation, and the `compact` record names the seqs it replaced, so the
    sessions tool (or a resume) can read them back. A run that keeps its own journal (a
    sub-agent) keeps its own compact record in it: the record always goes to `conv_key`'s
    log. With journaling off the cut is what it is — the note says the turns are gone
    instead of pointing at a log this run is not writing.

    Returns (compacted, note) — `note` is what the user is told: what was replaced, or
    why nothing was.
    """
    total = len(messages)
    if total < state.COMPACT_MIN_MESSAGES:
        return False, (
            f"only {total} messages in play — compaction waits for {state.COMPACT_MIN_MESSAGES} "
            "before there is worth summarising"
        )
    start = _system_prefix(messages)
    used = _context_tokens_used(messages, conv_key)
    turn_tokens = _turn_tokens(messages)
    # What the whole request looked like (tool schemas and system prompt included):
    # the part compaction cannot touch, recorded next to the part it cut.
    request_tokens = measure.estimate_context_tokens(messages, tools)
    scale = _token_scale(used, turn_tokens)
    # With no window known (--context-window unset, nothing probed), compare against
    # what this conversation is actually using instead of a window we don't have.
    window = state.CONTEXT_WINDOW if state.CONTEXT_WINDOW > 0 else max(used, 1)
    keep_tokens = max(1, window * state.COMPACT_KEEP_TOKENS_PCT // 100)
    # The budget is the server's number, the turns are ours: scale reads them in the
    # same units, so a reported usage of 18k against a 4k-looking conversation cuts
    # where 18k says it should instead of finding nothing to drop.
    cut = _compact_cut(messages, keep_tokens, scale)
    if cut <= start:
        return False, "nothing is older than the verbatim tail — there is nothing to replace"

    dropped = messages[start:cut]
    head, pruned = [], 0
    for msg in dropped:
        shrunk, saved = _compact_prune(msg)
        head.append(shrunk)
        pruned += saved
    tail_pruned = _compact_tail_prune(messages[cut:])  # the turns that stay, too
    # Room the summarising request gets: it carries the head it is condensing and the
    # note it is writing, but not the tail — so what is left after reserving the note
    # (roughly COMPACT_SUMMARY_LIMIT tokens) is what it may read. _fit_head trims the
    # oldest turns out of it, and _compact_summary trims again if it is still refused.
    budget = max(512, window - config.COMPACT_SUMMARY_LIMIT // 4)
    note, note_error = _compact_summary(head, budget, model, scale)

    span = journal._session_span(conv_key, start, cut)
    # What this note may promise about the turns it stands for: only a run that journals
    # has a transcript to point back at, and the note must not dangle one it cannot read.
    if journal.journaling():
        note_hint, no_note = config.COMPACT_NOTE_HINT, config.COMPACT_FALLBACK_NOTE
    else:
        note_hint, no_note = (
            config.COMPACT_NOTE_HINT_UNJOURNALED,
            config.COMPACT_FALLBACK_NOTE_UNJOURNALED,
        )
    summary_msg = {
        "role": "user",
        "content": config.COMPACT_SUMMARY_HEADER
        + "\n\n"
        + (
            caps._truncate(note, config.COMPACT_SUMMARY_LIMIT, hint=note_hint)
            if note
            else no_note
        )
        + "\n\n"
        + journal._transcript_note(conv_key, span),
    }

    seq_now = state.SESSION_SEQ.get(conv_key)
    summary_seq = seq_now + 1 if isinstance(seq_now, int) else None  # this record takes seq_now
    journal._session_append(conv_key, {
        "type": "compact",
        "reason": reason,
        "replaced": span,
        "dropped": len(dropped),
        "kept": total - len(dropped),
        "summary_seq": summary_seq,
        "summary": "model" if note else "fallback",
        "summary_error": note_error or None,
        "pruned_chars": pruned + tail_pruned,
        "window": window,
        "used_tokens": used,
        "turn_tokens": turn_tokens,  # what the turns alone looked like (chars/4); the
        # gap between it and used_tokens is what the server counts on top — the scale
        # the cut was made with, recorded so a reader can re-derive the cut.
        "token_scale": round(scale, 3),
        "request_tokens": request_tokens,  # the whole request, fixed overhead included
        "keep_tokens": keep_tokens,
    })

    messages[start:cut] = [summary_msg]
    written = journal._session_message(summary_msg, conv_key)
    entries = state.SESSION_MSG_SEQ.get(conv_key)
    if entries is not None:
        # Keep the seq list aligned with the conversation it describes: the summary
        # takes the slot the replaced turns occupied.
        if written >= 0:
            entries.pop()  # the seq _session_message just appended at the end
            entries[start:cut] = [written]
        else:
            entries[start:cut] = [None]

    stats = state.COMPACT_STATS.setdefault(conv_key, {"rounds": 0, "dropped": 0})
    stats["rounds"] += 1
    stats["dropped"] += len(dropped)
    # The server's last token count described the conversation *before* the cut, so it
    # is no evidence about the one we now have — leaving it in place would make the
    # proactive trigger compact again on every following turn. Until the next reply
    # reports usage, /status and the trigger fall back to the estimate.
    state.USAGE_BY_CONV.pop(conv_key, None)

    report = (
        f"compacted {len(dropped)} turns into a handoff note: {total} → {len(messages)} "
        f"messages, {total - cut} kept verbatim"
    )
    if pruned or tail_pruned:
        report += f", {pruned + tail_pruned} chars pruned first"
    if span:
        report += f" (transcript seqs {span[0]}–{span[1]} still readable)"
    if not note:
        report += f" — {note_error}, so the note is a placeholder; the turns still went"
    return True, report

def _compact_if_needed(messages: list, conv_key, model: str = "", tools: list = None) -> bool:
    """Proactive trigger: compact before the next request once this conversation's
    turns cross COMPACT_THRESHOLD_PCT of the window, rather than waiting for the server
    to refuse the request (what the server reported for it wins when it has said so —
    see _context_tokens_used for what is measured and why the fixed overhead is left
    out).

    Says what it did (or, once per conversation, why it is stuck) so a session that
    compacts itself is visible instead of mysterious. Returns whether it compacted.
    """
    if not state.AUTO_COMPACT or state.CONTEXT_WINDOW <= 0:
        return False  # no window to compare against: the reactive trigger covers that
    used = _context_tokens_used(messages, conv_key)
    threshold = max(1, state.CONTEXT_WINDOW * state.COMPACT_THRESHOLD_PCT // 100)
    if used < threshold:
        return False
    ok, note = compact_messages(
        messages, conv_key, model=model, reason="proactive", tools=tools
    )
    if ok:
        print(state.OUTPUT_INDENT + render.colorize(f"{render.icon('wait')} {note}", "dim"))
        return True
    stats = state.COMPACT_STATS.setdefault(conv_key, {"rounds": 0, "dropped": 0, "warned": False})
    if not stats.get("warned"):
        # Said once: the threshold will keep being crossed from here, and repeating the
        # same refusal every turn is noise.
        stats["warned"] = True
        print(
            state.OUTPUT_INDENT
            + render.colorize(
                f"{render.icon('wait')} context is {used}/{state.CONTEXT_WINDOW} tokens but not compacting: {note}",
                "dim",
            )
        )
    return False

def _recover_overflow(messages: list, conv_key, model: str, reason: str,
                      attempts: int, tools: list = None) -> bool:
    """Reactive trigger: the server rejected the prompt for being too long. Compact and
    let the caller retry the turn — up to COMPACT_MAX_ATTEMPTS times, which is why the
    caller counts them.

    The rejection is said out loud first (`reason` is the server's own wording): a
    session that silently re-sent a shrunken prompt is hard to trust afterwards.
    Returns False when the run is genuinely out of context: the attempts are spent, or
    there is nothing left that compaction could drop.
    """
    if attempts >= state.COMPACT_MAX_ATTEMPTS:
        return False
    if not state.AUTO_COMPACT:
        print(
            state.OUTPUT_INDENT
            + render.colorize(f"{render.icon('wait')} context is full ({reason}) and auto-compaction is off — /compact to make room", "dim")
        )
        return False
    print(state.OUTPUT_INDENT + render.colorize(f"{render.icon('wait')} the server rejected the prompt: {reason}", "error"))
    ok, note = compact_messages(
        messages, conv_key, model=model, reason="overflow", tools=tools
    )
    if not ok:
        print(
            state.OUTPUT_INDENT
            + render.colorize(f"{render.icon('error')} context is full and compaction cannot help: {note}", "error")
        )
        return False
    print(state.OUTPUT_INDENT + render.colorize(f"{render.icon('wait')} {note} — retrying the request", "tool"))
    return True

def set_auto_compact(arg: str) -> str:
    """Apply an /auto-compact argument; return the status line to print ('' = unknown)."""
    v = arg.strip().lower()
    if v in ("on", "true", "yes", "1"):
        state.AUTO_COMPACT = True
        return f"auto-compact on — compaction starts at {state.COMPACT_THRESHOLD_PCT}% of the window"
    if v in ("off", "false", "no", "0"):
        state.AUTO_COMPACT = False
        return "auto-compact off — only /compact (and a rejected request) will compact"
    if v.startswith("threshold"):
        rest = v[len("threshold"):].strip()
        try:
            state.COMPACT_THRESHOLD_PCT = caps._clamp_int(int(rest), 1, config.COMPACT_THRESHOLD_CEILING)
        except ValueError:
            return ""
        return f"auto-compact threshold: {state.COMPACT_THRESHOLD_PCT}% of the context window"
    if not v:
        return (
            f"auto-compact {'on' if state.AUTO_COMPACT else 'off'} at "
            f"{state.COMPACT_THRESHOLD_PCT}% of the window (/auto-compact on|off|threshold N)"
        )
    return ""

def format_compaction() -> str:
    """The /status line on compaction: what it would do, and what it already did."""
    window = f"{state.CONTEXT_WINDOW} tokens" if state.CONTEXT_WINDOW > 0 else "window unknown"
    rounds = sum(int(s.get("rounds", 0)) for s in state.COMPACT_STATS.values())
    dropped = sum(int(s.get("dropped", 0)) for s in state.COMPACT_STATS.values())
    done = f"{rounds} round(s), {dropped} turns replaced" if rounds else "no compactions yet"
    return (
        f"compaction: auto-compact {'on' if state.AUTO_COMPACT else 'off'} at "
        f"{state.COMPACT_THRESHOLD_PCT}% of {window}, tail keeps {state.COMPACT_KEEP_TOKENS_PCT}% of it "
        f"({config.COMPACT_KEEP_MIN_MESSAGES} messages minimum); {done}; /compact to do it now"
    )
