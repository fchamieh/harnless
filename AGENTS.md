# AGENTS.md

`harnless.py` is the entry point and CLI of a small CLI agent harness: it talks to a
local llama-server (OpenAI-compatible API) and exposes file/shell tools (bash on POSIX,
PowerShell on Windows). Its machinery lives in the `harnless_core/` package. Stdlib only;
no dependencies, no venv; builds are in `build.py`.

## Requirements

- A running LLM server for the agent to talk to. Default endpoint is
  `http://127.0.0.1:11434/v1/chat/completions` (`API_URL` in `harnless_core/state.py`),
  overridable via `--api-url`. Tests (`tests.py`) don't need a server.

## Layout

`import harnless` still exposes every name — the entry point keeps a generated `_MODULES`
list plus a module `__getattr__` as a compatibility façade, so `harnless.tool_grep(...)`
and tests that patch `harnless.<name>` keep working. New code reaches a core value through
its module (`state.CONTEXT_WINDOW = n`, `config.PATCH_CONTEXT_LINES`): a name imported by
value is a copy, and a copy stops seeing the writes the flags, the slash commands and the
tests make.

| module | what lives there |
|---|---|
| `config.py` | fixed tunables and the model-facing text (what is never changed after import), `REPL_COMMANDS` |
| `state.py` | everything mutable after import: flags, session identity, journals, MCP state |
| `errors.py` | `ExitSignal`, `ContextOverflow`, `StreamInterrupted`, the sub-agent signals |
| `caps.py` | output ceilings and how a capped value says so |
| `paths.py` | CWD containment and path resolution |
| `measure.py` | what the next request costs (`estimate_context_tokens`) |
| `render.py` | colour/icon tables, the Markdown renderer, printers, terminal widths |
| `toolspec.py` | the contract the model is shown: one OpenAI schema per built-in tool |
| `journal.py` | the session journal: records, resume, replay, the `sessions` tool |
| `context.py` | system prompt, `AGENTS.md`, memory, the todo block, `file://` refs, history |
| `lineedit.py` | key iterators, the multi-line editor, `InterruptWatcher`, the `/` command menu |
| `request.py` | request building, the abortable/counting opener, chat + streaming, SSE |
| `compact.py` | the cut, the pruning, the handoff note, both triggers |
| `search.py` | the walking/.gitignore engine and the grep, glob, list_dir, patch, move, copy tools |
| `shell.py` | shell resolution, process-tree killing, timeouts, `run_shell` |
| `mcp.py` | the MCP client: JSON-RPC over stdio or HTTP, discovery, registration |
| `agent.py` | the loop: `run_agent`/`_agent_loop`, what a turn advertises, closing summaries |
| `toolkit.py` | the tools a model calls, `DISPATCH`, and `execute_tool` (the executor a run is handed) |
| `harnless.py` | the CLI: argument parsing, the REPL loop, the status/help text, and the facade |

Three rules keep the layers honest:

- **A core module never imports `harnless`.** If one needs a name that is still in the entry
  point, that name moves down first (the link stage refuses it, and
  `TestAgentSeam.test_no_core_module_reaches_up_to_the_entry_point` checks it from the test
  side too).
- **Nothing above the agent layer decides which tools a request advertises.**
  `chat`/`stream_chat`/`stream_once`/`_request_body` take `tools: list | None`; the agent
  loop hands them `_active_tools(interactive)`, and a request meant only to write something
  (the compaction handoff note) is handed `None`.
- **The loop is handed the thing that runs a tool call.** `run_agent(..., execute=...)` is
  keyword-only and required: `main()` passes `toolkit.execute_tool`, and `tool_task` hands the
  run it starts the executor its own module owns — so one table answers calls at every depth,
  and the loop can be run over a tool name that exists nowhere in the harness (that is how
  `TestAgentSeam` tests it). The tool halves are split too: `toolspec.SPECS` is the contract
  (data, ordered), `toolkit.DISPATCH` is the behaviour (name → function), and a test asserts
  the two name the same tools in the same order.

## Moving code

`refactor/codemod.py` is the tool for it, and `refactor/registry.json` records where every
name lives:

```
python refactor/codemod.py pre              # renames that must happen before a move
python refactor/codemod.py extract <stage>  # create harnless_core/<stage>.py from its manifest
python refactor/codemod.py move <module> <name> ...   # add names to a module that exists
python refactor/codemod.py cycle            # relink + retarget tests + shadow audit
python refactor/codemod.py facade           # rebuild the forwarding import block alone
python refactor/codemod.py audit            # counts, facade writes left, duplicates
```

It rewrites references (bare name → `module.name`, never an alias), derives each file's
imports from what it actually uses (including the by-name imports a stage needs, which travel
with the code that needs them), and **refuses a local binding that shadows a module alias** —
a function with a local named `tools` would read that list every time the link stage wrote
`tools.<name>`. `refactor/rename_local.py` (token-based, so keyword argument names and string
literals are untouched) frees such a name; it is also why the tools module is called `toolkit`.
`relink()` likewise refuses a module that uses a name still living in the entry point: that
name moves down first, as `REPL_COMMANDS` (to config) and the `/` command menu (to lineedit)
did for the tools stage.

An extract is one-way: to undo one, `git checkout harnless.py tests.py refactor/registry.json`
*and* delete the new `harnless_core/<module>.py`, or its names stay behind as duplicates
(`audit` reports those).

## Notes

- `tests.py` imports the module directly (`import harnless`) and patches the module that
  owns each name (`h.request.chat = ...`).
- The Windows/Linux release builds are `python build.py` and `python build.py --target
  linux` (podman `harnless-builder`); see the release notes in `README.md`.
