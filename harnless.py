#!/usr/bin/env python3
"""harnless: a minimal agent harness for a local llama-server (OpenAI-compatible)."""

import argparse
import atexit
import base64
import html.parser
import http.client
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
import urllib.error
from urllib.parse import urlparse
from urllib.request import url2pathname

from harnless_core import caps, compact, config, context, errors, journal, lineedit, mcp, measure, paths, render, request, search, shell, state, toolspec
_MODULES = (caps, compact, config, context, errors, journal, lineedit, mcp, measure, paths, render, request, search, shell, state, toolspec)  # facade: moved modules


# ---------------------------------------------------------------- output caps


# ------------------------------------------------------------ session journal


# ------------------------------------------------------------ compaction


# ---------------------------------------------------------------- tools


def tool_get_cwd(args: dict) -> str:
    return state.CWD


def tool_exit(args: dict):
    code = int(args.get("code", 0))
    message = args.get("message", "")
    raise errors.ExitSignal(code, message)


def _prompt_line(prompt: str):
    """Read a line from the user, returning None on EOF/cancel."""
    try:
        line = lineedit.readline_prompt(prompt, commands_menu)
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return line.strip()


def _print_question(text: str):
    print(state.OUTPUT_INDENT + render.colorize(f"{render.icon('thinking')} {text}", "assistant"), flush=True)


def tool_ask_user(args: dict) -> str:
    question = (args.get("question") or "").strip()
    if not question:
        return "error: empty question"
    raw_options = args.get("options")
    if raw_options is not None and not isinstance(raw_options, list):
        return "error: options must be a list of strings"
    options = [str(o) for o in (raw_options or [])]
    print()
    _print_question(question)
    for i, opt in enumerate(options, 1):
        print(state.OUTPUT_INDENT + render.colorize(f"  {i}) {opt}", "dim"))
    if options:
        print(state.OUTPUT_INDENT + render.colorize("  (or type your own answer)", "dim"))
    prompt = state.OUTPUT_INDENT + render.colorize(f"{render.icon('user')} answer> ", "user")
    answer = _prompt_line(prompt)
    if answer is None:
        return "user cancelled (no answer)"
    if not answer:
        return "user answered: (empty)"
    if options:
        try:
            idx = int(answer)
        except ValueError:
            idx = 0
        if 1 <= idx <= len(options):
            return f"user selected: {options[idx - 1]}"
    return f"user answered: {answer}"


def tool_mkdir(args: dict) -> str:
    path = paths.safe_resolve(args["path"])
    os.makedirs(path, exist_ok=True)
    return f"created directory: {path}"


def tool_read_file(args: dict) -> str:
    path = paths.safe_resolve(args["path"])
    lines = paths._read_lines(path)
    offset = int(args.get("offset", 1))
    count = int(args.get("lines", 0))
    if offset < 1:
        return "error: offset must be >= 1"
    if count < 0:
        return "error: lines must be >= 0"
    start = offset - 1
    end = len(lines) if count == 0 else start + count
    selected = lines[start:end]
    if args.get("line_numbers", True):
        selected = [f"{i + start + 1}: {line}" for i, line in enumerate(selected)]
    content = "\n".join(selected)
    limit = caps._limit_arg(args, "max_chars", config.READ_FILE_LIMIT, config.TOOL_RESULT_LIMIT)
    content = caps._truncate(
        content, limit, hint="read the rest with offset/lines, or raise max_chars"
    )
    if content and (offset != 1 or count != 0):
        content += f"\n[lines {offset}-{min(end, len(lines))} of {len(lines)}]"
    return content if content else "(empty)"


def tool_write_file(args: dict) -> str:
    path = paths.safe_resolve(args["path"])
    content = args["content"]
    content_lines = content.split("\n")
    if content_lines and content_lines[-1] == "":
        content_lines.pop()
    if args.get("offset") is None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"wrote {len(content)} chars to {path}"
    offset = int(args["offset"])
    n = int(args.get("lines", 0))
    if offset < 1 or n < 0:
        return "error: offset must be >= 1 and lines must be >= 0"
    try:
        existing = paths._read_lines(path)
    except FileNotFoundError:
        return f"error: file does not exist, cannot modify line range: {path}"
    if offset > len(existing) + 1:
        return f"error: offset {offset} is beyond end of file ({len(existing)} lines)"
    start = offset - 1
    result = existing[:start] + content_lines + existing[start + n :]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(result) + ("\n" if result else ""))
    if n > 0:
        return f"replaced lines {offset}-{offset + n - 1} of {path} with {len(content_lines)} line(s)"
    return f"inserted {len(content_lines)} line(s) before line {offset} in {path}"


# ---------------------------------------------------------------- gitignore
#
# grep/glob skip what git itself would not have shown in the same directory.
# The ignore engine is re-implemented here rather than asked of `git
# check-ignore`/`git ls-files`: no subprocess, no dependency on git being
# installed or on CWD being a work tree, and the same verdict on every platform
# the tests run on (including the Linux build container, where git refuses the
# mounted repo). Semantics follow git's own, verified against `git check-ignore`:
# a bare pattern matches at any depth, a pattern holding a separator is anchored
# to the file's directory, a trailing slash only restricts a rule to directories,
# `**` crosses directories only as a whole path component, the last matching line
# of the nearest .gitignore decides, and a pruned directory is never re-entered
# by a negation written inside it.


class _HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in config._HTML_SKIP_TAGS:
            self._skip += 1
        elif tag in config._HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in config._HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in config._HTML_SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in config._HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(markup: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        pass
    lines = [
        re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        for line in "".join(parser.parts).split("\n")
    ]
    out = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def tool_fetch_url(args: dict) -> str:
    url = (args.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return "error: url must be an http:// or https:// URL"
    try:
        timeout = float(args.get("timeout", 30))
    except (TypeError, ValueError):
        return "error: timeout must be a number"
    if timeout <= 0:
        return "error: timeout must be > 0"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "harnless/1.0",
            "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*",
        },
    )
    max_bytes = 2_000_000
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        return f"error: HTTP {e.code} {e.reason} for {url}"
    except urllib.error.URLError as e:
        return f"error: could not fetch {url}: {e.reason}"
    except (OSError, ValueError) as e:
        return f"error: could not fetch {url}: {e}"
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    try:
        text = data.decode(charset, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    if args.get("strip_html"):
        text = _html_to_text(text)
    limit = caps._limit_arg(args, "max_chars", config.FETCH_TEXT_LIMIT, config.TOOL_RESULT_LIMIT)
    clipped = len(text) > limit
    text = caps._truncate(text, limit, hint="raise max_chars, or fetch a narrower URL")
    if truncated and not clipped:
        text += caps._cap_note(
            f"body read stopped at {max_bytes} bytes",
            "the response is larger than the harness read ceiling",
        )
    return text if text else "(empty)"


def _closing_summary(messages: list, exit_note: str) -> str:
    """The hand-off text a sub-agent leaves behind ("" when it left none).

    Models put a summary in different places: the assistant reply they end on,
    the `exit` tool's `message` argument (which the tool schema advertises as a
    final message), or — reasoning models — only in `reasoning_content`. Taking
    the more substantive of the two closing texts means a closing "Done" never
    replaces a real summary, and a real summary is never dropped just because
    it went into `exit`. Anything that has to be dug out of an earlier turn is
    labelled, so the parent can tell a summary from leftover narration.
    """
    exit_note = (exit_note or "").strip()
    final_content = ""  # content of the sub-agent's last assistant turn
    earlier_content = ""  # closest earlier assistant turn that had any content
    reasoning = ""
    seen_final = False
    for m in reversed(messages):
        if m.get("role") != "assistant":
            continue
        content = (m.get("content") or "").strip()
        if not seen_final:
            seen_final = True
            final_content = content
        elif not earlier_content:
            earlier_content = content
        if not reasoning:
            reasoning = (m.get("reasoning_content") or "").strip()
        if final_content and earlier_content and reasoning:
            break
    closing = max((exit_note, final_content), key=len)
    if closing:
        return closing
    for salvage in (reasoning, earlier_content):
        if salvage:
            return config.SALVAGED_SUMMARY_NOTE + "\n" + salvage
    return ""


def tool_task(args: dict) -> str:
    """Delegate a task to a sub-agent: a nested run_agent with a fresh context.

    Returns the sub-agent's exit code and closing summary as the tool result;
    when the sub-agent left no hand-off text, the result says so rather than
    reading like a successful empty answer.
    """
    task = (args.get("task") or "").strip()
    if not task:
        return "error: empty task"
    if state._AGENT_DEPTH >= state.MAX_SUBAGENT_DEPTH:
        return (
            "error: sub-agent depth limit reached — nesting is capped at "
            f"{state.MAX_SUBAGENT_DEPTH} level(s) and this call would be level "
            f"{state._AGENT_DEPTH + 1}; do the work yourself"
        )
    system = context.get_system_prompt(state.CWD, config.SUBAGENT_NOTE)
    agents_md = context.load_agents_md()
    if agents_md:
        system += (
            "\n\nProject instructions (AGENTS.md in the working directory):\n"
            + agents_md
        )
    additions = context._context_additions()
    if additions:
        system += "\n\n" + additions
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]
    old_indent = state.OUTPUT_INDENT
    old_todo_state = state.TODO_LAST_INJECTED
    old_log_id = state.AGENT_LOG_ID
    conv_id = state._NEXT_CONV_ID
    state._NEXT_CONV_ID += 1
    # A sub-agent's log is named after where it sits in the delegation chain: the
    # session's first sub-agent is '1', the next is '2', and one of them
    # delegating again is '1.1' — so <session>.1.2.jsonl says who delegated it.
    parent_log_id = state.AGENT_LOG_ID
    child_n = state.SUBAGENT_CHILDREN.get(parent_log_id, 0) + 1
    state.SUBAGENT_CHILDREN[parent_log_id] = child_n
    log_id = f"{parent_log_id}.{child_n}" if parent_log_id else str(child_n)
    state.OUTPUT_INDENT = old_indent + "  "
    state.AGENT_LOG_ID = log_id
    journal._session_open(
        conv_id,
        journal._session_path(state.SESSION_ID, log_id),
        {
            "depth": state._AGENT_DEPTH + 1,
            "mode": "sub-agent",
            "subagent_id": log_id,
            "parent_log": journal._session_path(state.SESSION_ID, parent_log_id),
            "task": task,
        },
    )
    journal._session_messages(messages, conv_id)  # what this sub-agent was told, in its own log
    interrupted = False
    interrupt_note = ""
    step_limited = False
    code = None  # None until run_agent answers: an end record only if it did
    try:
        try:
            code = run_agent(
                messages,
                state.MODEL,
                interactive=False,
                temperature=state.TEMPERATURE,
                depth=state._AGENT_DEPTH + 1,
                conv_id=conv_id,
            )
        except errors.StreamInterrupted as e:
            # The user interrupted the sub-agent (or something it delegated).
            # Keep whatever it managed to say, then bubble the stop up: a
            # double ESC means "stop", not "carry on with half the work".
            interrupted = True
            interrupt_note = ((e.message or {}).get("content") or "").strip()
            code = 0
        except errors.SubagentStepLimit:
            step_limited = True
            code = config.SUBAGENT_STEP_LIMIT_CODE
    finally:
        state.OUTPUT_INDENT = old_indent
        state.TODO_LAST_INJECTED = old_todo_state
        state.AGENT_LOG_ID = old_log_id
        if code is not None:
            # The sub-agent's own log closes with how it ended: a reader (and the
            # next compaction pass) can tell a clean exit from a stop.
            journal._session_append(
                conv_id,
                {
                    "exit_code": code,
                    "interrupted": interrupted,
                    "step_limited": step_limited,
                    "type": "end",
                },
            )
        # The sub-agent's messages list is freed on return; drop its usage
        # entry so the address can't be recycled and resurrect stale usage.
        state.USAGE_BY_CONV.pop(conv_id, None)
        # Same for its journal state: the file stays on disk, but the conversation
        # key is free again (an id() fallback could otherwise land on it later).
        state.SESSION_FILES.pop(conv_id, None)
        state.SESSION_SEQ.pop(conv_id, None)
        state.SESSION_MSG_SEQ.pop(conv_id, None)
        state.COMPACT_STATS.pop(conv_id, None)
        state.SUBAGENT_CHILDREN.pop(log_id, None)
        # An `exit(message=...)` argument is the sub-agent's own closing
        # statement: _agent_loop prints it, and only here does it reach the
        # parent.
        exit_note = state.EXIT_NOTE_BY_CONV.pop(conv_id, "") or ""
    final = _closing_summary(messages, exit_note)
    if interrupt_note:
        final = interrupt_note
    if not final:
        # No hand-off text anywhere: say so. A bare "exit code: 0" reads to the
        # model as "the sub-agent found nothing", which is how a lost summary
        # gets paid for twice.
        final = config.NO_SUMMARY_NOTE
    if step_limited:
        final = (
            final
            + "\n\n(sub-agent stopped: it reached its step limit of "
            f"{state.SUBAGENT_STEP_LIMIT} model turns; this summary may be incomplete)"
        ).strip()
    final = caps._truncate(
        final,
        config.SUBAGENT_SUMMARY_LIMIT,
        hint="delegate a narrower task, or ask the sub-agent for a shorter summary",
    )
    if interrupted:
        if final.startswith(config.SUBAGENT_INTERRUPT_PREFIX):
            body = final[len(config.SUBAGENT_INTERRUPT_PREFIX):]  # wrapped already, deeper up
        else:
            body = f"\n{final}" if final else ""
        raise errors.SubagentInterrupted(
            {"role": "assistant", "content": config.SUBAGENT_INTERRUPT_PREFIX + body}
        )
    return f"exit code: {code}\n{final}"


# ---------------------------------------------------------------- state tools


# The contract the model reads lives in harnless_core/toolspec.py; this is the other
# half: which function answers each call. Keys must match toolspec.SPECS.
DISPATCH = {
    "get_cwd": tool_get_cwd,
    "run_shell": shell.tool_run_shell,
    "mkdir": tool_mkdir,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "grep": search.tool_grep,
    "patch_file": search.tool_patch_file,
    "glob": search.tool_glob,
    "list_dir": search.tool_list_dir,
    "delete_file": search.tool_delete_file,
    "move_file": search.tool_move_file,
    "copy_file": search.tool_copy_file,
    "fetch_url": tool_fetch_url,
    "todo": context.tool_todo,
    "memory": context.tool_memory,
    "task": tool_task,
    "ask_user": tool_ask_user,
    "sessions": journal.tool_sessions,
    "exit": tool_exit,
}


# ---------------------------------------------------------------- mcp


def _builtin_tool_names() -> set:
    """The names the harness itself owns, for the MCP registration to refuse."""
    return set(DISPATCH)


# ---------------------------------------------------------------- file references


# ---------------------------------------------------------------- line editor


# ---------------------------------------------------------------- client


def execute_tool(name: str, raw_args: str) -> str:
    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        return f"error: invalid JSON arguments: {raw_args}"
    if name in state.DISABLED_TOOLS:
        return f"error: tool '{name}' is disabled (use /tools to re-enable)"
    fn = DISPATCH.get(name)
    if fn is None:
        entry = state.MCP_DISPATCH.get(name)
        if entry is not None:
            client, tool_name = entry
            try:
                result = client.call_tool(tool_name, args)
            except mcp.MCPError as e:
                return f"error: {e}"
            except Exception as e:
                return f"error: {type(e).__name__}: {e}"
        else:
            return f"error: unknown tool: {name}"
    else:
        try:
            result = str(fn(args))
        except (errors.ExitSignal, errors.StreamInterrupted):
            # Control-flow, not a tool failure: `exit` ends the agent's loop,
            # an interrupt ends the whole delegation chain.
            raise
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"
    # Final backstop: no single tool result may exceed this, whatever the
    # built-in tool (or an external MCP server) decided to return.
    return caps._truncate(
        result,
        config.TOOL_RESULT_LIMIT,
        unit="chars",
        hint="output exceeded the harness limit; split the work or narrow the request",
    )


def _active_tools(interactive: bool) -> list:
    """Tool specs to send to the API, excluding tools toggled off via /tools."""
    base = toolspec.OPENAI_TOOLS_INTERACTIVE if interactive else toolspec.OPENAI_TOOLS
    return [s for s in base + state.MCP_TOOLS if s["function"]["name"] not in state.DISABLED_TOOLS]


def _all_tool_names() -> set:
    return {s["function"]["name"] for s in toolspec.OPENAI_TOOLS_INTERACTIVE + state.MCP_TOOLS}


def format_tools() -> str:
    """Build the /tools checklist: '[X] name — description' per tool.

    MCP tools are grouped under their server ('[X] mcp: server (N tools)'),
    mirroring the interactive menu.
    """
    lines = []
    for s in sorted(toolspec.OPENAI_TOOLS_INTERACTIVE, key=lambda s: s["function"]["name"]):
        name = s["function"]["name"]
        desc = s["function"].get("description", "")
        mark = " " if name in state.DISABLED_TOOLS else "X"
        lines.append(f"[{mark}] {name} — {desc}")
    mcp_descs = {s["function"]["name"]: s["function"].get("description", "") for s in state.MCP_TOOLS}
    mcp_by_server = {}
    for name, (client, _) in state.MCP_DISPATCH.items():
        mcp_by_server.setdefault(client.name, []).append(name)
    server_order = [c.name for c in state.MCP_CLIENTS if c.name in mcp_by_server]
    if server_order:
        lines.append("mcp servers:")
        for server in server_order:
            tools = sorted(mcp_by_server[server])
            off = sum(1 for t in tools if t in state.DISABLED_TOOLS)
            mark = "X" if off == 0 else (" " if off == len(tools) else "-")
            lines.append(f"[{mark}] mcp: {server} ({len(tools)} tools)")
            for t in tools:
                tmark = " " if t in state.DISABLED_TOOLS else "X"
                lines.append(f"  [{tmark}] {t} — {mcp_descs.get(t, '')}")
    if state.PENDING_MCP:
        lines.append("mcp servers (disabled in config):")
        for n, _ in state.PENDING_MCP:
            lines.append(f"[ ] {n}")
    return "\n".join(lines)


def toggle_tools(names: list) -> list:
    """Toggle the given tool names on/off. Returns [(name, 'on'|'off'|'unknown')].

    A name of the form 'mcp:<server>' toggles every tool of that server at
    once (any off -> all on, otherwise all off).
    """
    known = _all_tool_names()
    results = []
    for n in names:
        if n.startswith("mcp:"):
            server = n[len("mcp:"):]
            tools = mcp._mcp_server_tools(server)
            if not tools:
                results.append((n, "unknown"))
            else:
                any_off = any(t in state.DISABLED_TOOLS for t in tools)
                for t in tools:
                    if any_off:
                        state.DISABLED_TOOLS.discard(t)
                    else:
                        state.DISABLED_TOOLS.add(t)
                results.append((n, "on" if any_off else "off"))
        elif n not in known:
            results.append((n, "unknown"))
        elif n in state.DISABLED_TOOLS:
            state.DISABLED_TOOLS.discard(n)
            results.append((n, "on"))
        else:
            state.DISABLED_TOOLS.add(n)
            results.append((n, "off"))
    return results


def tools_menu(keys=None) -> bool:
    """Interactive tool toggle menu.

    Built-in tools are listed flat; mcp tools are grouped under their server
    entry, collapsed by default — '+'/'=' expands the group under the cursor,
    '-' collapses it. Space on a server row toggles all of its tools at once
    (individual tools can be toggled on their own rows when expanded). A
    disabled-in-config mcp server is enabled by toggling its row. Up/down
    move the cursor (skipping collapsed rows), enter applies the changes and
    quits, esc (or ctrl+c/ctrl+d) quits without applying them. Returns True
    if changes were applied, False if cancelled.
    """
    builtin_specs = sorted(
        toolspec.OPENAI_TOOLS_INTERACTIVE, key=lambda s: s["function"]["name"]
    )
    mcp_by_server = {}
    for name, (client, _) in state.MCP_DISPATCH.items():
        mcp_by_server.setdefault(client.name, []).append(name)
    server_order = [c.name for c in state.MCP_CLIENTS if c.name in mcp_by_server]
    pending_names = [n for n, _ in state.PENDING_MCP]
    rows = [("tool", s["function"]["name"]) for s in builtin_specs]
    for server in server_order:
        rows.append(("server", server))
        for t in sorted(mcp_by_server[server]):
            rows.append(("mcp_tool", t))
    rows += [("pending", n) for n in pending_names]
    if not rows:
        return False
    descs = {s["function"]["name"]: s["function"].get("description", "") for s in builtin_specs}
    for s in state.MCP_TOOLS:
        descs[s["function"]["name"]] = s["function"].get("description", "")
    disabled = set(state.DISABLED_TOOLS)
    pending_on = set()
    collapsed = set(server_order)  # mcp groups start collapsed
    tool_server = {}
    for server in server_order:
        for t in mcp_by_server[server]:
            tool_server[t] = server
    cursor = 0
    width = shutil.get_terminal_size((80, 24)).columns
    applied = False
    enable_msgs = []
    n_builtin = len(builtin_specs)
    n_server_rows = sum(1 + len(mcp_by_server[s]) for s in server_order)

    def row_desc(kind, name):
        if kind == "pending":
            return f"mcp server '{name}' (disabled in config)"
        if kind == "server":
            return (
                f"mcp server '{name}' — {len(mcp_by_server[name])} tools "
                "(space toggles all, +/- expand/collapse)"
            )
        return descs.get(name, "")

    def server_mark(server):
        tools = mcp_by_server[server]
        off = sum(1 for t in tools if t in disabled)
        if off == 0:
            return "x"
        if off == len(tools):
            return " "
        return "-"

    def visible_indices():
        return [
            i
            for i, (kind, name) in enumerate(rows)
            if not (kind == "mcp_tool" and tool_server[name] in collapsed)
        ]

    def build_lines():
        lines = [
            render.colorize(
                "tools — space: toggle, +/-: expand/collapse, enter: apply, esc: cancel",
                "dim",
            )
        ]
        for i, (kind, name) in enumerate(rows):
            if kind == "mcp_tool" and tool_server[name] in collapsed:
                continue
            if i == n_builtin and server_order:
                lines.append(render.colorize("  mcp servers:", "dim"))
            if i == n_builtin + n_server_rows and pending_names:
                lines.append(render.colorize("  mcp servers (disabled in config):", "dim"))
            if kind == "server":
                mark = server_mark(name)
            elif kind == "pending":
                mark = "x" if name in pending_on else " "
            else:
                mark = " " if name in disabled else "x"
            indent = "    " if kind == "mcp_tool" else "  "
            if i == cursor:
                lines.append(render.colorize(f">{indent[1:]}[{mark}] {name}", "tool"))
            else:
                lines.append(f"{indent}[{mark}] {name}")
        desc = row_desc(*rows[cursor])
        if len(desc) > width - 1:
            desc = desc[: width - 4] + "..."
        lines.append(render.colorize(desc, "dim"))
        return lines

    drawn = 0

    def draw():
        nonlocal drawn
        lines = build_lines()
        for line in lines:
            sys.stdout.write("\r\x1b[2K" + line + "\n")
        if lines:
            sys.stdout.write(f"\x1b[{len(lines)}A")
        sys.stdout.flush()
        drawn = len(lines)

    def move(step):
        nonlocal cursor
        vis = visible_indices()
        pos = vis.index(cursor)
        cursor = vis[max(0, min(len(vis) - 1, pos + step))]

    if keys is None:
        keys = lineedit._iter_keys_windows() if os.name == "nt" else lineedit._iter_keys_posix()
    draw()
    try:
        while True:
            token = next(keys)
            if token == "up":
                move(-1)
            elif token == "down":
                move(1)
            elif token in (("char", "+"), ("char", "=")):
                kind, name = rows[cursor]
                if kind == "server":
                    collapsed.discard(name)
            elif token == ("char", "-"):
                kind, name = rows[cursor]
                if kind == "server":
                    collapsed.add(name)
            elif token in ("enter", "ctrl_enter"):
                state.DISABLED_TOOLS.clear()
                state.DISABLED_TOOLS.update(disabled)
                for name in pending_on:
                    _, msg = mcp._enable_pending_mcp(name, _builtin_tool_names())
                    enable_msgs.append(msg)
                applied = True
                break
            elif token in ("esc", "ctrl_c", "ctrl_d"):
                break
            elif token == ("char", " "):
                kind, name = rows[cursor]
                if kind == "pending":
                    if name in pending_on:
                        pending_on.discard(name)
                    else:
                        pending_on.add(name)
                elif kind == "server":
                    tools = mcp_by_server[name]
                    if any(t in disabled for t in tools):
                        disabled.difference_update(tools)
                    else:
                        disabled.update(tools)
                else:
                    if name in disabled:
                        disabled.discard(name)
                    else:
                        disabled.add(name)
            draw()
    except StopIteration:
        pass
    finally:
        close = getattr(keys, "close", None)
        if close is not None:
            close()
        if drawn:
            sys.stdout.write(f"\x1b[{drawn}B")
            sys.stdout.flush()
    for msg in enable_msgs:
        print(msg)
    return applied


def format_status(messages: list, context_window: int = 0, conv_id: int = None) -> str:
    """Build the /status report: context usage, API URL, tool names, MCP servers."""
    tools = _active_tools(True)
    total_chars = measure.estimate_context_chars(messages, tools)
    approx_tokens = total_chars // 4
    usage = state.USAGE_BY_CONV.get(conv_id if conv_id is not None else id(messages))
    prompt_tokens = (
        int(usage["prompt_tokens"])
        if isinstance(usage, dict) and usage.get("prompt_tokens")
        else 0
    )
    tool_names = ", ".join(sorted(s["function"]["name"] for s in tools))
    if prompt_tokens:
        ctx = f"context: {prompt_tokens} tokens (server-reported) in {len(messages)} messages"
    else:
        ctx = f"context: ~{approx_tokens} tokens (estimated, ~{total_chars} chars) in {len(messages)} messages"
    if context_window > 0:
        used = prompt_tokens or approx_tokens
        pct = used * 100 // context_window
        pct_str = "<1%" if pct == 0 and used > 0 else f"~{pct}%"
        ctx += f" ({pct_str} of {context_window} window)"
    lines = [
        ctx,
        f"api url: {state.API_URL}",
        f"tools: {tool_names}",
        f"session: {state.SESSION_ID or '(no session id)'}  {journal._session_note()}",
        compact.format_compaction(),
    ]
    if state.MCP_CLIENTS:
        mcp_lines = []
        for c in state.MCP_CLIENTS:
            names = ", ".join(c.tool_names) if c.tool_names else "no tools"
            mcp_lines.append(f"  {c.name} ({c.transport}): {names}")
        lines.append("mcp servers:\n" + "\n".join(mcp_lines))
    if state.PENDING_MCP:
        pend_lines = [f"  {n} (disabled in config; enable via /tools)" for n, _ in state.PENDING_MCP]
        lines.append("mcp servers (disabled):\n" + "\n".join(pend_lines))
    return "\n".join(lines)


REPL_COMMANDS = [
    ("/new", "clear session history and start over (in a new session log)"),
    ("/clear-screen", "clear the terminal screen"),
    ("/status", "show context usage, api url, and tools"),
    ("/sessions", "list the recorded session logs"),
    ("/resume", "continue a recorded session: /resume <session-id> (no argument lists them)"),
    ("/compact", "replace this session's older turns with a handoff note now"),
    ("/auto-compact", "on|off, or 'threshold N': compact automatically as the context fills up"),
    ("/tools", "interactive tool menu: up/down move, space toggle, enter apply, esc cancel"),
    ("/auto-send", "on: Enter submits (default); off: Enter inserts a newline, Ctrl+Enter sends"),
    ("/help", "show this help"),
    ("/exit", "quit (alias: /quit)"),
]


def set_auto_send(arg: str) -> str:
    """Apply an /auto-send argument; return the status line to print.

    Returns '' for an unknown argument (the caller prints a usage error).
    """
    v = arg.strip().lower()
    if v in ("on", "true", "yes", "1"):
        state.AUTO_SEND = True
        return "auto-send on — Enter submits, Ctrl+J inserts a newline"
    if v in ("off", "false", "no", "0"):
        state.AUTO_SEND = False
        return "auto-send off — Enter inserts a newline, Ctrl+Enter (or Ctrl+D) submits"
    if not v:
        return f"auto-send: {'on' if state.AUTO_SEND else 'off'}"
    return ""


def commands_menu(keys=None) -> str | None:
    """Interactive REPL command menu, opened by typing / on an empty line.

    Up/down move the cursor, enter selects the highlighted command (returned),
    esc (or ctrl+c/ctrl+d) cancels (returns None). Draws below the prompt
    line and restores the cursor to it on exit.
    """
    names = [name for name, _ in REPL_COMMANDS]
    descs = [desc for _, desc in REPL_COMMANDS]
    cursor = 0
    width = shutil.get_terminal_size((80, 24)).columns

    def build_lines():
        lines = [render.colorize("commands — up/down: move, enter: select, esc: cancel", "dim")]
        for i, name in enumerate(names):
            if i == cursor:
                lines.append(render.colorize(f"> {name}", "tool"))
            else:
                lines.append(f"  {name}")
        desc = descs[cursor]
        if len(desc) > width - 1:
            desc = desc[: width - 4] + "..."
        lines.append(render.colorize(desc, "dim"))
        return lines

    drawn = 0

    def draw():
        nonlocal drawn
        lines = build_lines()
        for line in lines:
            sys.stdout.write("\r\x1b[2K" + line + "\n")
        if lines:
            sys.stdout.write(f"\x1b[{len(lines)}A")
        sys.stdout.flush()
        drawn = len(lines)

    if keys is None:
        keys = lineedit._iter_keys_windows() if os.name == "nt" else lineedit._iter_keys_posix()
    # Start on the line below the prompt (the cursor sits at its end).
    sys.stdout.write("\r\n")
    sys.stdout.flush()
    draw()
    try:
        while True:
            token = next(keys)
            if token == "up":
                cursor = max(0, cursor - 1)
            elif token == "down":
                cursor = min(len(names) - 1, cursor + 1)
            elif token in ("enter", "ctrl_enter"):
                return names[cursor]
            elif token in ("esc", "ctrl_c", "ctrl_d"):
                return None
            draw()
    except StopIteration:
        return None
    finally:
        close = getattr(keys, "close", None)
        if close is not None:
            close()
        if drawn:
            # Cursor is on the first menu line: clear each menu line, then
            # return to the prompt line.
            out = ""
            for i in range(drawn):
                out += "\x1b[2K"
                if i < drawn - 1:
                    out += "\x1b[B"
            out += f"\x1b[{drawn}A"
            sys.stdout.write(out)
            sys.stdout.flush()


def format_help() -> str:
    """Build the /help report: list of REPL commands and file references."""
    lines = [f"{name:<16} {desc}" for name, desc in REPL_COMMANDS]
    lines.append(f"{'/tools <name>':<16} toggle a tool on/off ('mcp:<server>' toggles all of a server's tools)")
    lines.append("Ctrl+J          insert a newline (multi-line input)")
    lines.append("Enter           submit (auto-send on) — insert a newline (auto-send off)")
    lines.append("Ctrl+Enter      submit (auto-send off; Ctrl+D also sends)")
    lines.append(f"{'esc esc':<16} interrupt generation (press twice within 2 s)")
    return (
        "\n".join(lines)
        + "\n"
        "\n"
        "file references (attach a file to your message):\n"
        "  @[cwd://relative/path]   file relative to the working directory\n"
        "  @[file:///abs/path]      absolute local file\n"
        "  @[http(s)://host/file]   remote file\n"
        "text is inlined; images are sent when the model supports vision"
    )


# ---------------------------------------------------------------- loop


def run_agent(
    messages: list,
    model: str,
    interactive: bool = False,
    temperature: float = 1.0,
    depth: int = 0,
    conv_id: int = None,
) -> int:
    """Run the agent loop; returns the exit code of the model's `exit` call.

    `_AGENT_DEPTH` is scoped to this call: it is set to `depth` for the
    duration of the run (0 = top-level agent, 1 = sub-agent, 2 = a sub-agent
    of a sub-agent) and restored on the way out — on a return *or* an
    exception. Leaving it behind lets depth ratchet up inside a single turn:
    after one run nested to level 2, the delegating agent — and every later
    `task` call in the same turn, top-level ones included — would still be
    sitting at level 2 and wrongly told the nesting limit was reached.
    """
    previous_depth = state._AGENT_DEPTH
    state._AGENT_DEPTH = max(0, depth)
    try:
        return _agent_loop(messages, model, interactive, temperature, conv_id)
    finally:
        state._AGENT_DEPTH = previous_depth


def _agent_loop(
    messages: list,
    model: str,
    interactive: bool = False,
    temperature: float = 1.0,
    conv_id: int = None,
) -> int:
    """The agent loop: stream a turn, run its tool calls, repeat until the
    model answers without tool calls, calls `exit`, or is interrupted."""
    # A sub-agent must not loop forever: uncapped, a model that keeps calling
    # tools burns its context (and the summary it hands back) unattended. A
    # top-level agent is user-steered and double-ESC-able, so it stays uncapped.
    is_subagent = state._AGENT_DEPTH > 0
    steps = 0
    # A capped run gets one extra turn devoted to the hand-off. A sub-agent cut
    # off at the top of this loop has usually just finished a tool-call turn, so
    # stopping cold would leave tool_task with no summary to hand the parent.
    closing_turn = False
    # Journal key: the same key _record_usage keys this conversation's usage by —
    # MAIN_CONV_ID / a sub-agent conv id, else id(messages) — so a conversation's
    # records land in that conversation's log (and nowhere else if it has one).
    conv_key = conv_id if conv_id is not None else id(messages)
    def _step_limited() -> None:
        print(
            state.OUTPUT_INDENT
            + render.colorize(
                f"{render.icon('exit')} sub-agent stopped: step limit "
                f"({state.SUBAGENT_STEP_LIMIT}) reached",
                "error",
            )
        )
        raise errors.SubagentStepLimit(f"sub-agent exceeded {state.SUBAGENT_STEP_LIMIT} model turns")

    while True:
        if is_subagent and state.SUBAGENT_STEP_LIMIT and steps >= state.SUBAGENT_STEP_LIMIT:
            if closing_turn:
                _step_limited()
            closing_turn = True
            print(
                state.OUTPUT_INDENT
                + render.colorize(
                    f"{render.icon('exit')} sub-agent reached its step limit "
                    f"({state.SUBAGENT_STEP_LIMIT}) — one closing turn to summarise",
                    "error",
                )
            )
            messages.append({"role": "user", "content": config.STEP_LIMIT_CLOSE_PROMPT})
            journal._session_message(messages[-1], conv_key)
        # Make room before asking, not after the server refuses: compact when this
        # conversation's usage has crossed the threshold of the window.
        # What this turn advertises is the agent's decision (which tools the model may
        # call); the request layer only sends the list it is handed — see _request_body.
        tools = _active_tools(interactive)
        compact._compact_if_needed(messages, conv_key, model, tools)
        steps += 1
        # A rejected-for-size prompt gets retried after compacting — one turn either
        # way, so `steps` counts it once, and `attempts` is how many times compaction
        # has already been given a shot at this request.
        attempts = 0
        while True:
            message = None
            streamed = False
            try:
                message, streamed = request.stream_once(
                    messages,
                    model,
                    tools=tools,
                    temperature=temperature,
                    conv_id=conv_id,
                )
                if not streamed:
                    message = None
            except errors.ContextOverflow as e:
                if not compact._recover_overflow(messages, conv_key, model, str(e), attempts, tools):
                    print(
                        state.OUTPUT_INDENT
                        + render.colorize(f"{render.icon('error')} out of context ({e}) — this run stops here", "error")
                    )
                    print(state.OUTPUT_INDENT + render.colorize(journal._transcript_hint(conv_key), "dim"))
                    return config.CONTEXT_OVERFLOW_CODE
                attempts += 1
                continue
            except errors.StreamInterrupted as e:
                # Keep the partial output (minus any half-formed tool calls,
                # which would leave the conversation in an invalid state) so
                # the model can see what it had said, then back out. Partials
                # without content (e.g. interrupted mid-thinking) are dropped:
                # servers reject assistant messages that have neither content
                # nor tool_calls, which would break every later request.
                message = e.message
                message.pop("tool_calls", None)
                if message.get("content"):
                    messages.append(message)
                    journal._session_message(message, conv_key)  # what was said before the stop
                if is_subagent:
                    # Hand the stop to whoever delegated us: tool_task turns it
                    # into a stopped-sub-agent result and re-raises, so the whole
                    # chain halts instead of resuming with half the work done.
                    raise
                return 0
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                print(
                    state.OUTPUT_INDENT
                    + render.colorize(
                        f"{render.icon('error')} streaming failed ({e}); retrying non-streaming",
                        "error",
                    )
                )
            if message is None:
                try:
                    data = request.chat(
                        messages,
                        model,
                        tools=tools,
                        temperature=temperature,
                        conv_id=conv_id,
                    )
                except errors.ContextOverflow as e:
                    if not compact._recover_overflow(messages, conv_key, model, str(e), attempts, tools):
                        print(
                            state.OUTPUT_INDENT
                            + render.colorize(f"{render.icon('error')} out of context ({e}) — this run stops here", "error")
                        )
                        print(state.OUTPUT_INDENT + render.colorize(journal._transcript_hint(conv_key), "dim"))
                        return config.CONTEXT_OVERFLOW_CODE
                    attempts += 1
                    continue
                except urllib.error.URLError as e:
                    print(
                        state.OUTPUT_INDENT
                        + render.colorize(f"{render.icon('error')} connection error: {e}", "error")
                    )
                    return 1
                message = data["choices"][0]["message"]
            break
        messages.append(message)
        journal._session_message(message, conv_key)  # the model's answer, as recorded

        reasoning = message.get("reasoning_content")
        if reasoning and not streamed:
            render.print_reasoning(reasoning)

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            if not streamed:
                render.print_assistant(message.get("content", ""))
            if closing_turn:
                # The closing turn replied: still report the run as capped, so
                # the parent knows the summary may be partial.
                _step_limited()
            return 0

        if message.get("content") and not streamed:
            render.print_assistant(message["content"])
        for index, tc in enumerate(tool_calls):
            name = tc["function"]["name"]
            raw_args = tc["function"].get("arguments", "")
            render.print_tool_call(name, raw_args)
            try:
                result = execute_tool(name, raw_args)
            except errors.ExitSignal as e:
                if e.message:
                    print(
                        state.OUTPUT_INDENT + render.colorize(f"{render.icon('exit')} {e.message}", "tool")
                    )
                if is_subagent and conv_id is not None and e.message:
                    # A closing statement the parent would otherwise never see:
                    # tool_task harvests it as the sub-agent's summary.
                    state.EXIT_NOTE_BY_CONV[conv_id] = e.message
                # Answer the exit call and everything queued behind it, so the
                # batch never leaves tool_calls unanswered (as the interrupt
                # branch below does) — harmless today, since this agent's
                # conversation ends here, but a broken request waiting to happen
                # for anything that replays the conversation later (a resumed
                # session closes such dangling calls — see _session_rebuild).
                answered = [
                    {"role": "tool", "tool_call_id": tc["id"], "content": f"exiting with code {e.code}"},
                    *[
                        {
                            "role": "tool",
                            "tool_call_id": pending["id"],
                            "content": f"skipped: the agent exited with code {e.code}",
                        }
                        for pending in tool_calls[index + 1:]
                    ],
                ]
                messages.extend(answered)
                journal._session_messages(answered, conv_key)
                if closing_turn:
                    _step_limited()  # a capped run reports itself capped
                return e.code
            except errors.StreamInterrupted as e:
                # A tool (the task tool) was interrupted. Answer this call and
                # every remaining one in the batch — a server rejects an
                # assistant message whose tool calls were never answered — then
                # stop this agent's turn, or propagate the stop up to whoever
                # delegated us (tool_task re-raises it).
                note = ((e.message or {}).get("content") or "").strip()
                if not note:
                    note = "interrupted by user"
                answered = [
                    {"role": "tool", "tool_call_id": tc["id"], "content": note},
                    *[
                        {"role": "tool", "tool_call_id": pending["id"], "content": "interrupted by user"}
                        for pending in tool_calls[index + 1:]
                    ],
                ]
                messages.extend(answered)
                journal._session_messages(answered, conv_key)
                if is_subagent:
                    raise
                return 0
            render.print_tool_result(result)
            tool_result = {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            }
            messages.append(tool_result)
            journal._session_message(tool_result, conv_key)
        context._todo_reminder(messages, conv_key)


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[reportAttributeAccessIssue]
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description=f"harnless {config.VERSION}: minimal LLM agent harness"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"harnless {config.VERSION}",
        help="show the harnless version and exit",
    )
    parser.add_argument(
        "--api-url",
        default=state.API_URL,
        help=(
            "OpenAI-compatible API URL: either a base URL "
            "(e.g. https://openrouter.ai/api/v1) or a full chat completions "
            f"endpoint (default: {state.API_URL})"
        ),
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key sent as 'Authorization: Bearer <key>' (omit for local servers that need no auth)",
    )
    parser.add_argument(
        "--model",
        default="local-model",
        help="model name to send in the request (llama-server usually ignores it)",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="system prompt to use (replaces the built-in one)",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="one-shot mode: send this as the only user message, then exit",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="sampling temperature (default: 1.0; lower = more deterministic tool calls)",
    )
    parser.add_argument(
        "--max-subagents",
        type=int,
        default=2,
        help="maximum sub-agent nesting depth for the task tool (default: 2; "
        f"0 disables sub-agents, max {config.SUBAGENT_DEPTH_CEILING})",
    )
    parser.add_argument(
        "--max-subagent-steps",
        type=int,
        default=40,
        help="model turns a single sub-agent may take before its run is "
        "stopped (default: 40; 0 = unlimited; top-level agents are never capped)",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=0,
        help="model context window size in tokens, shown as a percentage in /status (0 = not shown; default: probed from the API)",
    )
    parser.add_argument(
        "--no-auto-compact",
        action="store_true",
        help=(
            "do not compact the conversation automatically when the context fills up "
            "(a rejected-for-size prompt then ends the run with exit code 3; /compact "
            "still works)"
        ),
    )
    parser.add_argument(
        "--compact-threshold",
        type=int,
        default=state.COMPACT_THRESHOLD_PCT,
        metavar="PCT",
        help=f"auto-compact once usage reaches this percentage of the context window (default: {state.COMPACT_THRESHOLD_PCT}, max {config.COMPACT_THRESHOLD_CEILING})",
    )
    parser.add_argument(
        "--no-session-log",
        action="store_true",
        help=(
            "do not journal the session (the .jsonl transcript under "
            "SESSION_DIR: .harnless/sessions unless overridden)"
        ),
    )
    parser.add_argument(
        "--sessions-dir",
        default=None,
        metavar="DIR",
        help="where session logs are written (default: .harnless/sessions, or HARNLESS_SESSIONS_DIR)",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION_ID",
        help=(
            "continue a recorded session: a session id, an '<id>.<chain>' "
            "sub-agent log, 'latest', or a path to a .jsonl log"
        ),
    )
    parser.add_argument(
        "--resume-last",
        type=int,
        nargs="?",
        const=0,
        default=None,
        metavar="N",
        help=(
            f"continue the newest recorded session, keeping only its last N "
            f"messages (no value or 0 = all, max {config.SESSION_KEEP_CEILING}); "
            f"with --resume it only trims that session"
        ),
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI color output (also auto-disabled for piped output and NO_COLOR)",
    )
    parser.add_argument(
        "--no-emoji",
        action="store_true",
        help="use plain ASCII labels instead of emoji icons",
    )
    parser.add_argument(
        "--no-vision",
        action="store_true",
        help="do not send image file references to the model (replace them with a text note); use with text-only models",
    )
    parser.add_argument(
        "--shell",
        choices=["auto", "pwsh", "powershell", "cmd"],
        default="auto",
        help=(
            "shell for run_shell on Windows: auto (pwsh → powershell → cmd), "
            "or force pwsh / powershell / cmd (default: auto; ignored off-Windows)"
        ),
    )
    parser.add_argument(
        "--mcp-config",
        action="append",
        default=None,
        metavar="FILE",
        help="MCP config file (JSON); repeatable. Accepts {'mcpServers': {...}} or a bare {...}",
    )
    parser.add_argument(
        "--mcp-stdio",
        action="append",
        default=None,
        metavar="NAME:COMMAND ARGS...",
        help="add a stdio MCP server, e.g. --mcp-stdio 'fs:npx -y @modelcontextprotocol/server-filesystem ./data'",
    )
    parser.add_argument(
        "--mcp-http",
        action="append",
        default=None,
        metavar="NAME=URL",
        help="add an http MCP server, e.g. --mcp-http 'api=http://127.0.0.1:8000/mcp'",
    )
    args = parser.parse_args()

    state.API_URL = request.normalize_api_url(args.api_url)
    state.API_KEY = args.api_key
    state.MODEL = args.model
    state.TEMPERATURE = args.temperature
    state.MAX_SUBAGENT_DEPTH = caps._clamp_int(args.max_subagents, 0, config.SUBAGENT_DEPTH_CEILING)
    state.SUBAGENT_STEP_LIMIT = caps._clamp_int(args.max_subagent_steps, 0, config.SUBAGENT_STEPS_CEILING)
    state.VISION_ENABLED = not args.no_vision
    state.SHELL_PREFERRED = args.shell
    state.SESSION_LOG = not args.no_session_log
    if args.sessions_dir:
        state.SESSION_DIR = os.path.abspath(os.path.expanduser(args.sessions_dir))
    state.AUTO_COMPACT = not args.no_auto_compact
    state.COMPACT_THRESHOLD_PCT = caps._clamp_int(args.compact_threshold, 1, config.COMPACT_THRESHOLD_CEILING)

    context_window = args.context_window
    if context_window == 0 and args.prompt is None:
        context_window = request.probe_context_window()
    # The compaction triggers compare against this: 0 (no flag, nothing probed) means
    # no proactive compaction — the reactive one still fires if the server complains.
    state.CONTEXT_WINDOW = max(0, int(context_window or 0))

    render.set_color_enabled(not args.no_color and render.color_enabled())
    render.set_emoji_enabled(not args.no_emoji and state._stdout_can_encode_emoji())

    mcp_servers = {}
    if os.path.exists(config.MCP_HOME_CONFIG):
        mcp._load_mcp_config_into(mcp_servers, config.MCP_HOME_CONFIG)
    for path in args.mcp_config or []:
        mcp._load_mcp_config_into(mcp_servers, path)
    for spec in args.mcp_stdio or []:
        try:
            name, cfg = mcp._parse_mcp_stdio(spec)
        except mcp.MCPError as e:
            print(render.colorize(f"{render.icon('error')} {e}", "error"))
            continue
        mcp_servers[name] = cfg
    for spec in args.mcp_http or []:
        try:
            name, cfg = mcp._parse_mcp_http(spec)
        except mcp.MCPError as e:
            print(render.colorize(f"{render.icon('error')} {e}", "error"))
            continue
        mcp_servers[name] = cfg

    # Servers with "enabled": false are kept pending; the user can enable them
    # from the /tools menu (which connects them on demand).
    for name in [n for n, cfg in mcp_servers.items() if not cfg.get("enabled", True)]:
        cfg = mcp_servers.pop(name)
        cfg.pop("enabled", None)
        state.PENDING_MCP.append((name, cfg))

    if mcp_servers:
        mcp_clients = [mcp.MCPClient(name, cfg) for name, cfg in mcp_servers.items()]
        mcp.register_mcp_tools(mcp_clients, _builtin_tool_names())

    if mcp_servers or state.PENDING_MCP:
        def _close_mcp():
            for c in state.MCP_CLIENTS:
                try:
                    c.close()
                except Exception:
                    pass

        atexit.register(_close_mcp)

    system_prompt_additions = (
        ""
        if args.prompt is None
        else (
            "When a task is done, summarize what you did concisely and call the exit tool with code 0. "
            "If the task cannot be completed, call the exit tool with a non-zero code and explain why."
        )
    )

    system_prompt = (
        args.system_prompt
        if args.system_prompt is not None
        else context.get_system_prompt(state.CWD, system_prompt_additions)
    )

    agents_md = context.load_agents_md()
    if agents_md:
        system_prompt += (
            "\n\nProject instructions (AGENTS.md in the working directory):\n"
            + agents_md
        )
    additions = context._context_additions()
    if additions:
        system_prompt += "\n\n" + additions
    context._todo_load()
    state.SESSION_MODE = "one-shot" if args.prompt is not None else "interactive"
    resume_spec, resume_keep, resume_implied = journal._resume_target(args.resume, args.resume_last)
    resumed = None  # restored messages; None means this run starts fresh
    # A resumed session is replayed back to the user only in the REPL: a one-shot run
    # has nobody standing there to read it.
    resume_echo = 0 if args.prompt is not None else config.RESUME_TAIL_ITEMS
    if resume_spec:
        restored, note = journal.resume_session(
            resume_spec, keep=resume_keep, mode=state.SESSION_MODE, echo=resume_echo
        )
        if restored is None:
            if not resume_implied:
                print(render.colorize(f"{render.icon('error')} {note}", "error"))
                sys.exit(1)
            # --resume-last on its own asks for whoever was here last; when nobody
            # was, that is not an error — say so and open a new session instead.
            print(render.colorize(f"{render.icon('wait')} {note} — starting a new session", "dim"))
        else:
            resumed = restored
            if not resume_echo:  # with echo on, resume_session already said it
                print(render.colorize(note, "dim"))
    if resumed is None:
        journal.start_session(state.SESSION_MODE)
        messages = [{"role": "system", "content": system_prompt}]
    else:
        # The transcript comes back; the system prompt does not. The resumed run
        # describes itself as it is now (this version, current AGENTS.md, current
        # memory notes) instead of replaying what an older run was told.
        messages = [{"role": "system", "content": system_prompt}] + resumed
    # The log starts where the conversation does. Restored messages are not
    # re-journaled: the file is one continuous transcript, not a copy per resume.
    journal._session_prepend(messages[0], state.MAIN_CONV_ID)

    if args.prompt is not None:
        print(render.colorize(f"harnless {config.VERSION} one-shot in {state.CWD} (api: {state.API_URL})", "dim"))
        print(render.colorize(journal._session_note(), "dim"))
        messages.append(context.build_user_message(args.prompt))
        journal._session_message(messages[-1], state.MAIN_CONV_ID)
        sys.exit(
            run_agent(messages, args.model, temperature=args.temperature,
                      conv_id=state.MAIN_CONV_ID)
        )

    context._history_load()

    state.INTERRUPT_ENABLED = sys.stdin.isatty()

    print(render.colorize(f"harnless {config.VERSION} ready in {state.CWD} (api: {state.API_URL})", "dim"))
    banner = (
        "type / for a command menu, /help for all commands, /exit to quit\n"
        "attach files with @[cwd://rel/path], @[file:///abs/path] or @[http(s)://host/file]\n"
        "use up/down arrows to recall previous input, Ctrl+J inserts a newline (multi-line prompt)\n"
        "Enter submits — /auto-send off switches to Ctrl+Enter to send\n"
    )
    if state.INTERRUPT_ENABLED:
        banner += "press esc twice (within 2 s) to interrupt generation\n"
    banner += journal._session_note() + "\n"
    print(render.colorize(banner, "dim"))

    while True:
        try:
            user_input = lineedit.readline_prompt(
                render.colorize(f"{render.icon('user')} you> ", "user"), commands_menu
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            break
        if user_input == "/new":
            journal._session_append(state.MAIN_CONV_ID, {"next": "a new session id", "type": "cleared"})
            # A cleared conversation is a new conversation, so it gets its own log:
            # a later /resume then rebuilds one conversation, never two at once.
            journal.start_session()
            messages = [{"role": "system", "content": system_prompt}]
            journal._session_message(messages[0], state.MAIN_CONV_ID)
            print(render.colorize(f"session cleared — starting over ({journal._session_note()})\n", "dim"))
            continue
        if user_input == "/sessions":
            print(render.colorize(journal.format_sessions(), "dim"))
            continue
        if user_input == "/resume" or user_input.startswith("/resume "):
            spec = user_input[len("/resume"):].strip()
            if not spec:
                print(render.colorize(journal.format_sessions(), "dim"))
                continue
            resumed, note = journal.resume_session(spec, echo=config.RESUME_TAIL_ITEMS)
            if resumed is None:
                print(render.colorize(f"{render.icon('error')} {note}", "error"))
                continue
            messages = [{"role": "system", "content": system_prompt}] + resumed
            journal._session_prepend(messages[0], state.MAIN_CONV_ID)
            if not config.RESUME_TAIL_ITEMS:  # echo off: the note is the only word on it
                print(render.colorize(note, "dim"))
            continue
        if user_input == "/clear-screen":
            os.system("cls" if os.name == "nt" else "clear")
            continue
        if user_input == "/status":
            print(
                render.colorize(
                    format_status(messages, context_window, conv_id=state.MAIN_CONV_ID),
                    "dim",
                )
            )
            continue
        if user_input == "/tools" or user_input.startswith("/tools "):
            rest = user_input[len("/tools"):].strip()
            if not rest:
                if sys.stdin.isatty():
                    if tools_menu():
                        print(render.colorize("tools updated", "dim"))
                    else:
                        print(render.colorize("tools unchanged", "dim"))
                else:
                    print(render.colorize(format_tools(), "dim"))
            else:
                for name, toggled in toggle_tools(rest.split()):
                    if toggled == "unknown":
                        print(render.colorize(f"{render.icon('error')} unknown tool: {name}", "error"))
                    else:
                        print(render.colorize(f"{name}: {toggled}", "dim"))
            continue
        if user_input == "/auto-compact" or user_input.startswith("/auto-compact "):
            msg = compact.set_auto_compact(user_input[len("/auto-compact"):])
            if msg:
                print(render.colorize(msg, "dim"))
            else:
                print(render.colorize("usage: /auto-compact on|off|threshold N", "error"))
            continue
        if user_input == "/compact":
            ok, note = compact.compact_messages(
                messages, state.MAIN_CONV_ID, model=args.model, reason="manual",
                tools=_active_tools(True),
            )
            print(render.colorize(note, "dim" if ok else "error"))
            continue
        if user_input == "/auto-send" or user_input.startswith("/auto-send "):
            msg = set_auto_send(user_input[len("/auto-send"):])
            if msg:
                print(render.colorize(msg, "dim"))
            else:
                print(render.colorize("usage: /auto-send on|off", "error"))
            continue
        if user_input == "/help":
            print(render.colorize(format_help(), "dim"))
            continue
        messages.append(context.build_user_message(user_input))
        journal._session_message(messages[-1], state.MAIN_CONV_ID)
        run_agent(
            messages,
            args.model,
            interactive=True,
            temperature=args.temperature,
            conv_id=state.MAIN_CONV_ID,
        )


if __name__ == "__main__":
    main()


def __getattr__(name: str):
    """Resolve a name the split moved out, so `import harnless` still
    exposes the whole harness. Reads stay live (state and the moved modules
    are consulted per lookup); a write has to target the owning module —
    `harnless.state.CONTEXT_WINDOW`, not `harnless.CONTEXT_WINDOW`.
    """
    for mod in _MODULES:
        try:
            return getattr(mod, name)
        except AttributeError:
            pass
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
