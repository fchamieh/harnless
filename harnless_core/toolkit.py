"""toolkit: the tools a model can call, and the table that maps a call onto one.

`SPECS` in toolspec.py is what the model is told exists; `DISPATCH` here is what answering it
runs. `execute_tool` is the only door: it parses the arguments the model wrote as JSON, refuses
a tool toggled off with /tools, falls through to the mcp table for names the harness did not
implement itself, caps whatever comes back, and re-raises the two control-flow signals a tool
can raise (`exit` ends a run, an interrupt ends a delegation chain) instead of stringifying
them — a swallowed interrupt is why a double-ESC once stopped only the innermost stream.

This module is where `run_agent`'s `execute=` argument comes from: the loop is *handed* an
executor, so the tool table lives beside the tools instead of above the loop, and
`tool_task` hands the run it starts the same table. It is called `toolkit` rather than `tools`
because every function in this package calls its own local list of tool specs `tools`, and a
local named after an imported module quietly breaks `tools.<name>`."""

import html
import html.parser
import json
import os
import re
import shutil
import sys
import urllib
import urllib.error
import urllib.request

from harnless_core import agent, caps, config, context, errors, journal, lineedit, mcp, paths, render, search, shell, state, toolspec

def tool_get_cwd(args: dict) -> str:
    return state.CWD

def tool_exit(args: dict):
    code = int(args.get("code", 0))
    message = args.get("message", "")
    raise errors.ExitSignal(code, message)

def _prompt_line(prompt: str):
    """Read a line from the user, returning None on EOF/cancel."""
    try:
        line = lineedit.readline_prompt(prompt, lineedit.commands_menu)
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
            code = agent.run_agent(
                messages,
                state.MODEL,
                execute=execute_tool,
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
    final = agent._closing_summary(messages, exit_note)
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

def _builtin_tool_names() -> set:
    """The names the harness itself owns, for the MCP registration to refuse."""
    return set(DISPATCH)

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
    known = agent._all_tool_names()
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
