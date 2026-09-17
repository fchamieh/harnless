# AGENTS.md

Single-file Python project: `harnless.py` — a minimal CLI agent harness that talks to a local llama-server (OpenAI-compatible API) and exposes file/bash tools. Stdlib only; no dependencies, no venv, no build step.

## Requirements

- A running LLM server for the agent to talk to. Default endpoint is `http://127.0.0.1:11434/v1/chat/completions` (`API_URL` in `harnless.py:14`), overridable via `--api-url`. Tests (`tests.py`) don't need a server.

## Commands

- Interactive: `python harnless.py` (chat REPL; `/new` clears session history, `/clear-screen` clears the terminal, `/tools` opens an interactive tool menu (up/down move, space toggle, enter apply, esc cancel) and `/tools <name>` toggles a tool on/off, `/exit` quits)
- One-shot: `python harnless.py --prompt "task"` (exits with the agent's exit code)
- Flags: `--api-url` (OpenAI-compatible API URL; accepts either a base URL like `https://openrouter.ai/api/v1` or a full `.../chat/completions` endpoint — a base URL gets `/chat/completions` appended; default `http://127.0.0.1:11434/v1/chat/completions`), `--api-key` (sent as `Authorization: Bearer <key>`; omit for unauthenticated local servers), `--model` (name sent in the request; llama-server ignores it), `--system-prompt` (replaces built-in), `--temperature` (sampling temperature; default `0.2`), `--max-subagents` (max sub-agent nesting depth for the `task` tool; default `3`), `--context-window` (model context window size in tokens, shown as a percentage in `/status`; if omitted, probed best-effort from the API's `/models` endpoint — `context_length` (OpenAI/OpenRouter) or `meta.n_ctx` (llama.cpp) — and hidden if the probe fails)
- MCP servers (tools only): `--mcp-config FILE` (repeatable; JSON `{"mcpServers": {...}}` or a bare `{...}`), `--mcp-stdio 'NAME:COMMAND ARGS...'`, `--mcp-http 'NAME=URL'`. `${VAR}` in a config is expanded from the environment. A server that fails to connect/list tools is skipped with a warning.
- `AGENTS.md` in the working directory (case-insensitive) is auto-loaded and appended to the system prompt in all modes.
- Interactive-mode input history is persisted to `~/.harnless_history` (last 100 entries, one per line); path overridable via the `HARNLESS_HISTORY` env var.
- Tests: `python tests.py` — stdlib unittest, no pytest. Must run from repo root; uses a temp `./_test_tmp` dir (cleaned up); exit code 1 on failure.

## Notes

- `tests.py` imports the module directly (`import harnless`).
- Tools are defined once in the `TOOLS` dict (`harnless.py:399`): each entry is `(OpenAI schema, handler)`. `OPENAI_TOOLS`/`DISPATCH` are derived from it — add new tools there. `OPENAI_TOOLS_INTERACTIVE` (same minus `exit`) is sent to the API in interactive mode; `exit` stays one-shot only.
- Sub-agents: the built-in `task` tool (`tool_task`) delegates a self-contained task to a nested `run_agent` with a fresh context (sub-agent system prompt + AGENTS.md, full one-shot tool set). It returns the sub-agent's exit code + final summary as the tool result. Depth is tracked in `_AGENT_DEPTH` (set at `run_agent` entry) and capped at `MAX_SUBAGENT_DEPTH` (`--max-subagents`, default 3); sub-agents can delegate further until the cap. Sub-agent output is printed live, indented 2 spaces per level via `OUTPUT_INDENT`. `run_agent` returns the `exit` tool's code instead of calling `sys.exit` (the one-shot path in `main` does `sys.exit(run_agent(...))`), which is what lets a sub-agent's `exit` end only the sub-loop.
- MCP tools are discovered at startup (`register_mcp_tools`) and registered into `MCP_TOOLS`/`MCP_DISPATCH` (not `TOOLS`). They use raw names; a name colliding with a built-in tool or an earlier MCP server is skipped (built-ins win, then first server). MCP tools are external and bypass the `safe_resolve` sandbox.
- All tool file paths are relative to CWD and must stay inside it (`safe_resolve`, `harnless.py:105`); never weaken this.
- `patch_file` supports an optional `offset`/`lines` region to disambiguate duplicate `old_string` matches; `write_file` with `offset` does line-range replace/insert (semantics covered by `tests.py` — run it after touching those functions).
- Assistant output is styled by `MarkdownRenderer` (`harnless.py`), which supports headings, lists, blockquotes, fenced code, inline emphasis/code/links, and pipe tables (box-drawing borders, `:--`/`:-:`/`--:` alignment, inline styling in cells, terminal-width clamping). Tables are buffered whole before printing (a `|` line is held one line to test for a delimiter row); with colors disabled the raw Markdown passes through unchanged.
