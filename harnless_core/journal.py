"""Session journal: every conversation recorded as it happens, and the read side
that resumes one.

Each message the harness replays to the API is appended as one JSON line, so compaction
can name the turns it replaced and --resume / /resume can rebuild a conversation.
<id>.jsonl holds the top-level run; each sub-agent run gets <id>.<chain>.jsonl."""

import json
import os
import time
from harnless_core import caps, config, render, state

def _session_stamp() -> str:
    """Timestamp for a journal record (local time, whole seconds — `seq` is what
    orders records inside the same second)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def _session_new_id() -> str:
    """Session id: <YYYYmmdd-HHMMSS>-<rand>. Deliberately dot-free, so a journal
    file's first dot separates the session id from the dotted sub-agent chain id
    (<id>.1.2.jsonl)."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"

def _session_path(session_id: str = "", subagent_id: str = "") -> str:
    """Journal file for a conversation: <session-id>.jsonl for the top-level
    conversation, <session-id>.<chain>.jsonl for a sub-agent run ('1', '1.2', …
    — the name shows how the run was delegated)."""
    sid = session_id or state.SESSION_ID
    name = f"{sid}.{subagent_id}" if subagent_id else sid
    return os.path.join(state.SESSION_DIR, name + ".jsonl")

def _session_disable(error) -> None:
    """Journaling is best effort: a log that cannot be written is dropped (said
    out loud once) and the session carries on without it."""
    state.SESSION_LOG = False
    if not state.SESSION_WARNED:
        state.SESSION_WARNED = True
        print(render.colorize(f"{render.icon('error')} session log disabled: {error}", "error"))

def _session_append(key, record: dict) -> int:
    """Append one record to conversation `key`'s journal; returns the seq it got
    (-1 when nothing was written: logging off, or a conversation never opened).

    A conversation that was never opened — a test driving run_agent directly, a
    --no-session-log run — is silently skipped, which is how the journal stays off
    by default."""
    if not state.SESSION_LOG:
        return -1
    path = state.SESSION_FILES.get(key)
    if not path:
        return -1
    seq = state.SESSION_SEQ.get(key, 0)
    record = {**record, "seq": seq, "ts": _session_stamp()}  # envelope last: a
    # journal field can never be shadowed by a key the server put in the message
    try:
        os.makedirs(state.SESSION_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as e:
        _session_disable(e)
        return -1
    state.SESSION_SEQ[key] = seq + 1
    return seq

def _session_elide(content):
    """Keep base64 megabytes out of the log: an inline image data URL becomes a
    note naming it (the record's `chars` still reports the real size). A short
    URL — a remote image reference — is kept as it is."""
    if not isinstance(content, list):
        return content
    kept = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            url = ((part.get("image_url") or {}).get("url") or "")
            if len(url) > config.SESSION_URL_KEEP:
                kept.append({
                    "type": "text",
                    "text": f"[image elided in the session log: {url.split(',', 1)[0]}, {len(url)} chars]",
                })
            else:
                kept.append(part)
        else:
            kept.append(part)
    return kept

def _session_message(msg: dict, key) -> int:
    """Journal one message exactly as the harness replays it to the API (role,
    content, tool_calls, tool_call_id, reasoning_content … whatever the message
    actually carries). Returns its seq (-1 if nothing was written) — compaction
    needs a stable id for the turns it replaces."""
    record = dict(msg)
    if "content" in record:
        record["content"] = _session_elide(record.get("content"))
    record["chars"] = caps._content_chars(msg.get("content"))
    record["type"] = "message"
    seq = _session_append(key, record)
    if seq >= 0:
        # Positional: this list stays aligned with the conversation itself, so a
        # compact record can name the exact seqs of the turns it drops.
        state.SESSION_MSG_SEQ.setdefault(key, []).append(seq)
    return seq

def _session_messages(messages: list, key) -> None:
    """Journal a batch of messages in order."""
    for msg in messages:
        _session_message(msg, key)

def _session_open(key, path: str, header: dict) -> None:
    """Start conversation `key`'s journal at `path`, headed by what the run was."""
    if not state.SESSION_LOG:
        return
    state.SESSION_FILES[key] = path
    state.SESSION_SEQ[key] = 0
    state.SESSION_MSG_SEQ[key] = []
    try:
        os.makedirs(state.SESSION_DIR, exist_ok=True)
        # A new session owns its file: opening a journal starts it, it does not
        # continue one that happens to be there (two runs in one file would mix
        # their records under restarting seqs). Continuations use _session_continue.
        with open(path, "w", encoding="utf-8"):
            pass
    except OSError as e:
        _session_disable(e)
        return
    _session_append(key, {
        "harnless": config.VERSION,
        "session_id": state.SESSION_ID,
        "mode": state.SESSION_MODE,
        "cwd": state.CWD,
        "api_url": state.API_URL,
        "model": state.MODEL,
        **header,
        "type": "session",
    })

def _session_continue(key, path: str, start_seq: int, record: dict, seqs: list = None) -> None:
    """Point conversation `key`'s journal at an already-written file and keep its
    numbering, then mark the continuation in the log itself."""
    if not state.SESSION_LOG:
        return
    state.SESSION_FILES[key] = path
    state.SESSION_SEQ[key] = max(0, start_seq)
    # Restored messages carry the seqs their records already have: a compaction in
    # the resumed run names the turns it replaced in the same numbering.
    state.SESSION_MSG_SEQ[key] = list(seqs or [])
    _session_append(key, record)

def start_session(mode: str = "") -> str:
    """Begin a new top-level session: a fresh id, a fresh journal, its header."""
    if mode:
        state.SESSION_MODE = mode
    state.SESSION_ID = _session_new_id()
    _session_open(state.MAIN_CONV_ID, _session_path(), {"depth": 0, "subagent_id": None})
    return _session_path()

def _session_note() -> str:
    """The line that tells you where this run's transcript is going (banner,
    /status): sub-agent runs get their own files alongside it."""
    if not state.SESSION_LOG:
        return "session log: off (--no-session-log)"
    return f"session log: {_session_path()} (sub-agents: {_session_path(state.SESSION_ID, '1')}, …)"

def _session_mtime_str(path: str) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
    except OSError:
        return "?"

def _session_files() -> list:
    """Every journal file in SESSION_DIR, newest first."""
    try:
        names = os.listdir(state.SESSION_DIR)
    except OSError:
        return []
    logs = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(state.SESSION_DIR, name)
        if os.path.isfile(path):
            logs.append(path)
    logs.sort(key=_session_mtime_key, reverse=True)
    return logs

def _session_mtime_key(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0

def _session_main_files() -> list:
    """Top-level conversation journals only (a session id contains no dot, so a
    main log has exactly one), newest first."""
    return [
        p for p in _session_files()
        if os.path.basename(p)[: -len(".jsonl")].find(".") == -1
    ]

def _session_id_of(path: str) -> str:
    """Session part of a journal file name: <id>.1.2.jsonl → <id>.1.2 (the chain
    keeps its place, so continuing that log continues that chain)."""
    return os.path.basename(path)[: -len(".jsonl")]

def _session_read(path: str, kinds: tuple = ("message",)) -> tuple:
    """Read one journal: (header record, kept records in order, last seq).

    `kinds` picks the record types to keep: the conversation rebuild wants messages
    only, the sessions tool also wants the compact markers that name a gap."""
    header: dict = {}
    messages = []
    last_seq = -1
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for index, line in enumerate(f):
                if index >= config.SESSION_RECORD_LIMIT:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                seq = rec.get("seq")
                if isinstance(seq, int):
                    last_seq = max(last_seq, seq)
                kind = rec.get("type")
                if kind == "session" and not header:
                    header = rec
                elif kind in kinds:
                    messages.append(rec)
    except OSError as e:
        _session_disable(e)
        return {}, [], -1
    return header, messages, last_seq

def _session_rebuild_pairs(records: list) -> list:
    """Turn journal records back into a conversation a server will accept, keeping
    each message's journal seq beside it (None for a message this rebuild invented,
    which has no record of its own) — the numbering compaction reports against.

    A run recorded mid tool-call is the interesting case: its calls were never
    answered, and a server rejects an assistant message whose tool calls are
    unanswered — so those get answered with a note. Tool results whose call is
    missing (cut off by --resume-last, or a log written by an older build) are
    dropped, and so is an assistant message with neither content nor tool calls
    (an interrupted mid-thinking partial — the server rejects those too). System
    messages are dropped as well: the resumed run supplies its own.
    """
    pairs: list = []  # (seq, message), the conversation in replay order
    pending = []  # tool_call ids the log never answered

    def _close_pending():
        for call_id in pending:
            pairs.append((None, {
                "role": "tool",
                "tool_call_id": call_id,
                "content": config.SESSION_UNANSWERED_NOTE,
            }))

    for rec in records:
        msg = {k: v for k, v in rec.items() if k not in config._SESSION_META_KEYS}
        seq = rec.get("seq") if isinstance(rec.get("seq"), int) else None
        role = msg.get("role")
        if role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id in pending:
                pending.remove(call_id)
                pairs.append((seq, msg))
            continue  # an answer to a call this rebuild never asked about
        _close_pending()  # the log moved on without answering them
        pending = []
        if role not in ("user", "assistant"):
            continue  # system (and anything unexpected) — the run builds its own
        if role == "assistant" and not msg.get("content") and not msg.get("tool_calls"):
            continue
        pairs.append((seq, msg))
        if role == "assistant":
            pending = [
                tc.get("id")
                for tc in (msg.get("tool_calls") or [])
                if isinstance(tc, dict) and tc.get("id")
            ]
    _close_pending()  # the log ended on an unanswered call
    return pairs

def _session_rebuild(records: list) -> list:
    """The conversation a journal describes (see _session_rebuild_pairs)."""
    return [msg for _, msg in _session_rebuild_pairs(records)]

def _session_resolve(spec: str) -> tuple:
    """Resolve a resume spec to a journal file: 'latest', a session id, an
    '<id>.<chain>' sub-agent log, or a path. Returns (path, error)."""
    spec = (spec or "").strip()
    if not spec:
        return "", "usage: /resume <session-id> (with no argument, lists recent sessions)"
    if spec.lower() in ("latest", "last", "newest", "-"):
        files = _session_main_files()
        if not files:
            return "", f"no session logs in {state.SESSION_DIR}"
        return files[0], ""
    if spec.endswith(".jsonl") or os.sep in spec or "/" in spec or spec.startswith("~"):
        base = os.path.expanduser(spec)
        for candidate in (base, base + ".jsonl"):
            if os.path.isfile(candidate):
                return candidate, ""
        return "", f"no session log at: {spec}"
    path = os.path.join(state.SESSION_DIR, spec + ".jsonl")
    if os.path.isfile(path):
        return path, ""
    known = [_session_id_of(p) for p in _session_files()[:3]]
    hint = f" recent sessions: {', '.join(known)}" if known else ""
    return "", f"no session log for '{spec}' in {state.SESSION_DIR};{hint} (see /sessions)"

def _session_injected_turn(rec: dict) -> bool:
    """A user record the harness wrote for the *model* rather than a line someone
    typed: a todo reminder, or the handoff note a compacted head was replaced by.
    The session never printed either as the user's own line, so neither does a replay."""
    if rec.get("role") != "user":
        return False
    text = _record_text(rec.get("content")).lstrip()
    return text.startswith(config.TODO_REMINDER_HEADER) or text.startswith(config.COMPACT_SUMMARY_HEADER)

def _session_tail(records: list, items: int) -> list:
    """The contiguous slice of a journal a resumed run replays.

    `items` is how many *speaking turns* — a user line or an assistant message — the
    echo reaches back over; the window also carries the tool calls and answers that
    sit between them, since those are part of what the user saw. It is cut to the
    last RESUME_TAIL_RECORDS records so a tool-heavy turn cannot flood the terminal."""
    if items <= 0:
        return []
    spoken = [
        i
        for i, rec in enumerate(records)
        if rec.get("role") == "assistant"
        or (rec.get("role") == "user" and not _session_injected_turn(rec))
    ]
    if not spoken:
        return []
    start = max(
        spoken[-items] if len(spoken) >= items else 0,
        len(records) - config.RESUME_TAIL_RECORDS,
    )
    if start > spoken[-1]:
        # The window cut off every turn but a run of tool answers that came after
        # the last one: that is what the session ended looking like, so show it.
        start = spoken[-1]
    return records[start:]

def replay_session_tail(
    records: list, items: int = config.RESUME_TAIL_ITEMS, label: str = ""
) -> int:
    """Print the last stretch of a journaled session the way the original session did.

    A resumed run gets the transcript back but not the memory of what it looked like,
    so the REPL replays its tail through the very printers the agent loop used: the
    `you>` line, the thinking line, the reply through the Markdown renderer, each tool
    call followed by the answer the log recorded for it, with the same previews. Turns
    the harness injected for the model (todo reminders, a compaction note) stay
    unprinted, as they were live. Returns how many turns were shown (0 = nothing)."""
    tail = _session_tail(records, items)
    spoken = [
        rec
        for rec in tail
        if rec.get("role") == "assistant"
        or (rec.get("role") == "user" and not _session_injected_turn(rec))
    ]
    if not spoken:
        return 0
    if label:
        plural = "turns" if len(spoken) != 1 else "turn"
        print(render.colorize(f"last {len(spoken)} {plural} from {label}:", "dim"))
    shown = 0
    index = 0
    while index < len(tail):
        rec = tail[index]
        role = rec.get("role")
        if role == "user":
            if not _session_injected_turn(rec):
                render.print_user(_record_text(rec.get("content")))
                shown += 1
            index += 1
            continue
        if role == "assistant":
            shown += 1
            render.print_reasoning(rec.get("reasoning_content") or "", config.RESUME_TAIL_CHARS)
            calls = [tc for tc in (rec.get("tool_calls") or []) if isinstance(tc, dict)]
            content = _record_text(rec.get("content"))
            if content or not calls:
                render.print_assistant(content)
            index += 1
            answers = []
            while index < len(tail) and tail[index].get("role") == "tool":
                answers.append(tail[index])
                index += 1
            answered = {
                a.get("tool_call_id"): _record_text(a.get("content")) for a in answers
            }
            for call in calls:  # call, then its answer, then the next — as it ran
                fn = call.get("function") or {}
                render.print_tool_call(fn.get("name") or "?", fn.get("arguments") or "")
                if call.get("id") in answered:
                    render.print_tool_result(answered[call.get("id")])
            for answer in answers:  # answered here, but its call is outside the window
                if answer.get("tool_call_id") not in {c.get("id") for c in calls}:
                    render.print_tool_result(_record_text(answer.get("content")))
            continue
        if role == "tool":  # an answer whose call the window cut off
            render.print_tool_result(_record_text(rec.get("content")))
            index += 1
            continue
        index += 1  # the system prompt this run supplies itself, anything unexpected
    return shown

def resume_session(spec: str, keep: int = 0, mode: str = "", echo: int = 0) -> tuple:
    """Continue a recorded session: rebuild its conversation from the journal.

    Returns (messages, note), or (None, error) when nothing matches. The resumed
    run keeps appending to that same journal — one continuous transcript — so the
    restored messages are deliberately *not* re-journaled: only what the resumed
    run adds from here on, and it picks up the log's `seq` numbering where it
    stopped instead of restarting it (a log that re-recorded its own restored
    messages would double on every resume).

    `echo` is how many of the restored turns the caller wants shown to the user: the
    note and then those turns, printed out of the journal in the session's own format
    (see replay_session_tail) — so the caller does not print the note itself. 0, what
    a one-shot run asks for, prints nothing and leaves the note to the caller.
    """
    path, error = _session_resolve(spec)
    if not path:
        return None, error
    header, records, last_seq = _session_read(path)
    if keep:
        # Cut the records, not the rebuilt messages: a cut landing inside a
        # tool-call batch leaves answers whose call is gone, and _session_rebuild
        # is what drops them.
        records = records[-keep:]
    pairs = _session_rebuild_pairs(records)
    messages = [msg for _, msg in pairs]
    if not messages:
        return None, f"no messages to resume in {os.path.basename(path)}"
    if mode:
        state.SESSION_MODE = mode
    state.SESSION_ID = _session_id_of(path)
    _session_continue(
        state.MAIN_CONV_ID,
        path,
        last_seq + 1,
        {
            "harnless": config.VERSION,
            "session_id": state.SESSION_ID,
            "mode": state.SESSION_MODE,
            "resumed_file": path,
            "restored": len(messages),
            "recorded": len(records),
            "model": state.MODEL,
            "type": "resume",
        },
        seqs=[seq for seq, _ in pairs],
    )
    note = f"resumed session {state.SESSION_ID}: {len(messages)} messages from {path}"
    if keep:
        note += f" (kept the last {keep} of {last_seq + 1} records)"
    if echo:
        # The caller asked to be told where it left off, so it is told in full: the
        # note (which it then does not print itself) and the turns themselves, read
        # back out of the records — the transcript as it happened, orphan answers and
        # all — through the same printers the session used live.
        print(render.colorize(note, "dim"))
        replay_session_tail(records, echo, state.SESSION_ID)
    return messages, note

def _resume_target(resume, resume_last) -> tuple:
    """What the resume flags ask for: (spec, keep, implied).

    Normally --resume names the session and --resume-last only trims it. Given on
    its own, --resume-last is the shorthand for "continue where I left off": it
    supplies spec 'latest' (no value = keep everything, N = keep the last N).
    `implied` marks that shorthand, and main() treats a miss accordingly: a newest
    session that isn't there is a soft miss (say so, start fresh), while a session
    the user named by id is not (that fails the run).
    """
    named = (resume or "").strip()
    implied = not named and resume_last is not None
    return (
        named or ("latest" if implied else ""),
        caps._clamp_int(resume_last or 0, 0, config.SESSION_KEEP_CEILING),
        implied,
    )

def _session_prepend(msg: dict, key) -> int:
    """Journal a message being inserted at the *front* of a conversation — the system
    prompt a resumed run supplies.

    SESSION_MSG_SEQ is positional, so the seq _session_message appended at the end moves
    to the slot the message actually occupies; left where it was, a later compact record
    would name the wrong turns."""
    seq = _session_message(msg, key)
    entries = state.SESSION_MSG_SEQ.get(key)
    if seq >= 0 and entries is not None:
        entries.pop()
        entries.insert(0, seq)
    return seq

def format_sessions(limit: int = config.SESSION_LIST_LIMIT) -> str:
    """Describe the recorded sessions (newest first) for /resume and /sessions."""
    files = _session_files()
    if not files:
        return f"no session logs in {state.SESSION_DIR}"
    sessions: dict = {}
    for path in files:
        name = os.path.basename(path)[: -len(".jsonl")]
        session_id, _, chain = name.partition(".")
        info = sessions.setdefault(
            session_id, {"log": path, "header": {}, "messages": 0, "sub_logs": 0}
        )
        header, records, _ = _session_read(path)
        if chain:
            info["sub_logs"] += 1
        else:
            info["log"] = path
            info["header"] = header
            info["messages"] = len(records)
    lines = [f"session logs in {state.SESSION_DIR} (newest first):"]
    for session_id, info in list(sessions.items())[: max(1, limit)]:
        header = info["header"] or {}
        bits = [
            header.get("ts") or _session_mtime_str(info["log"]),
            f"{header.get('mode', '?')} ({header.get('model', '?')})",
            f"{info['messages']} messages",
        ]
        if info["sub_logs"]:
            bits.append(f"{info['sub_logs']} sub-agent logs")
        if header.get("cwd"):
            bits.append(header["cwd"])
        lines.append(f"  {session_id}  {'  '.join(bits)}")
    shown = min(len(sessions), max(1, limit))
    if len(sessions) > shown:
        lines.append(f"  … {len(sessions) - shown} more (increase with the SESSION_LIST_LIMIT constant)")
    lines.append("resume one with: /resume <session-id>  —  or --resume <session-id> at startup")
    return "\n".join(lines)

def _record_text(content) -> str:
    """A content value as plain text (multimodal parts flattened; an image becomes a
    label) — what the sessions tool prints for a recorded turn."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                out.append(part.get("text") or "")
            elif part.get("type") == "image_url":
                out.append("[image]")
        return "\n".join(out)
    return ""

def _session_record_line(rec: dict) -> str:
    """One journal record as a readable transcript line (for the sessions tool).

    A `compact` record prints as the gap it made — which turns, replaced by what — so
    a model reading the log can see where the conversation was cut and pick up the
    threads it needs from either side of it."""
    seq = rec.get("seq")
    if rec.get("type") == "compact":
        span = rec.get("replaced") or []
        span_text = f"seqs {span[0]}-{span[1]}" if len(span) == 2 else "turns"
        note = (
            "a handoff note"
            if rec.get("summary") == "model"
            else "a placeholder (the summarising request failed)"
        )
        return (
            f"#{seq} [HARNLESS COMPACTED {rec.get('dropped', 0)} turns ({span_text}) into "
            f"{note} at seq {rec.get('summary_seq')}; reason: {rec.get('reason', '?')}]"
        )
    msg = {k: v for k, v in rec.items() if k not in config._SESSION_META_KEYS}
    role = msg.get("role") or "?"
    head = f"#{seq} {role}"
    if role == "tool":
        head += f" (answer to {msg.get('tool_call_id', '?')})"
    lines = [head]
    text = _record_text(msg.get("content"))
    if text:
        lines.append(
            caps._truncate(
                text,
                config.SESSION_RECORD_CHARS,
                hint="the rest of this turn is in the log file itself",
            )
        )
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        lines.append(f"    called {fn.get('name', '?')}({caps._clip_line(fn.get('arguments') or '', 200)})")
    if msg.get("reasoning_content"):
        lines.append("    (thinking recorded in the log)")
    return "\n".join(lines)

def _seq_arg(args: dict, key: str, default: int) -> int:
    """An integer seq argument (a seq can legitimately be 0, so no clamp to 1)."""
    raw = args.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default

def tool_sessions(args: dict) -> str:
    """Read the session journal: list the recorded sessions, or read turns back out of
    one — including the turns compaction replaced.

    This is the payoff of journaling every message: a session that compacted itself is
    not a session that forgot things. The `compact` record names the seqs it replaced,
    so `read` can fetch the exact turns the handoff note condensed — a fact that went
    back in the summary is re-readable, not something to re-derive by re-running tools.
    """
    action = (args.get("action") or "list").strip().lower()
    if action == "list":
        limit = caps._limit_arg(args, "limit", config.SESSION_LIST_LIMIT, config.MAX_COUNT_LIMIT)
        return caps._truncate(format_sessions(limit), config.SESSION_READ_LIMIT,
                         hint="narrow it with the 'limit' argument")
    if action not in ("read", "spans", "compactions"):
        return "error: unknown action (use list, read, or spans)"
    path, error = _session_resolve(args.get("session") or "latest")
    if not path:
        return f"error: {error}"
    kinds = ("compact",) if action in ("spans", "compactions") else ("message", "compact")
    header, records, last_seq = _session_read(path, kinds)
    lo = _seq_arg(args, "from", 0)
    hi = _seq_arg(args, "to", last_seq)
    picked = [r for r in records if lo <= int(r.get("seq", 0)) <= hi]
    if not picked:
        return (
            f"no records in {os.path.basename(path)} between seq {lo} and {hi} "
            f"(this log holds seqs 0-{last_seq}; 'list' shows the other sessions)"
        )
    limit = caps._limit_arg(args, "limit", config.SESSION_LIST_LIMIT * 2, config.MAX_COUNT_LIMIT)
    shown = picked[-limit:] if len(picked) > limit else picked
    max_chars = caps._limit_arg(args, "max_chars", config.SESSION_READ_LIMIT, config.TOOL_RESULT_LIMIT)
    title = f"session {_session_id_of(path)} ({path}): records {lo}-{hi}, {len(picked)} found"
    if action in ("spans", "compactions"):
        title += f" — {len(picked)} compaction(s)"
    lines = [title]
    if len(shown) < len(picked):
        lines.append(
            f"(showing the last {limit} of {len(picked)}; earlier seqs start at "
            f"{picked[0].get('seq')} — ask again with a narrower from/to)"
        )
    lines.extend(_session_record_line(r) for r in shown)
    return caps._truncate("\n".join(lines), max_chars,
                     hint="narrow from/to, raise max_chars, or read the .jsonl file directly")

def _elision_note(dropped: int) -> str:
    """Says, inside an elided message, what is missing and where the full text is."""
    return (
        f"\n\n[... harnless elided {dropped} chars of this message when compacting the "
        "session; the full text is in the session log (the sessions tool) ...]\n\n"
    )

def _session_span(key, start: int, end: int) -> list:
    """The [first, last] journal seqs of messages[start:end] ([] when none of them has
    a record of its own — a message a rebuild invented)."""
    real = [s for s in (state.SESSION_MSG_SEQ.get(key) or [])[start:end] if isinstance(s, int)]
    return [real[0], real[-1]] if real else []

def _transcript_note(conv_key, span: list) -> str:
    """Names the transcript a handoff note stands for, inside the conversation itself:
    the model can ask for the exact turns the note condensed (sessions tool) instead of
    re-deriving them with fresh tool calls."""
    if not state.SESSION_LOG:
        return "[transcript: not journaled (--no-session-log), so these turns are gone]"
    path = state.SESSION_FILES.get(conv_key) or _session_path()
    replaced = f"seqs {span[0]}-{span[1]}" if len(span) == 2 else "the turns above"
    return (
        f"[transcript of this session: {_session_id_of(path)} — what this note replaced "
        f"({replaced}) is still recorded; the sessions tool reads it back by seq]"
    )

def _transcript_hint(conv_key) -> str:
    """Tells a stranded run where its transcript still is (out-of-context report)."""
    if not state.SESSION_LOG:
        return "the transcript was not journaled (--no-session-log), so there is nothing to read back"
    path = state.SESSION_FILES.get(conv_key) or _session_path()
    return (
        f"the transcript is in {path}: read the turns that were dropped with the sessions "
        f"tool, or continue this session with --resume {_session_id_of(path)}"
    )
