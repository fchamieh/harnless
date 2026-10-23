#!/bin/sh
# Copy the working tree into the distro and run the `**` semantics probe there.
# Kept as a file: PowerShell hands `bash -c '... $PATH ...'` over with $PATH already expanded,
# and the Windows PATH entries contain parentheses bash then tries to parse.
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
git --version
python3 refactor/git_semantics.py
