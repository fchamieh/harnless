"""The agent itself: the tool tables and their dispatch, tool execution, sub-agent
runs, and the conversation loop."""

import urllib
from harnless_core import compact, config, context, errors, journal, render, request, state, toolspec

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

def _active_tools(interactive: bool) -> list:
    """Tool specs to send to the API, excluding tools toggled off via /tools."""
    base = toolspec.OPENAI_TOOLS_INTERACTIVE if interactive else toolspec.OPENAI_TOOLS
    return [s for s in base + state.MCP_TOOLS if s["function"]["name"] not in state.DISABLED_TOOLS]

def _all_tool_names() -> set:
    return {s["function"]["name"] for s in toolspec.OPENAI_TOOLS_INTERACTIVE + state.MCP_TOOLS}

def run_agent(
    messages: list,
    model: str,
    *,
    execute,
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
        return _agent_loop(
            messages, model, execute=execute, interactive=interactive,
            temperature=temperature, conv_id=conv_id,
        )
    finally:
        state._AGENT_DEPTH = previous_depth

def _agent_loop(
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
    """
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
                result = execute(name, raw_args)
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
