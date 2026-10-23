#!/usr/bin/env python3
"""refactor/codemod.py — throwaway tooling for the harnless.py -> harnless_core split.

Not part of the shipped harness; it exists so the split is a mechanical, reviewable
transformation instead of thousands of hand edits.

  extract <stage>   move that stage's top-level statements out of harnless.py into
                    harnless_core/<module>.py, copying the source text verbatim
                    (comments and formatting travel with the code);
  link              rewrite every reference in every file according to the registry
                    of where each name now lives, and regenerate each file's imports.

The reference conventions `link` enforces are what keeps the suite's monkeypatching
alive after the split:

  * values in config.py  -> imported by name; nothing ever rebinds them;
  * mutable state        -> always `state.NAME`, through the state module object, so a
                            flag in main(), a slash command or a test is seen by callers;
  * behaviour            -> always called through its module (`journal.tool_sessions`),
                            never imported by name: patching the owning module still
                            intercepts every call site, exactly as one flat namespace did.
"""

import argparse
import ast
import json
import pathlib
import re
import sys
import tokenize

SRC = pathlib.Path("harnless.py")
TESTS = pathlib.Path("tests.py")
PKG_DIR = pathlib.Path("harnless_core")
REGISTRY = pathlib.Path("refactor/registry.json")

# Constants the harness or the test suite rebinds at runtime (main()'s flags, the
# slash commands, and every `h.NAME = ...` in tests.py). These must live in state.py
# and be reached as `state.NAME` — a name imported by name would keep a stale copy and
# the patch would silently do nothing. Derived once, before the split, from the flat
# module plus tests.py; kept here so the rule stays mechanical.
MUTABLE_CONSTANTS = {
    "AGENT_LOG_ID", "API_KEY", "API_URL", "AUTO_COMPACT", "AUTO_SEND",
    "COLORS_ENABLED", "COMPACT_DROP_REASONING", "COMPACT_KEEP_TOKENS_PCT",
    "COMPACT_MAX_ATTEMPTS", "COMPACT_MIN_MESSAGES", "COMPACT_PRUNE_TOOL_RESULTS",
    "COMPACT_STATS", "COMPACT_SUMMARY_TRIMS", "COMPACT_THRESHOLD_PCT",
    "CONTEXT_PROGRESS_DELAY", "CONTEXT_WINDOW", "CWD", "DISABLED_TOOLS",
    "EMOJI_ENABLED", "EXIT_NOTE_BY_CONV", "HISTORY", "HISTORY_FILE", "HISTORY_MAX",
    "INTERRUPT_ENABLED", "MAIN_CONV_ID", "MAX_SUBAGENT_DEPTH", "MCP_CLIENTS",
    "MCP_DISPATCH", "MCP_TOOLS", "MEMORY_GLOBAL_FILE", "MEMORY_PROJECT_FILE",
    "MODEL", "OUTPUT_INDENT", "PENDING_MCP", "SESSION_DIR", "SESSION_FILES",
    "SESSION_ID", "SESSION_LOG", "SESSION_MODE", "SESSION_MSG_SEQ", "SESSION_SEQ",
    "SESSION_WARNED", "SHELL_PREFERRED", "SUBAGENT_CHILDREN", "SUBAGENT_STEP_LIMIT",
    "TEMPERATURE", "TODO_FILE", "TODO_ITEMS", "TODO_LAST_INJECTED", "TODO_NEXT_ID",
    "USAGE_BY_CONV", "VISION_ENABLED", "_AGENT_DEPTH", "_NEXT_CONV_ID",
    "_SHELL_CACHE",
}

# Values that cannot leave harnless.py yet: they are built from tool handlers that
# still live there (the registry) or from a function call at import time.
KEEP_IN_HARNLESS = {
    "TOOLS", "OPENAI_TOOLS", "OPENAI_TOOLS_INTERACTIVE", "DISPATCH",
    "REPL_COMMANDS", "_TASKKILL",
}

# Behaviour to move per stage, in file-order-independent terms. Stages run bottom-up:
# a package module may only reference names in a lower package module (harnless.py
# imports the package, so the package may never import harnless).
# Statements that ride along with a mechanical (constant) stage.
STAGES_EXTRA = {"state": ["_stdout_can_encode_emoji"]}

STAGES = {
    "config": {"config": None},          # None = every constant not in MUTABLE_CONSTANTS
    "state": {"state": None},            # None = every constant in MUTABLE_CONSTANTS
    "errors": {"errors": [
        "ExitSignal", "ContextOverflow", "_context_overflow_reason",
    ]},
    "caps": {"caps": [
        "_bool_arg", "_limit_arg", "_clamp_int", "_cap_note", "_truncate",
        "_cap_count", "_clip_line", "_content_chars",
    ]},
    "paths": {"paths": [
        "_cwd_roots", "_inside_cwd", "_rel_to_cwd", "safe_resolve", "_case_flags",
        "_glob_to_regex", "_read_lines",
    ]},
    "render": {"render": [
        "set_color_enabled", "set_emoji_enabled", "icon", "color_enabled",
        "colorize", "display_width", "terminal_width", "_is_table_sep",
        "_split_table_row", "_table_alignments", "_truncate_visible",
        "MarkdownRenderer", "print_assistant", "print_user", "print_reasoning",
        "print_tool_call", "print_tool_result", "_format_tokens", "_format_elapsed",
    ]},
    "journal": {"journal": [
        "re:^_session_", "re:^log_", "start_session", "replay_session_tail",
        "resume_session", "_resume_target", "format_sessions", "_record_text",
        "_elision_note", "_seq_arg", "tool_sessions", "_transcript_note",
        "_transcript_hint", "_session_span",
    ]},
    "compact": {"compact": [
        "re:^_compact", "re:^_fit_", "re:^_prune_", "_system_prefix",
        "_msg_tokens", "_turn_tokens", "_context_tokens_used", "_token_scale",
        "compact_messages",
        "_compact_if_needed", "_recover_overflow", "set_auto_compact", "format_compaction",
        "estimate_context_chars", "estimate_context_tokens",
    ]},
    "search": {"search": [
        "re:^_gitignore", "re:^_ignore", "_is_ignored", "_iter_files", "tool_grep",
        "tool_glob", "tool_list_dir", "tool_patch_file", "tool_delete_file",
        "tool_move_file", "tool_copy_file",
    ]},
    "lineedit": {"lineedit": [
        "_windows_ctrl_held", "_windows_key_token", "_iter_keys_windows",
        "_iter_keys_windows_poll", "_parse_csi_seq", "_set_raw_input", "_iter_keys_posix",
        "_iter_keys_posix_poll", "_iter_keys_interrupt", "check_double_esc",
        "_edit_cur", "_edit_rows", "_edit_phys_pos", "_edit_line", "readline_prompt",
        "InterruptWatcher", "ContextProgress", "_UploadProgress", "_response_socket",
    ]},
    "mcp": {"mcp": [
        "MCPError", "MCPClient", "re:^_mcp", "_expand_env", "load_mcp_config",
        "re:^_load_mcp", "re:^_parse_mcp", "_register_mcp_client", "register_mcp_tools",
        "_enable_pending_mcp", "_mcp_server_tools",
    ]},
    "request": {"request": [
        "normalize_api_url", "_headers", "probe_context_window", "_progress_opener",
        "_open_request", "chat", "_request_body", "_build_request", "parse_sse_line",
        "stream_chat", "accumulate_delta", "stream_once", "_record_usage",
    ]},
    "context": {"context": [
        "re:^_todo", "tool_todo", "re:^_memory", "tool_memory", "load_memory",
        "_context_additions", "get_system_prompt", "load_agents_md", "re:^_read_local",
        "_read_cwd_relative", "_fetch_remote", "_resolve_uri", "expand_file_refs",
        "build_user_message", "re:^_history",
    ]},
    "agent": {"agent": [
        "TOOLS", "OPENAI_TOOLS", "OPENAI_TOOLS_INTERACTIVE", "DISPATCH", "REPL_COMMANDS",
        "tool_get_cwd", "tool_exit", "_prompt_line", "_print_question", "tool_ask_user",
        "_resolve_shell", "_taskkill_cmd", "_kill_tree", "_drain_killed", "_shell_timeout",
        "tool_run_shell", "tool_mkdir", "tool_read_file", "tool_write_file",
        "_html_to_text", "tool_fetch_url", "_closing_summary", "tool_task", "execute_tool",
        "_active_tools", "_all_tool_names", "format_tools", "toggle_tools", "tools_menu",
        "format_status", "set_auto_send", "commands_menu", "format_help",
        "run_agent", "_agent_loop",
    ]},
}

DOCSTRINGS = {
    "config": "Fixed tunables: every knob the harness ships with, plus the fixed text it is\nbuilt from (tool descriptions, notes and prompts are model-facing copy).\n\nNothing here is ever rebound at runtime. A knob a flag, a slash command or a test can\nchange lives in state.py instead — see harnless_core/state.py.",
    "state": "Mutable runtime state: one home for everything that changes after import.\n\nAlways reached through this module object (`state.CONTEXT_WINDOW`), never copied with\n`from ... import` — a copy would silently stop seeing the changes main()'s flags, the\nslash commands and the test suite make.",
    "errors": "The harness's control-flow signals, and how a server's 'context is full'\nrejection is recognised.\n\nThese are signals rather than error strings: execute_tool re-raises them so the agent\nloop can act on them instead of feeding them back to the model as a tool failure.",
    "caps": "Output caps: the ceiling on what a tool may hand back.\n\nTwo layers: a tool's `limit`-style argument is model-facing (the agent can ask for\nless), the constants in harnless_core/config.py are the ceiling it cannot exceed, and\nthese helpers apply both while keeping the truncation note the agent needs to know it\nhappened.",
    "paths": "Working-directory containment: turning a model-supplied relative path into an\nabsolute one guaranteed to stay inside CWD, and back again.\n\nCWD itself may be a symlink/junction, which is why the checks run against both forms of\nit (`_cwd_roots`).",
    "render": "Terminal rendering: the colour/icon tables and their switches, the Markdown\nrenderer, the width helpers, and the lines the agent loop prints.\n\nThe printers are shared with the journal replay on purpose (see harnless_core/journal.py\nreplay_session_tail) so a resumed session replays exactly what it showed live.",
    "compact": "Context compaction: measuring what a request costs, cutting the oldest turns,\nand writing the handoff note the model is left with.\n\nThe tail (the turns kept verbatim) is chosen in the server's own token units when the\nserver reports usage, so a cut means what the server says it means. Everything replaced\nstays in the journal (see harnless_core/journal.py) and the `compact` record names the\nseqs it replaced.",
    "search": "The file search tools: CWD-contained walking, .gitignore honouring, and the\ngrep/glob/list_dir/patch_file/read-side helpers they share.\n\nA model-supplied pattern never escapes the working directory, and ignored files are\nskipped unless the caller asks for them.",
    "lineedit": "Reading the keyboard: the raw-mode key iterators (Windows and POSIX), the\ncursor-editing line editor behind `readline_prompt`, and the console-side watchers the\nagent loop uses (ESC handling, the context-usage meter, upload progress).",
    "mcp": "The MCP client: JSON-RPC over stdio or HTTP, tool discovery, and how a server's\ntools join the harness's own tool tables.",
    "request": "Talking to the model server: request building, non-streaming and streaming\ncompletions, SSE parsing, progress reporting and the context-window probe.",
    "context": "What rides along with every request: the system prompt, AGENTS.md, the memory\nblock, the todo list, `file://`/`http(s)://` references in the user's message, and the\nprompt history file.",
    "agent": "The agent itself: the tool tables and their dispatch, tool execution, sub-agent\nruns, and the conversation loop.",
    "journal": "Session journal: every conversation recorded as it happens, and the read side\nthat resumes one.\n\nEach message the harness replays to the API is appended as one JSON line, so compaction\ncan name the turns it replaced and --resume / /resume can rebuild a conversation.\n<id>.jsonl holds the top-level run; each sub-agent run gets <id>.<chain>.jsonl.",
}

PKG_INIT = '''"""harnless_core: the harness, split into layers.

Layer order — a module may only reference names from modules listed above it:

    config, state                 fixed tunables / mutable runtime state
    errors, caps, paths, render   shared low-level behaviour
    journal                       session recording and resume
    (tools, client, agent loop    — later stages of the split)

harnless.py stays the entry point and the facade: it imports this package and
re-exports it, so `import harnless` still resolves every harness name.
"""
'''


# ---------------------------------------------------------------- source blocks
def read_source() -> str:
    return SRC.read_text(encoding="utf-8")


def write_source(text: str):
    SRC.write_text(collapse_blanks(text), encoding="utf-8", newline="\n")


def collapse_blanks(text: str) -> str:
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"^(\s*\n)+", "", text)
    return re.sub(r"\n+\Z", "\n", text)


def constants_of(src: str):
    """Top-level constant names (UPPER_CASE or _Upper), in definition order."""
    out = []
    for node in ast.parse(src).body:
        targets = list(node.targets) if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for t in targets:
            if isinstance(t, ast.Name) and re.match(r"^_?[A-Z][A-Z0-9_]*$", t.id):
                out.append(t.id)
    return out


def top_level_blocks(src: str):
    """[(names, start0, end0)] for every top-level statement, comments attached."""
    lines = src.split("\n")
    blocks, prev_end = [], 0
    for node in ast.parse(src).body:
        start = node.lineno - 1
        i = start - 1
        while i >= prev_end and lines[i].strip().startswith("#"):
            i -= 1
        start = max(i + 1, prev_end)
        end = node.end_lineno
        names = []
        if isinstance(node, ast.Assign):
            for t in node.targets:
                names += [x.id for x in ast.walk(t) if isinstance(x, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        blocks.append((tuple(names), start, end))
        prev_end = end
    return lines, blocks


def defined_names(src: str):
    out = set()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign):
            out |= {x.id for x in ast.walk(node.targets[0]) if isinstance(x, ast.Name)}
            for t in node.targets:
                out |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
    return out


# ---------------------------------------------------------------- registry
def refresh_registry():
    reg = {}
    for path in sorted(PKG_DIR.glob("*.py")):
        for name in defined_names(path.read_text(encoding="utf-8")):
            reg[name] = path.stem
    for name in defined_names(read_source()):
        reg[name] = "harnless"
    REGISTRY.write_text(json.dumps(reg, indent=1, sort_keys=True), encoding="utf-8")
    return reg


# ---------------------------------------------------------------- extract
# Renames that must happen *before* the split: these locals/parameters are named
# `state`, and a function that binds a local `state` cannot read the state module.
PRE_RENAMES = [
    ("def check_double_esc(state: dict, token, now: float) -> bool:",
     "def check_double_esc(presses: dict, token, now: float) -> bool:"),
    ('"""Track ESC presses in `state` ({"last_esc": float}).',
     '"""Track ESC presses in `presses` ({"last_esc": float}).'),
    ('        if now - state["last_esc"] < DOUBLE_ESC_WINDOW:',
     '        if now - presses["last_esc"] < DOUBLE_ESC_WINDOW:'),
    ('        state["last_esc"] = now', '        presses["last_esc"] = now'),
    ('        state["last_esc"] = 0.0', '        presses["last_esc"] = 0.0'),
    ('        state = {"last_esc": 0.0}', '        presses = {"last_esc": 0.0}'),
    ("            if check_double_esc(state, token, time.monotonic()):",
     "            if check_double_esc(presses, token, time.monotonic()):"),
    ("                for name, state in toggle_tools(rest.split()):",
     "                for name, toggled in toggle_tools(rest.split()):"),
    ('                    if state == "unknown":', '                    if toggled == "unknown":'),
    ('                        print(colorize(f"{name}: {state}", "dim"))',
     '                        print(colorize(f"{name}: {toggled}", "dim"))'),
    ('    state = json.dumps(TODO_ITEMS, sort_keys=True)',
     '    signature = json.dumps(TODO_ITEMS, sort_keys=True)'),
    ("    if state == TODO_LAST_INJECTED:", "    if signature == TODO_LAST_INJECTED:"),
    ("    TODO_LAST_INJECTED = state", "    TODO_LAST_INJECTED = signature"),
    # The line editor's nested repaint helper is named `render`, which would shadow the
    # harnless_core.render module alias used by every print/width call in that function.
    ("    def render(commit=False):", "    def redraw(commit=False):"),
    ("        render(commit=True)", "        redraw(commit=True)"),
    ("    render()", "    redraw()"),
    # _session_files() built its own list named `paths`, shadowing the paths module.
    ("    paths = []", "    logs = []"),
    ("            paths.append(path)", "            logs.append(path)"),
    ("    paths.sort(key=_session_mtime_key, reverse=True)", "    logs.sort(key=_session_mtime_key, reverse=True)"),
    ("    return paths", "    return logs"),
]


def cmd_pre():
    src = read_source()
    for old, new in PRE_RENAMES:
        if old not in src:
            print(f"  skipped (already renamed): {old.strip()[:60]}")
            continue
        src = src.replace(old, new)
    write_source(src)
    print(f"  {len(PRE_RENAMES)} renames applied: `state` is now free as the module alias")


def expand(names, by_name):
    """Resolve a manifest entry: a plain statement name, or 're:<pattern>' matching
    every top-level name that fits (so a layer is described by its naming convention)."""
    out = []
    for entry in names:
        if isinstance(entry, str) and entry.startswith("re:"):
            out += sorted(n for n in by_name if re.search(entry[3:], n))
        else:
            out.append(entry)
    return out


def cmd_extract(stage: str):
    manifest = STAGES[stage]
    src = read_source()
    lines, blocks = top_level_blocks(src)
    by_name, dupes = {}, []
    for names, start, end in blocks:
        for n in names:
            if n in by_name:
                dupes.append(n)
            by_name.setdefault(n, (start, end))
    if dupes:
        sys.exit(f"stage {stage}: names defined twice at top level: {sorted(set(dupes))}")

    resolved = {}
    for module, names in manifest.items():
        if names is None:                      # constant stages: derive mechanically
            consts = [c for c in constants_of(src) if c not in KEEP_IN_HARNLESS]
            if module == "state":
                names = [c for c in consts if c in MUTABLE_CONSTANTS]
            else:
                names = [c for c in consts if c not in MUTABLE_CONSTANTS]
            names = names + STAGES_EXTRA.get(module, [])
        names = expand(names, by_name)
        missing = [n for n in names if n not in by_name]
        if missing:
            sys.exit(f"stage {stage}: {module} names not found in harnless.py: {missing}")
        resolved[module] = names

    for module, names in resolved.items():
        picked, last_end = [], -1
        for n in sorted(names, key=lambda x: by_name[x][0]):
            start, end = by_name[n]
            if start >= last_end:
                picked.append((start, end))
                last_end = end
        body = "\n\n".join("\n".join(lines[s:e]).rstrip() for s, e in picked)
        PKG_DIR.mkdir(exist_ok=True)
        doc = DOCSTRINGS.get(module, f"{module}: part of harnless (see harnless.py).")
        text = f'"""{doc}"""\n\n# __IMPORTS__\n\n' + body + "\n"
        path = PKG_DIR / f"{module}.py"
        path.write_text(collapse_blanks(text), encoding="utf-8", newline="\n")
        print(f"  {path}: {len(picked)} statements, {len(text.splitlines())} lines")

    removed = set()
    for names in resolved.values():
        removed |= {by_name[n] for n in names}
    out = lines[:]
    for start, end in sorted(removed, reverse=True):
        del out[start:end]
    write_source("\n".join(out))
    reg = refresh_registry()
    print(f"  harnless.py: {len(lines)} -> {len(read_source().splitlines())} lines")
    print(f"  {REGISTRY}: {len(reg)} names mapped")


# ---------------------------------------------------------------- link
def cmd_link():
    registry = refresh_registry()
    # harnless.py imports every core module statically: it is the facade, and a
    # frozen build (Nuitka) can only bundle what it can see in an import statement.
    mods = sorted({owner for owner in registry.values() if owner != "harnless"})
    files = [SRC] + sorted(p for p in PKG_DIR.glob("*.py") if p.name != "__init__.py")
    for path in files:
        relink(path, registry, mods if path == SRC else [])
    cmd_facade()  # keep _MODULES / __getattr__ in step with what has moved so far


def relink(path: pathlib.Path, registry: dict, extra_mods=()):
    src = path.read_text(encoding="utf-8")
    module = path.stem
    local = defined_names(src)
    tree = ast.parse(src)
    pinned, import_rows = pinned_positions(tree)
    edits, core_mods, harnless_refs = scan(src, registry, module, local, pinned, import_rows)
    if harnless_refs and path.parent == PKG_DIR:
        sys.exit(f"{path}: uses {sorted(harnless_refs)} — names still living in "
                 "harnless.py. A package module may not import the entry point: "
                 "move those names down first.")
    core_mods |= {(m, None) for m in extra_mods}
    aliases = {m for m, _ in core_mods} | {o for o in registry.values() if o != "harnless"}
    clashes = local_bindings(src, tree) & aliases
    if clashes:
        sys.exit(f"{path}: binds {sorted(clashes)} inside a function, but those are core "
                 "module aliases — the rewrite would read the local value instead of the "
                 "module. Rename the local (see cmd_pre / PRE_RENAMES).")
    src = apply_edits(src, edits)
    src = fix_global_lines(src, registry)
    src = write_imports(src, core_mods, ast.parse(src))
    path.write_text(collapse_blanks(src), encoding="utf-8", newline="\n")
    print(f"  {path}: {len(edits)} reference rewrites, "
          f"{len(core_mods)} core imports")


def local_bindings(src: str, tree) -> set:
    """Every name bound *inside* a function or class body (parameters, nested defs,
    assignments, loop/comprehension targets, with/except bindings, import aliases).

    A core module's alias may not appear in this set: `def render(...)` inside a
    function makes `render.colorize(...)` in that function an AttributeError."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                out |= {x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)}
                out |= {x.arg for x in (a.vararg, a.kwarg) if x}
        elif isinstance(node, ast.Assign):
            out |= {x.id for x in node.targets if isinstance(x, ast.Name)}
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)) and isinstance(
                getattr(node, "target", None), ast.Name):
            out.add(node.target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)) and isinstance(
                node.target, ast.Name):
            out.add(node.target.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            out |= {i.optional_vars.id for i in node.items if isinstance(i.optional_vars, ast.Name)}
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in node.names}
    for node in tree.body:  # module-level bindings are not "local"
        if isinstance(node, ast.Assign):
            out -= {x.id for x in node.targets if isinstance(x, ast.Name)}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.discard(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out -= {(a.asname or a.name).split(".")[0] for a in node.names}
    return out


def pinned_positions(tree):
    """(row, col, name) where a NAME token must stay bare — parameters and keyword
    arguments (prefixing one would be a syntax error) — plus the line ranges of
    import statements, whose names must never be rewritten."""
    out, import_rows = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_rows.update(range(node.lineno, node.end_lineno + 1))
        elif isinstance(node, ast.keyword) and node.arg:
            out.add((node.lineno, node.col_offset, node.arg))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
                        + [x for x in (a.vararg, a.kwarg) if x]):
                out.add((arg.lineno, arg.col_offset, arg.arg))
    return out, import_rows


def scan(src: str, registry: dict, module: str, local: set, pinned: set, import_rows):
    """One tokenize pass: name rewrites to apply, plus the core modules needed.

    Skipped on purpose: attribute names (NAME after a '.'), names already defined in
    this file, parameters/keyword arguments, and names inside a global/nonlocal
    declaration (fixed separately — `global state.X` is not legal Python).
    """
    edits, core_mods, harnless_refs = [], set(), set()
    prev = None
    in_decl = False
    for tok in tokenize.generate_tokens(iter(src.splitlines(True)).__next__):
        t, s = tok.type, tok.string
        if t in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT,
                 tokenize.ENDMARKER):
            continue
        if t == tokenize.NEWLINE:
            in_decl = False
            prev = None
            continue
        if t == tokenize.NAME:
            after_dot = prev is not None and prev[0] == tokenize.OP and prev[1] == "."
            owner = registry.get(s)
            if in_decl:
                pass
            elif (not after_dot and tok.start[0] not in import_rows
                  and (tok.start[0], tok.start[1], s) not in pinned
                  and owner and owner != module and s not in local):
                if owner == "harnless":
                    harnless_refs.add(s)
                if owner == "config":
                    edits.append((tok.start[0], tok.start[1], len(s), "config." + s))
                    core_mods.add(("config", None))
                elif owner == "state":
                    edits.append((tok.start[0], tok.start[1], len(s), "state." + s))
                    core_mods.add(("state", None))
                else:
                    edits.append((tok.start[0], tok.start[1], len(s), owner + "." + s))
                    core_mods.add((owner, None))
            in_decl = in_decl or s in ("global", "nonlocal")
        prev = (t, s)
    return edits, core_mods, harnless_refs


def apply_edits(src: str, edits) -> str:
    rows = src.split("\n")
    by_row = {}
    for row, col, old_len, new in edits:
        by_row.setdefault(row, []).append((col, new, old_len))
    for row, items in by_row.items():
        line = rows[row - 1]
        for col, new, old_len in sorted(items, key=lambda x: -x[0]):
            line = line[:col] + new + line[col + old_len:]
        rows[row - 1] = line
    return "\n".join(rows)


def fix_global_lines(src: str, registry: dict) -> str:
    """Drop moved names from `global NAME, ...` declarations (they are no longer
    module globals of this file); delete the declaration when nothing is left."""
    moved = {n for n, owner in registry.items() if owner in ("state", "config")}
    out, dropped = [], 0
    for line in src.split("\n"):
        m = re.match(r"^(\s*)(global|nonlocal)\s+(.*)$", line)
        if m:
            names = [x.strip() for x in m.group(3).split(",")]
            keep = [n for n in names if n not in moved]
            if len(keep) != len(names):
                dropped += len(names) - len(keep)
                if not keep:
                    continue
                line = f"{m.group(1)}{m.group(2)} {', '.join(keep)}"
        out.append(line)
    if dropped:
        print(f"  global declarations trimmed: {dropped}")
    return "\n".join(out)


def write_imports(src: str, core_mods, tree) -> str:
    """Replace the # __IMPORTS__ marker with the imports this file actually needs.

    harnless_core names are always imported as modules (`config.X`, `journal.fn()`),
    never by name: a by-name import is a copy, and a copy stops seeing the changes
    main()'s flags, the slash commands and the tests make. An import is derived from
    what the file *uses* (`mod.attr`), so re-running this is idempotent and self-healing.
    """
    core_names = {p.stem for p in PKG_DIR.glob("*.py")} - {"__init__"}
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used.add(node.value.id)
    local_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            local_imports |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            local_imports |= {(a.asname or a.name) for a in node.names}
    mods = sorted((used & core_names) | {m for m, n in core_mods if n is None})
    stdlib = sorted((used & STDLIB_ROOTS) - local_imports)
    named = {}
    for m, n in core_mods:
        if n:
            named.setdefault(m, set()).add(n)
    lines = []
    if stdlib:
        lines.append("import " + "\nimport ".join(stdlib))
    if mods:
        lines.append("from harnless_core import " + ", ".join(mods))
    for m, ns in sorted(named.items()):
        lines.append(f"from harnless_core.{m} import " + ", ".join(sorted(ns)))
    body = "\n".join(lines)
    if "# __IMPORTS__" in src:
        return src.replace("# __IMPORTS__", body if body else "").replace("\n\n\n\n", "\n\n")
    # harnless.py keeps its own import block: drop the core imports a previous run
    # added (except the facade's own line) and add whatever it now needs
    rows = [r for r in src.split("\n")
            if not (r.startswith("from harnless_core") and "# facade" not in r)]
    src = "\n".join(rows)
    tree = ast.parse(src)
    insert_at = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            insert_at = max(insert_at, node.end_lineno)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            insert_at = max(insert_at, node.end_lineno)
    rows = src.split("\n")
    if body:
        rows[insert_at:insert_at] = [""] + lines
    return "\n".join(rows)


def needed_stdlib(src: str, tree) -> list:
    """Stdlib modules this file uses but does not import itself anywhere."""
    local_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                local_imports.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                local_imports.add(a.asname or a.name)
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used.add(node.value.id)
    out = []
    for name in sorted(used):
        if name in local_imports or name in ("harnless_core",):
            continue
        if name in STDLIB_ROOTS:
            out.append(name)
    return out


STDLIB_ROOTS = {
    "argparse", "atexit", "base64", "ctypes", "html", "http", "json", "os", "queue",
    "re", "shutil", "signal", "subprocess", "sys", "threading", "time", "unicodedata",
    "urllib",
}


def cmd_facade():
    """Keep `import harnless` exposing the whole harness after the split (PEP 562).

    `link` already gives harnless.py a static import of every core module; this adds
    the tuple the forwarding walks and the forwarding function. Both are re-generated
    from the registry, so the stage list is never maintained by hand.
    """
    registry = refresh_registry()
    mods = sorted({m for m in registry.values() if m != "harnless"})
    src = read_source()
    rows = [r for r in src.split("\n") if not r.endswith("# facade: moved modules")]
    src = "\n".join(rows)
    at = 0
    for node in ast.parse(src).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            at = max(at, node.end_lineno)
    rows = src.split("\n")
    rows[at:at] = ["_MODULES = (" + ", ".join(mods) + ")  # facade: moved modules"]
    src = "\n".join(rows)
    if "def __getattr__" not in src:
        src = src.rstrip("\n") + (
            "\n\n\ndef __getattr__(name: str):\n"
            '    """Resolve a name the split moved out, so `import harnless` still\n'
            "    exposes the whole harness. Reads stay live (state and the moved modules\n"
            "    are consulted per lookup); a write has to target the owning module —\n"
            "    `harnless.state.CONTEXT_WINDOW`, not `harnless.CONTEXT_WINDOW`.\n"
            "    \"\"\"\n"
            "    for mod in _MODULES:\n"
            "        try:\n"
            "            return getattr(mod, name)\n"
            "        except AttributeError:\n"
            "            pass\n"
            '    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")\n'
        )
    write_source(src)
    print(f"  facade forwarding to: {', '.join(mods)}")


def non_code_spans(src: str):
    """(start, end) character ranges covered by strings and comments — the tests
    rewrite must never touch text."""
    spans = []
    for tok in tokenize.generate_tokens(iter(src.splitlines(True)).__next__):
        if tok.type in (tokenize.STRING, tokenize.COMMENT, tokenize.FSTRING_MIDDLE):
            spans.append((tok.start, tok.end))
    starts = [0]
    for line in src.split("\n"):
        starts.append(starts[-1] + len(line) + 1)
    return [(starts[r - 1] + c, starts[rr - 1] + cc) for (r, c), (rr, cc) in spans]


def inside(pos: int, spans) -> bool:
    return any(a <= pos < b for a, b in spans)


def cmd_tests():
    """Point tests.py at where each name now lives.

    Every patch form the suite uses is handled: `h.NAME = v`, `old = h.NAME`,
    `setattr(h, "NAME", v)`, `addCleanup(setattr, h, "NAME", v)` and
    `mock.patch.object(h, "NAME", v)`. Reads are retargeted too, so a test never
    reaches a moved name through the facade by accident.
    """
    registry = refresh_registry()
    src = TESTS.read_text(encoding="utf-8")
    spans = non_code_spans(src)
    edits = []
    for m in re.finditer(r'(?<![\w.])h\.([A-Za-z_]\w*)', src):
        owner = registry.get(m.group(1))
        if not owner or owner == "harnless" or inside(m.start(), spans):
            continue
        edits.append((m.start(), len(m.group(0)), f"h.{owner}.{m.group(1)}"))
    for m in re.finditer(r'(?<![\w.])h,\s*"([A-Za-z_]\w*)"', src):
        owner = registry.get(m.group(1))
        if not owner or owner == "harnless" or inside(m.start(), spans):
            continue
        edits.append((m.start(), 1, f"h.{owner}"))
    for pos, old_len, new in sorted(edits, key=lambda e: -e[0]):
        src = src[:pos] + new + src[pos + old_len:]
    TESTS.write_text(src, encoding="utf-8", newline="\n")
    print(f"  tests.py: {len(edits)} references retargeted")


def cmd_shadow():
    """Report local bindings that shadow an imported name (state, or a config value).

    A local `state = {}` inside a function makes `state.CONTEXT_WINDOW` in that same
    function an UnboundLocalError; a local named like a config value silently hides it.
    """
    registry = refresh_registry()
    imported = {n for n, owner in registry.items() if owner == "config"}
    aliases = {"state"}
    bad = 0
    for path in [SRC] + sorted(PKG_DIR.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = set()
            a = node.args
            names |= {x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    names |= {x.id for x in sub.targets if isinstance(x, ast.Name)}
                elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)) and isinstance(sub.target, ast.Name):
                    names.add(sub.target.id)
                elif isinstance(sub, (ast.For, ast.AsyncFor)) and isinstance(sub.target, ast.Name):
                    names.add(sub.target.id)
                elif isinstance(sub, ast.comprehension) and isinstance(sub.target, ast.Name):
                    names.add(sub.target.id)
                elif isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
                    names.add(sub.target.id)
                elif isinstance(sub, (ast.With, ast.AsyncWith)):
                    names |= {i.optional_vars.id for i in sub.items
                              if isinstance(i.optional_vars, ast.Name)}
                elif isinstance(sub, ast.ExceptHandler) and sub.name:
                    names.add(sub.name)
            hits = sorted(names & (imported | aliases))
            if hits:
                bad += 1
                print(f"  {path}:{node.lineno} {node.name}() shadows {hits}")
    print(f"shadowing functions: {bad}")


def cmd_cycle():
    """Everything a move needs: retarget the code, retarget the tests, audit shadows."""
    cmd_link()
    cmd_tests()
    cmd_shadow()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pre", "extract", "link", "facade", "tests", "shadow",
                                    "cycle", "audit"])
    ap.add_argument("stage", nargs="?", default="")
    a = ap.parse_args()
    if a.cmd == "pre":
        print("pre: freeing the `state` name")
        cmd_pre()
    elif a.cmd == "extract":
        if a.stage not in STAGES:
            sys.exit(f"unknown stage {a.stage!r}; known: {', '.join(STAGES)}")
        print(f"extract {a.stage}:")
        cmd_extract(a.stage)
    elif a.cmd == "link":
        print("link:")
        cmd_link()
    elif a.cmd == "facade":
        print("facade:")
        cmd_facade()
    elif a.cmd == "tests":
        print("tests:")
        cmd_tests()
    elif a.cmd == "shadow":
        print("shadow audit:")
        cmd_shadow()
    elif a.cmd == "cycle":
        cmd_cycle()
    else:
        registry = refresh_registry()
        moved = {n for n, m in registry.items() if m != "harnless"}
        tests = TESTS.read_text(encoding="utf-8")
        hits = sorted({m.group(1) for m in re.finditer(r"\bh\.([A-Za-z_]\w*)\s*=[^=]", tests)})
        bad = [n for n in hits if n in moved]
        print(f"{len(registry)} names, {len(moved)} moved out")
        print(f"tests write {len(hits)} harness attributes; still targeting the facade: {bad}")


if __name__ == "__main__":
    main()
