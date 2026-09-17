# AGENTS.md

Single-file Python project: `fadiz-harness.py` — a minimal CLI agent harness that talks to a local llama-server (OpenAI-compatible API) and exposes file/bash tools. Stdlib only; no dependencies, no venv, no build step.

## Requirements

- A running LLM server at `http://127.0.0.1:11434/v1/chat/completions` (hardcoded `API_URL` in `fadiz-harness.py:13`). Nothing works without it.

## Commands

- Interactive: `python fadiz-harness.py` (chat REPL; `/new` clears session history, `/clear-screen` clears the terminal, `/exit` quits)
- One-shot: `python fadiz-harness.py --prompt "task"` (exits with the agent's exit code)
- Flags: `--model` (name sent in the request; llama-server ignores it), `--system-prompt` (replaces built-in)
- Tests: `python test_line_ops.py` — plain script, no pytest. Must run from repo root; creates and deletes `./t.txt`; exit code 1 on failure.

## Notes

- `fadiz-harness.py` has a hyphen, so it can't be imported normally; `test_line_ops.py` loads it via `importlib.util.spec_from_file_location`. Keep that pattern if adding tests.
- Tools are defined once in the `TOOLS` dict (`fadiz-harness.py:222`): each entry is `(OpenAI schema, handler)`. `OPENAI_TOOLS`/`DISPATCH` are derived from it — add new tools there.
- All tool file paths are relative to CWD and must stay inside it (`safe_resolve`, `fadiz-harness.py:24`); never weaken this.
- `patch_file` supports an optional `offset`/`lines` region to disambiguate duplicate `old_string` matches; `write_file` with `offset` does line-range replace/insert (semantics covered by `test_line_ops.py` — run it after touching those functions).
