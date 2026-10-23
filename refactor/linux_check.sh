#!/bin/sh
# Run the suite in a POSIX shell environment without podman in the way.
#
# A WSL distro is a real Linux for the purposes of this suite, with one wrinkle: the tests that
# run a snippet through run_shell ask for `python`, and a stock Debian has only `python3`. Put a
# `python` on PATH first and the same tests pass exactly as they do in the build container.
#
# The repo is copied to the distro's own filesystem rather than run from /mnt/x: NTFS mounts do
# not carry exec bits, and run_shell's whole job is executing things.
set -e
mkdir -p /tmp/bin
ln -sf "$(command -v python3)" /tmp/bin/python
export PATH=/tmp/bin:$PATH

SRC=$(dirname "$0")/..
rm -rf /tmp/hcheck
mkdir -p /tmp/hcheck
cd "$SRC"
cp -r harnless.py tests.py harnless_core refactor AGENTS.md README.md /tmp/hcheck/
cd /tmp/hcheck
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

python3 tests.py 2>&1 | grep -E '^(Ran |OK|FAILED)|^(FAIL|ERROR): ' | head -14
