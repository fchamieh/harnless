#!/usr/bin/env python3
"""harnless: a minimal agent harness for a local llama-server (OpenAI-compatible)."""

import argparse
import atexit
import os
import sys

from harnless_core import agent, caps, compact, config, context, errors, journal, lineedit, mcp, measure, paths, render, request, search, shell, state, toolkit, toolspec
_MODULES = (agent, caps, compact, config, context, errors, journal, lineedit, mcp, measure, paths, render, request, search, shell, state, toolkit, toolspec)  # facade: moved modules


def format_status(messages: list, context_window: int = 0, conv_id: int = None) -> str:
    """Build the /status report: context usage, API URL, tool names, MCP servers."""
    tools = agent._active_tools(True)
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


def format_help() -> str:
    """Build the /help report: list of REPL commands and file references."""
    lines = [f"{name:<16} {desc}" for name, desc in config.REPL_COMMANDS]
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


# ---------------------------------------------------------------- the CLI


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
        mcp.register_mcp_tools(mcp_clients, toolkit._builtin_tool_names())

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
            agent.run_agent(
                messages,
                args.model,
                execute=toolkit.execute_tool,
                temperature=args.temperature,
                conv_id=state.MAIN_CONV_ID,
            )
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
                render.colorize(f"{render.icon('user')} you> ", "user"), lineedit.commands_menu
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
                    if toolkit.tools_menu():
                        print(render.colorize("tools updated", "dim"))
                    else:
                        print(render.colorize("tools unchanged", "dim"))
                else:
                    print(render.colorize(toolkit.format_tools(), "dim"))
            else:
                for name, toggled in toolkit.toggle_tools(rest.split()):
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
                tools=agent._active_tools(True),
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
        agent.run_agent(
            messages,
            args.model,
            execute=toolkit.execute_tool,
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
