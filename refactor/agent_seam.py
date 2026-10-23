"""One-off seam: the agent loop is handed the thing that runs a tool call.

_agent_loop used to call execute_tool, which lives in harnless.py next to the tool table — so
agent.py could only have been written by letting a core module import the entry point. Instead
the loop takes `execute(name, raw_args) -> str`: it still decides what a turn is worth, what to
advertise, when to compact and when to stop, but it no longer knows which tools exist or how
one is run.

Keyword-only and required on purpose. Every caller says out loud which tool set a run may use.
"""

import pathlib
import re

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "harnless.py"

EDITS = [
    (
        """def run_agent(
    messages: list,
    model: str,
    interactive: bool = False,""",
        """def run_agent(
    messages: list,
    model: str,
    *,
    execute,
    interactive: bool = False,""",
    ),
    (
        """        return _agent_loop(messages, model, interactive, temperature, conv_id)""",
        """        return _agent_loop(
            messages, model, execute=execute, interactive=interactive,
            temperature=temperature, conv_id=conv_id,
        )""",
    ),
    (
        '''def _agent_loop(
    messages: list,
    model: str,
    interactive: bool = False,
    temperature: float = 1.0,
    conv_id: int = None,
) -> int:
    """The agent loop: stream a turn, run its tool calls, repeat until the
    model answers without tool calls, calls `exit`, or is interrupted."""''',
        '''def _agent_loop(
    messages: list,
    model: str,
    *,
    execute,
    interactive: bool = False,
    temperature: float = 1.0,
    conv_id: int = None,
) -> int:
    """The agent loop: stream a turn, run its tool calls, repeat until the
    model answers without tool calls, calls `exit`, or is interrupted.

    `execute(name, raw_args) -> str` is the caller's: the loop decides what a turn is worth,
    what to advertise, when to compact and when to stop, but it does not know which tools
    exist or how one is run — which is why harnless_core/agent.py never has to look up at the
    entry point that owns the tool table. Sub-agents inherit it (tool_task passes it on), so a
    delegated run answers calls with the same tools its parent had.
    """''',
    ),
    (
        "        result = execute_tool(name, raw_args)",
        "        result = execute(name, raw_args)",
    ),
    (
        """            code = run_agent(
                messages,
                state.MODEL,
                interactive=False,""",
        """            code = run_agent(
                messages,
                state.MODEL,
                execute=execute_tool,
                interactive=False,""",
    ),
    (
        """            run_agent(messages, args.model, temperature=args.temperature,
                      conv_id=state.MAIN_CONV_ID)""",
        """            run_agent(
                messages,
                args.model,
                execute=execute_tool,
                temperature=args.temperature,
                conv_id=state.MAIN_CONV_ID,
            )""",
    ),
    (
        """        run_agent(
            messages,
            args.model,
            interactive=True,""",
        """        run_agent(
            messages,
            args.model,
            execute=execute_tool,
            interactive=True,""",
    ),
]


def main():
    src = SOURCE.read_text(encoding="utf-8")
    for old, new in EDITS:
        n = src.count(old)
        if n != 1:
            raise SystemExit(f"anchor matched {n} times: {old[:60]!r}")
        src = src.replace(old, new)
    SOURCE.write_text(src, encoding="utf-8", newline="\n")
    calls = len(re.findall(r"\brun_agent\(", src))
    print(f"the loop now runs tool calls through its caller ({calls} run_agent mentions)")


if __name__ == "__main__":
    main()
