#!/bin/sh
# Run one test from the copied-out suite in a WSL distro and show its diff.
#
# Kept as a file rather than an inline `bash -c '...'`: PowerShell hands the string over with
# $PATH already expanded, and the Windows PATH entries contain parentheses that bash then tries
# to parse as syntax.
#
# usage: wsl -d Debian -u root bash ./refactor/one_test.sh tests.TestRunShell.test_success
set -e
mkdir -p /tmp/bin
ln -sf "$(command -v python3)" /tmp/bin/python
export PATH=/tmp/bin:$PATH
cd /tmp/hcheck
python3 -m unittest "$1" 2>&1 | tail -25
echo "--- which git: $(command -v git || echo none); $(git --version 2>/dev/null || echo 'no git')"
