# AGENTS.md

Single-file Python project: `harnless.py` — a minimal CLI agent harness that talks to a local llama-server (OpenAI-compatible API) and exposes file/bash tools. Stdlib only; no dependencies, no venv, no build step.

## Requirements

- A running LLM server for the agent to talk to. Default endpoint is `http://127.0.0.1:11434/v1/chat/completions` (`API_URL` in `harnless.py:14`), overridable via `--api-url`. Tests (`tests.py`) don't need a server.

## Commands

- Interactive: `python harnless.py` (chat REPL; `/new` clears session history, `/clear-screen` clears the terminal, `/exit` quits)
- One-shot: `python harnless.py --prompt "task"` (exits with the agent's exit code)
- Flags: `--api-url` (OpenAI-compatible endpoint; default `http://127.0.0.1:11434/v1/chat/completions`), `--api-key` (sent as `Authorization: Bearer <key>`; omit for unauthenticated local servers), `--model` (name sent in the request; llama-server ignores it), `--system-prompt` (replaces built-in), `--temperature` (sampling temperature; default `0.2`)
- MCP servers (tools only): `--mcp-config FILE` (repeatable; JSON `{"mcpServers": {...}}` or a bare `{...}`), `--mcp-stdio 'NAME:COMMAND ARGS...'`, `--mcp-http 'NAME=URL'`. `${VAR}` in a config is expanded from the environment. A server that fails to connect/list tools is skipped with a warning.
- `AGENTS.md` in the working directory (case-insensitive) is auto-loaded and appended to the system prompt in all modes.
- Interactive-mode input history is persisted to `~/.harnless_history` (last 100 entries, one per line); path overridable via the `HARNLESS_HISTORY` env var.
- Tests: `python tests.py` — stdlib unittest, no pytest. Must run from repo root; uses a temp `./_test_tmp` dir (cleaned up); exit code 1 on failure.

## Notes

- `tests.py` imports the module directly (`import harnless`).
- Tools are defined once in the `TOOLS` dict (`harnless.py:399`): each entry is `(OpenAI schema, handler)`. `OPENAI_TOOLS`/`DISPATCH` are derived from it — add new tools there. `OPENAI_TOOLS_INTERACTIVE` (same minus `exit`) is sent to the API in interactive mode; `exit` stays one-shot only.
- MCP tools are discovered at startup (`register_mcp_tools`) and registered into `MCP_TOOLS`/`MCP_DISPATCH` (not `TOOLS`). They use raw names; a name colliding with a built-in tool or an earlier MCP server is skipped (built-ins win, then first server). MCP tools are external and bypass the `safe_resolve` sandbox.
- All tool file paths are relative to CWD and must stay inside it (`safe_resolve`, `harnless.py:105`); never weaken this.
- `patch_file` supports an optional `offset`/`lines` region to disambiguate duplicate `old_string` matches; `write_file` with `offset` does line-range replace/insert (semantics covered by `tests.py` — run it after touching those functions).
