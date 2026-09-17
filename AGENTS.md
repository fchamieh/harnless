# AGENTS.md

Single-file Python project: `harnless.py` — a minimal CLI agent harness that talks to a local llama-server (OpenAI-compatible API) and exposes file/bash tools. Stdlib only; no dependencies, no venv, no build step.

## Requirements

- A running LLM server at `http://127.0.0.1:11434/v1/chat/completions` (hardcoded `API_URL` in `harness.py:13`). Nothing works without it.

## Commands

- Interactive: `python harness.py` (chat REPL; `/new` clears session history, `/clear-screen` clears the terminal, `/exit` quits)
- One-shot: `python harness.py --prompt "task"` (exits with the agent's exit code)
- Flags: `--api-url` (OpenAI-compatible endpoint; default `http://127.0.0.1:11434/v1/chat/completions`), `--api-key` (sent as `Authorization: Bearer <key>`; omit for unauthenticated local servers), `--model` (name sent in the request; llama-server ignores it), `--system-prompt` (replaces built-in)
- `AGENTS.md` in the working directory (case-insensitive) is auto-loaded and appended to the system prompt in all modes.
- Tests: `python tests.py` — stdlib unittest, no pytest. Must run from repo root; uses a temp `./_test_tmp` dir (cleaned up); exit code 1 on failure.

## Notes

- `tests.py` imports the module directly (`import harness`).
- Tools are defined once in the `TOOLS` dict (`harness.py:298`): each entry is `(OpenAI schema, handler)`. `OPENAI_TOOLS`/`DISPATCH` are derived from it — add new tools there.
- All tool file paths are relative to CWD and must stay inside it (`safe_resolve`, `harness.py:24`); never weaken this.
- `patch_file` supports an optional `offset`/`lines` region to disambiguate duplicate `old_string` matches; `write_file` with `offset` does line-range replace/insert (semantics covered by `tests.py` — run it after touching those functions).
