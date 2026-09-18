#!/usr/bin/env python3
"""build.py: build a self-contained harnless executable with Nuitka.

Nuitka compiles harnless.py to C and bundles it into a standalone binary
for the platform this script runs on (no cross-compilation: run it on
each target OS, or in a CI matrix).

Usage:
    python build.py           # single-file binary (default)
    python build.py --onedir  # folder build (faster startup, fewer AV false positives)
    python build.py --lto     # enable LTO (smaller/faster binary, much slower build)

Requires:  pip install nuitka
           (Windows: a C compiler, or Nuitka downloads MinGW64 automatically;
            macOS: Xcode Command Line Tools, `xcode-select --install`)
Output:    dist/harnless  (dist/harnless.exe on Windows)
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "harnless.py"
DIST = ROOT / "dist"


def detect_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def nuitka_available() -> bool:
    probe = subprocess.run(
        [sys.executable, "-m", "nuitka", "--version"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a self-contained harnless executable with Nuitka.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else None,
    )
    parser.add_argument("--onedir", action="store_true",
                        help="build a folder instead of a single file (faster startup)")
    parser.add_argument("--lto", action="store_true",
                        help="enable LTO (smaller/faster binary, much slower build)")
    args = parser.parse_args()

    platform = detect_platform()
    if platform not in ("windows", "macos", "linux"):
        print(f"error: unsupported platform '{platform}'", file=sys.stderr)
        return 1

    if not nuitka_available():
        print("error: Nuitka is not installed for "
              f"{sys.executable}. Run: {sys.executable} -m pip install nuitka",
              file=sys.stderr)
        return 1

    mode = "standalone" if args.onedir else "onefile"
    cmd = [
        sys.executable, "-m", "nuitka",
        f"--{mode}",
        f"--output-dir={DIST}",
        "--remove-output",
        "--assume-yes-for-downloads",
    ]
    if args.lto:
        cmd.append("--lto=yes")
    if platform == "windows":
        cmd.append("--windows-console-mode=force")
    cmd.append(str(SOURCE))

    print(f"Building harnless for {platform} ({mode}):")
    print("  " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"\nbuild failed (exit code {result.returncode})", file=sys.stderr)
        return result.returncode

    exe = "harnless.exe" if platform == "windows" else "harnless"
    artifact = DIST / "harnless" / exe if args.onedir else DIST / exe
    if not artifact.exists():
        print(f"error: build reported success but {artifact} is missing", file=sys.stderr)
        return 1

    print(f"\nDone: {artifact}")
    print(f"Try it: {artifact} --help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
