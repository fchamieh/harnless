"""run_shell: running a command, and stopping everything it started.

The shell is chosen once per process (`_resolve_shell`) and cached: the command is what the
model asked for, and only the argv in front of it depends on the platform. A timeout or an
interrupt has to reach further than the shell itself — a `pwsh -Command` that spawned a build
leaves the build running when the shell dies — so the process tree is walked and killed root
down (real child links on Windows via the toolhelp snapshot, a process group on POSIX), and
the output kept is the window around whatever the harness stopped: the tail of a long build if
it ran to the end, the head of a hung one.

Cancellation is separate from the timeout on purpose: ESC in the REPL must stop a run even in
a one-shot call that has no watcher thread of its own.

Output is read by daemon reader threads, never by `communicate`. That distinction is what
tells "the command is still running" apart from "someone still holds the pipes": the call
waits on the command wrapper, and when the wrapper is done but a pipe stays open — the
started process kept a handle it inherited from the shell, e.g. `Start-Process` with
redirects on Windows, `cmd &` on bash — the call returns with what it collected and names
the stray instead of waiting out the full timeout and then killing the very process the
model was trying to detach. `background:true` is the first-class answer to the same need:
the command runs detached, logging to a file under BG_DIR, and the call returns at once;
`cleanup_background` stops whatever is still running when the harness exits."""

import locale
import os
import shutil
import signal
import subprocess
import threading
import time

from harnless_core import caps, config, paths, state


def _resolve_shell(preferred: str = "auto") -> tuple:
    """Resolve the shell run_shell should use.

    Returns (argv_prefix, command_prefix):
      argv_prefix: list [shell, flags...] to spawn directly, or None to use
                   shell=True (cmd.exe on Windows, sh elsewhere).
      command_prefix: text prepended to the user command (forces UTF-8 console
                      output on all PowerShell — both 5.1 and pwsh 7 may emit
                      the ANSI code page on redirected stdout, e.g. cp1256 on
                      an Arabic-locale Windows with pwsh 7.6).

    On Windows the preference order is pwsh (PowerShell 7+) → powershell.exe
    (5.1) → cmd.exe; `preferred` can force one of them (falling back to the
    order when the forced shell is missing). Non-Windows always uses shell=True.
    """
    if os.name != "nt":
        return (None, "")
    if preferred in state._SHELL_CACHE:
        return state._SHELL_CACHE[preferred]

    def _pwsh():
        p = shutil.which("pwsh")
        return [p, "-NoProfile", "-NonInteractive", "-Command"] if p else None

    def _ps():
        p = shutil.which("powershell")
        return [p, "-NoProfile", "-NonInteractive", "-Command"] if p else None

    utf8 = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    result = None
    if preferred == "pwsh":
        a = _pwsh()
        result = (a, utf8) if a else None
    elif preferred == "powershell":
        a = _ps()
        result = (a, utf8) if a else None
    elif preferred == "cmd":
        result = (None, "")
    if result is None:  # auto, or a forced shell that is missing
        a = _pwsh()
        result = (a, utf8) if a else None
        if result is None:
            a = _ps()
            result = (a, utf8) if a else (None, "")
    state._SHELL_CACHE[preferred] = result
    return result

def _taskkill_cmd() -> list:
    """taskkill.exe as a command prefix on Windows ([] where it is missing);
    POSIX kills whole process groups instead — see _kill_tree."""
    if os.name != "nt":
        return []
    path = shutil.which("taskkill")
    if not path:
        path = os.path.join(
            os.environ.get("WINDIR") or r"C:\Windows", "System32", "taskkill.exe"
        )
    return [path, "/PID"] if os.path.exists(path) else []

_TASKKILL = _taskkill_cmd()  # resolved once: None where taskkill does not exist


def _kill_tree(proc) -> None:
    """Stop a timed-out command *and everything it started*.

    proc.kill() reaches only the shell wrapper. Its children — the actual
    compile/test/serve — inherited the output pipes and keep running, and the
    harness then waits on those pipes: that was the "timeout" that never came
    back and the runaway the user had to kill by hand.
    """
    if _TASKKILL and proc.pid:
        try:
            subprocess.run(
                _TASKKILL + [str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=config.TASKKILL_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass  # taskkill failed or is missing: last resort below
    elif os.name != "nt":
        try:
            # The command is spawned in its own process group, so the shell and
            # everything it forked go together.
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass

def _shell_timeout(args: dict) -> tuple:
    """run_shell's timeout as (seconds, error_message).

    Explicit about a bad value (as fetch_url is) and capped at
    SHELL_TIMEOUT_CEILING: a tool call that never returns stalls the turn.
    """
    raw = args.get("timeout")
    if raw is None:
        raw = config.SHELL_TIMEOUT_DEFAULT
    try:
        timeout = int(float(raw))
    except (TypeError, ValueError):
        return 0, "error: timeout must be a number of seconds"
    if timeout < config.SHELL_TIMEOUT_MIN:
        return 0, f"error: timeout must be >= {config.SHELL_TIMEOUT_MIN}"
    return min(timeout, config.SHELL_TIMEOUT_CEILING), None

def _output_limit(args: dict) -> tuple:
    """run_shell's output pair: how many characters to keep, and from which end.

    `tail` is the model saying *the answer is at the end* — a test summary, a build
    failure, the last frame of a traceback. `max_output` stays the budget either
    way, so the two never fight over the same characters: one sizes the window, the
    other says where the window sits.
    """
    limit = caps._limit_arg(args, "max_output", config.SHELL_OUTPUT_LIMIT, config.TOOL_RESULT_LIMIT)
    return limit, caps._bool_arg(args, "tail", False)

def _cap_output(text: str, limit: int, tail: bool, heading: str, hint: str) -> str:
    """Cap run_shell's output at `limit` characters, keeping the beginning or the end.

    `heading` is the `[stderr]` label when the output carries one (empty otherwise).
    It sits *between* the two blocks, so a tail window smaller than stderr itself
    starts below it and hands back stderr lines with no label on them — which reads
    as stdout. A kept tail always reaches the end of the text, so a heading that
    survived is still in it; when the cut swallowed one it goes back on top of what
    remained, and room for it comes off the tail rather than off the ceiling.
    """
    if not tail:
        return caps._truncate(text, limit, hint=hint)
    room = limit - (len(heading) + 1) if heading else limit
    kept, dropped = caps._tail_window(text, max(1, room))
    if heading:
        at = text.rfind("\n" + heading)
        if at == -1 and text.startswith(heading):
            at = 0
        if at != -1 and dropped > at:  # the window starts below the label
            kept = heading + "\n" + kept
    snapped = dropped > len(text) - room  # the snap gave back more than the window needed
    return caps._cap_note_above(
        f"{len(text)} chars total, showing the last {len(text) - dropped}"
        + (" (cut forward to the next line start)" if snapped else ""),
        hint + ", or pass tail:false to keep the beginning instead",
    ) + kept

def _pump(pipe, chunks: list) -> None:
    """A daemon reader: append a pipe's bytes to `chunks` as they arrive.

    Draining the pipes while the command runs is what keeps a chatty command
    from blocking on a full pipe buffer while the harness waits for it to exit;
    read1 hands over whatever is available instead of waiting for a full buffer.
    Closing the pipe (or the process behind it dying) ends the loop; a pipe the
    harness gave up on closes the reader with an OSError it swallows.
    """
    try:
        while True:
            data = pipe.read1(65536)
            if not data:
                break
            chunks.append(data)
    except (OSError, ValueError):
        pass
    try:
        # EOF reached (or given up on): the reader hands its side of the pipe
        # back. _close_pipes then finds it already closed, which it tolerates.
        pipe.close()
    except (OSError, ValueError):
        pass

def _join_readers(readers: list, seconds: float) -> bool:
    """Give the pipe readers *seconds* to finish; True when all of them did.

    After the command wrapper exits this is the grace window that separates a
    finished command — its pipes hit EOF at once — from one that left a process
    behind holding an inherited copy of the pipe handles.
    """
    deadline = time.monotonic() + seconds
    for reader in readers:
        reader.join(max(0.0, deadline - time.monotonic()))
    return not any(reader.is_alive() for reader in readers)

def _close_pipes(proc) -> None:
    for pipe in (proc.stdout, proc.stderr):
        if pipe:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

def _decode(chunks: list, encoding) -> str:
    """The bytes a reader collected, as text — with the universal-newline
    translation text-mode used to do (`\r\n` and a lone `\r` become `\n`), so
    the capping and the model see one kind of line break. A stray non-UTF-8
    byte (a native command writing in the ANSI code page) becomes U+FFFD
    instead of wiping the output; None means the locale's own encoding."""
    if not chunks:
        return ""
    text = b"".join(list(chunks)).decode(
        encoding or locale.getpreferredencoding(False), "replace"
    )
    return text.replace("\r\n", "\n").replace("\r", "\n")

def _background_log() -> str:
    """A fresh log path for a background run: bg-<n>-<timestamp>.log in BG_DIR.

    Inside CWD so read_file/grep reach it, and never reused: a second server
    must not truncate the log a still-running first one is writing."""
    os.makedirs(state.BG_DIR, exist_ok=True)
    while True:
        state.BACKGROUND_SEQ += 1
        full = os.path.join(
            state.BG_DIR, f"bg-{state.BACKGROUND_SEQ}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        )
        if not os.path.exists(full):
            return full

def _start_background(cmd, use_shell: bool) -> str:
    """Spawn the command detached and return at once: no pipes to wait on, no
    output to collect — the call's job is only to start the thing and say where
    its output lives. stdout and stderr share the log file; on POSIX the process
    gets its own session (kill -- -<pid> reaches everything it forked), on
    Windows its own process group (a Ctrl+C aimed at the harness console does
    not reach it)."""
    log_full = _background_log()
    try:
        log_file = open(log_full, "wb")
    except OSError as e:
        return f"error: could not open the background log: {e}"
    spawn = {
        "shell": use_shell,
        "cwd": state.CWD,
        "stdin": subprocess.DEVNULL,  # never let it eat the REPL's keystrokes
        "stdout": log_file,
        "stderr": log_file,
    }
    if os.name != "nt":
        spawn["start_new_session"] = True
    else:
        spawn["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(cmd, **spawn)
    except (OSError, ValueError) as e:
        log_file.close()
        try:
            os.remove(log_full)
        except OSError:
            pass
        return f"error: could not run the command: {e}"
    log_file.close()  # the child has its own handle; the harness's copy would just leak
    state.BACKGROUND_PROCS.append({"pid": proc.pid, "log": log_full, "proc": proc})
    stop = f"taskkill /PID {proc.pid} /T /F" if os.name == "nt" else f"kill -- -{proc.pid}"
    return (
        f"started in background: pid {proc.pid}\n"
        f"log: {paths._rel_to_cwd(log_full)} (grows while the process writes; read it with read_file)\n"
        f"the process keeps running after this call returns; stop it with {stop}. "
        "A leftover background process is killed when harnless exits."
    )

def cleanup_background() -> int:
    """Stop every background:true process that is still running; say how many.

    The promise behind background:true is *across calls*, not *across sessions*:
    main() registers this at exit so the harness never leaves a server it started
    running behind it (tests call it from tearDown too)."""
    killed = 0
    for entry in state.BACKGROUND_PROCS:
        proc = entry.get("proc")
        if proc is None or proc.poll() is not None:
            continue  # already finished on its own
        _kill_tree(proc)
        try:
            proc.wait(timeout=config.KILL_DRAIN_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass  # already killed: stop caring instead of hanging on it
        killed += 1
    state.BACKGROUND_PROCS.clear()
    return killed

def tool_run_shell(args: dict) -> str:
    command = args.get("command", "")
    if not command.strip():
        return "error: empty command"
    timeout, bad_timeout = _shell_timeout(args)
    if bad_timeout:
        return bad_timeout
    limit, tail = _output_limit(args)
    argv_prefix, command_prefix = _resolve_shell(state.SHELL_PREFERRED)
    if argv_prefix is not None:
        # PowerShell -Command maps any non-zero native exit code to 1; append
        # `exit $LASTEXITCODE` so the real exit code of the last native command
        # is propagated (pure PowerShell statements leave it at 0).
        cmd = argv_prefix + [command_prefix + command + "; exit $LASTEXITCODE"]
        use_shell = False  # `shell` is a module alias in this file
        # PowerShell output is UTF-8 (forced via the command prefix on both
        # pwsh 7 and 5.1); decode as UTF-8 rather than the locale code page.
        encoding = "utf-8"
    else:
        cmd = command
        use_shell = True
        encoding = None
    if caps._bool_arg(args, "background", False):
        return _start_background(cmd, use_shell)
    spawn = {
        "shell": use_shell,
        "cwd": state.CWD,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name != "nt":
        # Its own process group, so _kill_tree can signal the shell *and*
        # everything it forked (taskkill /T plays that role on Windows).
        spawn["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **spawn)
    except (OSError, ValueError) as e:
        return f"error: could not run the command: {e}"
    # Wait on the *command wrapper*, not on the pipes closing: communicate()
    # conflates the two, and a process the command merely started — a
    # Start-Process child inherits the shell's handle copies even with its own
    # std handles redirected — then reads as "the command still runs" forever.
    chunks = [[], []]  # stdout bytes, stderr bytes, as the readers collect them
    readers = [
        threading.Thread(target=_pump, args=(proc.stdout, chunks[0]), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, chunks[1]), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        # Whether the readers finished or not, the harness-side pipe handles
        # close here: a tree kill that missed something stops being a reason to
        # wait on its output forever.
        _join_readers(readers, config.KILL_DRAIN_TIMEOUT)
        _close_pipes(proc)
        try:
            proc.wait(timeout=config.KILL_DRAIN_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass  # already killed: stop caring instead of hanging on it
        stdout, stderr = _decode(chunks[0], encoding), _decode(chunks[1], encoding)
        parts = [
            f"error: command timed out after {timeout}s — it was killed, "
            "along with everything it started"
        ]
        bits = []
        if stdout.strip():
            bits.append(stdout.rstrip())
        if stderr.strip():
            bits.append(f"[stderr]\n{stderr.rstrip()}")
        if bits:
            parts.append(
                "[partial output]\n"
                + _cap_output(
                    "\n".join(bits),
                    limit,
                    tail,
                    "[stderr]" if stderr.strip() else "",
                    "re-run with a larger timeout, or redirect to a file and read it with read_file",
                )
            )
        return "\n".join(parts)
    # The command is done. Its own pipes closing is the normal ending; readers
    # still busy after the grace window mean a process it started is holding a
    # handle — leave that process alone (it is the model's attempt at a server,
    # not at a runaway) and say so plainly. The blocked readers retire on the
    # stray's own EOF or when the harness exits; closing a pipe another thread
    # is blocked reading is not safe, so the harness never force-closes those.
    detached = not _join_readers(readers, config.SHELL_DETACHED_GRACE)
    if not detached:
        _close_pipes(proc)  # the normal ending: hand the pipe handles back
    out = []
    stdout, stderr = _decode(chunks[0], encoding), _decode(chunks[1], encoding)
    if stdout:
        out.append(stdout.rstrip())
    if stderr:
        out.append(f"[stderr]\n{stderr.rstrip()}")
    result = "\n".join(out) if out else "(no output)"
    result = _cap_output(
        result,
        limit,
        tail,
        "[stderr]" if stderr else "",
        "re-run with a larger max_output, or redirect to a file and read it with read_file",
    )
    if detached:
        stray = (
            "Get-Process / tasklist, then taskkill /PID <pid> /T /F"
            if os.name == "nt"
            else "ps, then kill -- -<pid>"
        )
        result += (
            "\n[detached: the command finished, but a process it left running is still holding "
            "the output pipes — its further output went unread and it was NOT killed. "
            f"Find it with {stray}; for long-running commands prefer background:true.]"
        )
    return f"exit code: {proc.returncode}\n{result}"
