"""shell: part of harnless (see harnless.py)."""

import os
import shutil
import signal
import subprocess

from harnless_core import caps, config, state


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

def _drain_killed(proc, exc) -> tuple:
    """The output a killed command had already produced.

    On Windows the reader threads hold it until communicate() collects it (the
    TimeoutExpired fields are empty there); on POSIX the exception already
    carries it. The drain is bounded: a pipe still held by something the tree
    kill missed must not hang the harness.
    """
    exc_out = getattr(exc, "stdout", None)
    exc_err = getattr(exc, "stderr", None)
    if isinstance(getattr(exc, "output", None), tuple):
        exc_out, exc_err = exc.output
    try:
        out, err = proc.communicate(timeout=config.KILL_DRAIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        out, err = None, None  # give up on the pipes rather than wait forever
        for pipe in (proc.stdout, proc.stderr):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass
    except (OSError, ValueError):
        out, err = None, None
    try:
        proc.wait(timeout=config.KILL_DRAIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        pass  # already killed: stop caring instead of hanging on it
    return (out or exc_out or ""), (err or exc_err or "")

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

def tool_run_shell(args: dict) -> str:
    command = args.get("command", "")
    if not command.strip():
        return "error: empty command"
    timeout, bad_timeout = _shell_timeout(args)
    if bad_timeout:
        return bad_timeout
    limit = caps._limit_arg(args, "max_output", config.SHELL_OUTPUT_LIMIT, config.TOOL_RESULT_LIMIT)
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
    spawn = {
        "shell": use_shell,
        "cwd": state.CWD,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": encoding,
        # A stray non-UTF-8 byte (e.g. a native command writing in the
        # ANSI code page) must not crash the reader thread and wipe the
        # whole output; degrade to U+FFFD instead.
        "errors": "replace",
    }
    if os.name != "nt":
        # Its own process group, so _kill_tree can signal the shell *and*
        # everything it forked (taskkill /T plays that role on Windows).
        spawn["start_new_session"] = True
    try:
        # Popen rather than run(timeout=...): run() kills only the direct child
        # and then drains the pipes with no deadline of its own.
        proc = subprocess.Popen(cmd, **spawn)
    except (OSError, ValueError) as e:
        return f"error: could not run the command: {e}"
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        _kill_tree(proc)
        stdout, stderr = _drain_killed(proc, e)
        parts = [
            f"error: command timed out after {timeout}s — it was killed, "
            "along with everything it started"
        ]
        chunks = []
        if stdout.strip():
            chunks.append(stdout.rstrip())
        if stderr.strip():
            chunks.append(f"[stderr]\n{stderr.rstrip()}")
        if chunks:
            parts.append(
                "[partial output]\n"
                + caps._truncate(
                    "\n".join(chunks),
                    limit,
                    hint=(
                        "re-run with a larger timeout, or redirect to a file and "
                        "read it with read_file"
                    ),
                )
            )
        return "\n".join(parts)
    out = []
    if stdout:
        out.append(stdout.rstrip())
    if stderr:
        out.append(f"[stderr]\n{stderr.rstrip()}")
    result = "\n".join(out) if out else "(no output)"
    result = caps._truncate(
        result,
        limit,
        hint="re-run with a larger max_output, or redirect to a file and read it with read_file",
    )
    return f"exit code: {proc.returncode}\n{result}"
