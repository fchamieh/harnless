# Releasing harnless

A release is five things: a version bump, a pair of commits, a tag, two binaries in `dist/`,
and one notes file. `dist/` is gitignored, so only the commits and the tag land in the repo.

Short form:

1. Bump `VERSION` in `harnless_core/config.py`
2. Commit the work, then commit the bump alone as `release: X.Y.Z`; tag `vX.Y.Z`
3. `python tests.py` (with the right git on PATH)
4. `python build.py` + `python build.py --target linux` → rename the two artifacts
5. Verify both binaries; run the suite in the builder container
6. Write `dist/RELEASE-X.Y.Z.md`; delete the scratch files

## 1. Pick the version

- Any `feat:` since the last release → minor (`1.11.0` → `1.12.0`)
- Only `fix`/`refactor`/`chore` → patch (`1.11.0` → `1.11.1`)

## 2. Commits and tag

- Conventional-commit style, as in `git log --oneline`
  (e.g. `fix: a 'model not found' rejection lists the server's models and points at --model`).
- The `release:` commit must contain **only** the version bump. When the work also touched
  `config.py`, revert the `VERSION` line temporarily, commit the work, re-apply the bump,
  then commit `release: X.Y.Z`.
- Tag the release commit: `git tag vX.Y.Z`.

## 3. Test before building

`python tests.py` — about a minute; the expected result on the Windows host is
`OK (skipped=8)` (the pty/raw-mode/TTY and Linux-shell tests).

**Gotcha — which git is on PATH.** `TestGitignoreAgainstGit` diffs harnless's engine against
the *installed* git. This machine has two gits: cmder's 2.45.1
(`X:\Software Installers\cmder_with_git\vendor\git-for-windows`) which precedes the real
install (`C:\Program Files\Git`, 2.55.x) on PATH. Old git matches an embedded `**`
(e.g. `O**/S.txt`) across directories; new git treats it like `*` — so the suite fails
against 2.45.1 with a star-case mismatch. Prepend the real one:

```powershell
$env:PATH = "C:\Program Files\Git\cmd;" + $env:PATH
```

## 4. Build

- **Windows**: `python build.py` → `dist/harnless.exe` (Nuitka onefile, MSVC `cl`).
- **Linux**: `python build.py --target linux` → `dist/harnless`, cross-built in the
  `harnless-builder` image (built once with `podman build -t harnless-builder .`).

Both take a few minutes; run them in the background and poll the log. Nuitka's stdout is
block-buffered when redirected, so an empty build log mid-build is normal.

**Gotcha — podman machine.** The WSL machine is often stopped: `podman machine start`
before the Linux build.

**Gotcha — stale build dir.** A `dist/harnless.build` left by a previous *container* build
breaks the next one: Nuitka cannot unlink its `.obj` files on the Windows mount
(`PermissionError [Errno 13]`). Delete `dist/harnless.build` (and any
`nuitka-crash-report.xml`) from the Windows side before re-running.

## 5. Rename the artifacts

```powershell
Copy-Item dist/harnless.exe dist/harnless-X.Y.Z-windows-amd64.exe
Copy-Item dist/harnless     dist/harnless-X.Y.Z-linux-amd64
```

Leave the unversioned names (local dev builds) and the old versioned files in place.

## 6. Verify both binaries

For each:

- `--version` prints `harnless X.Y.Z`.
- A run against a dead endpoint — `--prompt hi --api-url http://127.0.0.1:9/v1/chat/completions`
  — shows the banner and session journal, streaming falls back to non-streaming, ends with a
  clean connection error, exit code 1, no traceback.
- When the release changed an error path, exercise it too — e.g. against a tiny stub
  `http.server` that answers `/v1/models` and rejects `/v1/chat/completions`.

The Linux binary cannot run on the host; run it inside the builder image, which has glibc:

```powershell
podman run --rm -v "<repo>:/src" -w /src harnless-builder bash /src/verify.sh
```

the repo mount makes `dist/` and any helper script visible inside.

**Gotcha — CRLF scripts.** A bash script written on Windows is CRLF, and the container then
fails in mangled ways (a trailing `\r` rides along with arguments — argparse reports
"unrecognized arguments: `--version`" when it was really `--version\r`; `sleep` and `set`
complain about invalid options). Convert the script to LF before running it.

## 7. Container test run (for the notes table)

```powershell
podman run --rm -v "<repo>:/src" -w /src harnless-builder python tests.py
```

expected: `OK (skipped=4)` — the Windows/PowerShell-only tests.

## 8. Write `dist/RELEASE-X.Y.Z.md`

Follow the shape of the previous `RELEASE-*.md` files:

- a one-paragraph intro: what changed since the last version;
- one section per change: the complaint it answers, how it works, a sample of the output;
- an **Artifacts** table: `file | size | sha256 | built with` — sizes from the filesystem,
  hashes from `Get-FileHash -Algorithm SHA256`, the compiler line from the build log
  (e.g. `Nuitka-Scons: Backend C compiler: cl (cl 14.5)`);
- a verification paragraph stating exactly what was run on each binary;
- a **Tests** section: the total with the previous version's count in parentheses, then an
  environment table (builder container with its Python/git versions, Windows host with its
  Python/git versions, and the skip counts with what they skip).

## 9. Clean up and final state

Delete the scratch: build logs, verify scripts, stub servers, `nuitka-crash-report.xml`.
`git status` must be clean, and the log must read:

```
release: X.Y.Z        ← vX.Y.Z
fix: … / feat: …
…
```
