import urllib.request
"""What rides along with every request: the system prompt, AGENTS.md, the memory
block, the todo list, `file://`/`http(s)://` references in the user's message, and the
prompt history file."""

import base64
import json
import os
import re
import urllib

from urllib.parse import urlparse
from urllib.request import url2pathname

from harnless_core import caps, config, journal, paths, state


def _todo_render() -> str:
    if not state.TODO_ITEMS:
        return "(empty)"
    marks = {"pending": " ", "in_progress": "~", "done": "x"}
    return "\n".join(
        f"- [{marks.get(i['status'], ' ')}] {i['id']}. {i['text']}" for i in state.TODO_ITEMS
    )

def _todo_block() -> str:
    """The todo list as shown to the model: capped, so a long list can't bloat context.

    _todo_render() stays uncapped for the file on disk; only what enters the
    conversation is trimmed.
    """
    return caps._truncate(
        _todo_render(),
        config.TODO_BLOCK_LIMIT,
        unit="chars",
        hint="the list is long; mark finished items done and use action 'clear'",
    )

def _todo_save():
    try:
        os.makedirs(os.path.dirname(state.TODO_FILE), exist_ok=True)
        with open(state.TODO_FILE, "w", encoding="utf-8") as f:
            f.write(_todo_render() + "\n" if state.TODO_ITEMS else "")
    except OSError:
        pass

def _todo_load():
    """Load the todo list from TODO_FILE (best effort)."""
    try:
        with open(state.TODO_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    items = []
    for line in lines:
        m = re.match(r"^\s*-\s*\[( |~|x)\]\s*(\d+)\.\s+(.*)$", line)
        if not m:
            continue
        mark, num, text = m.groups()
        items.append(
            {
                "id": int(num),
                "text": text,
                "status": {" ": "pending", "~": "in_progress", "x": "done"}[mark],
            }
        )
    if items:
        state.TODO_ITEMS = items
        state.TODO_NEXT_ID = max(i["id"] for i in items) + 1
        state.TODO_LAST_INJECTED = None

def _todo_update_one(args: dict):
    """Apply a single update (id, plus status and/or text). Returns an error string or None."""
    if args.get("id") is None:
        return "error: 'update' requires id"
    try:
        tid = int(args["id"])
    except (TypeError, ValueError):
        return "error: id must be an integer"
    item = next((i for i in state.TODO_ITEMS if i["id"] == tid), None)
    if item is None:
        return f"error: no todo with id {tid}"
    if args.get("text"):
        item["text"] = args["text"].strip()
    if args.get("status"):
        status = args["status"].strip().lower()
        if status not in ("pending", "in_progress", "done"):
            return "error: status must be pending, in_progress, or done"
        item["status"] = status
    return None

def tool_todo(args: dict) -> str:
    action = (args.get("action") or "list").strip().lower()
    if action == "add":
        items = args.get("items")
        if isinstance(items, list):
            texts = [str(t).strip() for t in items]
        else:
            texts = [(args.get("text") or "").strip()]
        texts = [t for t in texts if t]
        if not texts:
            return "error: 'add' requires text or a non-empty items array"
        for t in texts:
            state.TODO_ITEMS.append({"id": state.TODO_NEXT_ID, "text": t, "status": "pending"})
            state.TODO_NEXT_ID += 1
    elif action == "update":
        updates = args.get("updates")
        if isinstance(updates, list):
            for u in updates:
                if not isinstance(u, dict):
                    return "error: each entry in 'updates' must be an object with an 'id'"
                err = _todo_update_one(u)
                if err:
                    return err
        else:
            err = _todo_update_one(args)
            if err:
                return err
    elif action == "clear":
        state.TODO_ITEMS.clear()
    elif action != "list":
        return f"error: unknown action '{action}' (use add, update, list, or clear)"
    _todo_save()
    return "todo list:\n" + _todo_block()

def _todo_reminder(messages: list, conv_key=None):
    """Append a reminder with the current todo list if it changed since the last injection.

    Sent as a *user* message, not system: some chat templates (e.g. Qwen) forbid
    system messages anywhere but the first position, and a mid-conversation system
    reminder would make the server reject the whole request. It is journaled like
    any other message the model is about to see — the log is the conversation.
    """
    if not state.TODO_ITEMS:
        state.TODO_LAST_INJECTED = None
        return
    signature = json.dumps(state.TODO_ITEMS, sort_keys=True)
    if signature == state.TODO_LAST_INJECTED:
        return
    messages.append({"role": "user", "content": "Current todo list:\n" + _todo_block()})
    journal._session_message(messages[-1], conv_key)
    state.TODO_LAST_INJECTED = signature

def _memory_read(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln[2:].strip() for ln in f.read().splitlines() if ln.startswith("- ")]
    except OSError:
        return []

def _memory_write(path: str, notes: list):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(f"- {n}" for n in notes) + ("\n" if notes else ""))
    except OSError:
        pass

def _memory_path(scope: str) -> str:
    return state.MEMORY_PROJECT_FILE if scope == "project" else state.MEMORY_GLOBAL_FILE

def tool_memory(args: dict) -> str:
    action = (args.get("action") or "list").strip().lower()
    scope = (args.get("scope") or "").strip().lower()
    if scope not in ("", "project", "global", "all"):
        return "error: scope must be 'project' or 'global'"
    if action == "add":
        text = (args.get("text") or "").strip()
        if not text:
            return "error: 'add' requires text"
        target = scope or "project"
        notes = _memory_read(_memory_path(target))
        if text in notes:
            return f"already in {target} memory"
        notes.append(text)
        _memory_write(_memory_path(target), notes)
        return f"added to {target} memory ({len(notes)} notes)"
    if action == "remove":
        text = (args.get("text") or "").strip()
        if not text:
            return "error: 'remove' requires text"
        target = scope or "project"
        notes = _memory_read(_memory_path(target))
        kept = [n for n in notes if text not in n]
        if len(kept) == len(notes):
            return f"no {target} memory note contains '{text}'"
        _memory_write(_memory_path(target), kept)
        return f"removed {len(notes) - len(kept)} note(s) from {target} memory"
    if action == "list":
        scopes = ("project", "global") if scope in ("", "all") else (scope,)
        parts = []
        for s in scopes:
            notes = _memory_read(_memory_path(s))
            body = "\n".join(f"- {n}" for n in notes) if notes else "(empty)"
            body = caps._truncate(
                body,
                config.MEMORY_BLOCK_LIMIT,
                unit="chars",
                hint=f"the {s} memory is long; remove stale notes with action 'remove'",
            )
            parts.append(f"{s} memory:\n{body}")
        return "\n\n".join(parts)
    return f"error: unknown action '{action}' (use add, list, or remove)"

def load_memory() -> str:
    """Read project and global memory notes. Returns a formatted block, '' if none.

    Capped: this block is appended to every system prompt (parent and
    sub-agents), so an unbounded memory file would silently eat context.
    """
    parts = []
    for label, path in (("project", state.MEMORY_PROJECT_FILE), ("global", state.MEMORY_GLOBAL_FILE)):
        notes = _memory_read(path)
        if notes:
            parts.append(f"{label}:\n" + "\n".join(f"- {n}" for n in notes))
    return caps._truncate(
        "\n\n".join(parts),
        config.MEMORY_BLOCK_LIMIT,
        unit="chars",
        hint="memory is long; remove stale or superseded notes",
    )

def _context_additions() -> str:
    """Memory block to append to a system prompt ('' if none)."""
    memory = load_memory()
    if not memory:
        return ""
    return "Memory (persistent notes from previous sessions):\n" + memory

def get_system_prompt(cwd, additional) -> str:
    return (
        "You are a coding assistant running inside a harness. Your working directory is {cwd}. "
        "All file paths you use must be relative to it (e.g. ./src/main.py). "
        "Use the provided tools to inspect and modify files, run commands, and search the codebase. "
        "Tool results are capped: if a result ends with a '[truncated: ...]' note, narrow the request "
        "(a smaller limit/max_chars, a tighter pattern, or a narrower path) instead of re-running it unchanged. "
        "Explore with list_dir, glob, and grep; read files (the trailer shows total line count) before editing them, "
        "and use patch_file with exact matches for edits. "
        "If patch_file reports multiple matches, re-read the area with line numbers and retry using offset/lines. "
        "Before taking any action that modifies the file system (writing, patching, moving, copying, or deleting files), "
        "plan the change when required and use the ask_user tool to get the user's explicit approval before acting; "
        "only proceed once the user has approved. Read-only exploration does not require approval. "
        "Use the ask_user tool whenever you need feedback, a decision, clarification, missing information, "
        "or yes/no approval; it blocks until the user responds. "
        "For multi-step work, track progress with the todo tool: add all the steps up front in a single 'add' call, mark each in_progress then done as you go. "
        "When you hit an error and determine the root cause, record a one-line note with the memory tool, prefixed 'gotcha: ' (e.g. 'gotcha: tests must run from the repo root'), so you don't repeat it. "
        "Use the memory tool for durable facts (user preferences, project conventions) that should survive across sessions. "
        "Delegate generously with the task tool to protect your context: the sub-agent runs in a fresh context and returns only its final summary. "
        "Use it for codebase exploration, research to create plans, 'how does this work' questions, and any work producing lots of intermediate output — "
        "do the exploration in a sub-agent, then plan from its summary. "
        "Write self-contained tasks: the sub-agent cannot see this conversation, so include the relevant paths, constraints, and the exact output you want "
        "(e.g. 'return key findings with file:line references, under 50 lines'). "
        "Format responses in Markdown (headings, lists, tables, fenced code blocks); the terminal renders it. "
        "{additional}"
    ).format(cwd=cwd, additional=additional)

def load_agents_md() -> str:
    """Read AGENTS.md (case-insensitive) from CWD, truncated. Returns "" if absent."""
    path = os.path.join(state.CWD, "AGENTS.md")
    if not os.path.exists(path):
        for name in os.listdir(state.CWD):
            if name.upper() == "AGENTS.MD":
                path = os.path.join(state.CWD, name)
                break
        else:
            return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return caps._truncate(
        content,
        config.AGENTS_MD_LIMIT,
        unit="chars",
        hint="AGENTS.md is long; trim the project instructions",
    )

def _read_local_file(path: str, label: str) -> dict:
    """Read a local file into a content part (text or image_url)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in config.IMAGE_MIME:
        if not state.VISION_ENABLED:
            return {"type": "text", "text": f"[image: {label} (not sent; vision disabled)]"}
        size = os.path.getsize(path)
        if size > config.MAX_IMAGE_BYTES:
            return {"type": "text", "text": f"[error: image too large ({size} bytes): {label}]"}
        with open(path, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{config.IMAGE_MIME[ext]};base64,{b64}"},
        }
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    content = caps._truncate(
        content,
        config.READ_FILE_LIMIT,
        unit="chars",
        hint="the referenced file is large; reference a narrower file",
    )
    return {"type": "text", "text": f"[file: {label}]\n{content}"}

def _read_cwd_relative(rel: str) -> dict:
    """Resolve a cwd:// reference (CWD-relative, sandboxed) to a content part."""
    try:
        path = paths.safe_resolve(rel)
    except ValueError as e:
        return {"type": "text", "text": f"[error: {e}]"}
    if not os.path.isfile(path):
        return {"type": "text", "text": f"[error: file not found: {rel}]"}
    return _read_local_file(path, rel)

def _read_local_uri(uri: str) -> dict:
    """Resolve a file:// reference (absolute local path) to a content part."""
    path = url2pathname(urlparse(uri).path)
    if not os.path.isfile(path):
        return {"type": "text", "text": f"[error: file not found: {uri}]"}
    return _read_local_file(path, uri)

def _fetch_remote(uri: str) -> dict:
    """Fetch an http(s):// reference and return a content part (text or image)."""
    url = uri.replace(" ", "%20")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "harnless/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(config.MAX_REMOTE_BYTES + 1)
            ctype = resp.headers.get_content_type() or ""
    except Exception as e:
        return {"type": "text", "text": f"[error: could not fetch {uri}: {e}]"}
    truncated = len(data) > config.MAX_REMOTE_BYTES
    if truncated:
        data = data[:config.MAX_REMOTE_BYTES]
    ext = os.path.splitext(urlparse(uri).path)[1].lower()
    mime = ctype if ctype.startswith("image/") else config.IMAGE_MIME.get(ext)
    if mime:
        if not state.VISION_ENABLED:
            return {"type": "text", "text": f"[image: {uri} (not sent; vision disabled)]"}
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }
    text = data.decode("utf-8", errors="replace")
    clipped = len(text) > config.FETCH_TEXT_LIMIT
    text = caps._truncate(
        text,
        config.FETCH_TEXT_LIMIT,
        hint="the referenced URL is large; reference a narrower page",
    )
    if truncated and not clipped:
        text += caps._cap_note(
            f"body read stopped at {config.MAX_REMOTE_BYTES} bytes",
            "the response is larger than the harness read ceiling",
        )
    return {"type": "text", "text": f"[file: {uri}]\n{text}"}

def _resolve_uri(uri: str):
    """Resolve a reference URI to a content part, or None for an unknown scheme."""
    if uri.startswith("cwd://"):
        return _read_cwd_relative(uri[len("cwd://"):])
    if uri.startswith("file://"):
        return _read_local_uri(uri)
    if re.match(r"^https?://", uri):
        return _fetch_remote(uri)
    return None

def expand_file_refs(text: str):
    """Expand @[uri] file references in text into OpenAI message content.

    Returns the original string when no reference resolves, otherwise a list
    of content parts (text and image_url) in order. A @[...] token whose
    scheme is not cwd/file/http(s) is left as literal text.
    """
    refs_found = False
    parts = []
    last = 0
    for m in config._REF_RE.finditer(text):
        if m.start() > last:
            parts.append(("text", text[last:m.start()]))
        part = _resolve_uri(m.group(1))
        if part is None:
            parts.append(("text", m.group(0)))
        else:
            refs_found = True
            parts.append(("part", part))
        last = m.end()
    if last < len(text):
        parts.append(("text", text[last:]))
    if not refs_found:
        return text
    content = []
    for kind, value in parts:
        if kind == "text":
            if value:
                content.append({"type": "text", "text": value})
        else:
            content.append(value)
    return content

def build_user_message(text: str) -> dict:
    """Build a user message, expanding any @[uri] file references in text."""
    return {"role": "user", "content": expand_file_refs(text)}

def _history_load():
    """Load persisted history (last HISTORY_MAX entries) into HISTORY."""
    try:
        with open(state.HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\n") for ln in f]
    except OSError:
        return
    state.HISTORY.extend(ln for ln in lines if ln.strip())
    if len(state.HISTORY) > state.HISTORY_MAX:
        del state.HISTORY[: len(state.HISTORY) - state.HISTORY_MAX]

def _history_save():
    """Write the current HISTORY to HISTORY_FILE (best effort)."""
    try:
        with open(state.HISTORY_FILE, "w", encoding="utf-8") as f:
            for entry in state.HISTORY:
                f.write(entry + "\n")
    except OSError:
        pass

def _history_add(entry: str):
    entry = entry.strip()
    if not entry:
        return
    if state.HISTORY and state.HISTORY[-1] == entry:
        return
    state.HISTORY.append(entry)
    if len(state.HISTORY) > state.HISTORY_MAX:
        state.HISTORY.pop(0)
    _history_save()
