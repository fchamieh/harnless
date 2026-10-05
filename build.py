#!/usr/bin/env python3
"""build.py: build a self-contained harnless executable with Nuitka.

Nuitka compiles harnless.py to C and bundles it into a standalone binary
for the platform this script runs on (no cross-compilation: run it on
each target OS, or in a CI matrix).

Usage:
    python build.py           # single-file binary (default)
    python build.py --onedir  # folder build (faster startup, fewer AV false positives)
    python build.py --lto     # enable LTO (smaller/faster binary, much slower build)
    python build.py --target linux
                              # cross-build a Linux binary via the 'harnless-builder'
                              # container image (podman/docker); build the image once
                              # with: podman build -t harnless-builder .

Requires:  pip install nuitka
           (Windows: a C compiler, or Nuitka downloads MinGW64 automatically;
            macOS: Xcode Command Line Tools, `xcode-select --install`)
Output:    dist/harnless  (dist/harnless.exe on Windows)
"""

import argparse
import shutil
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


def find_container_engine() -> str | None:
    for name in ("podman", "docker"):
        if shutil.which(name):
            return name
    return None


def build_in_container(extra: list[str]) -> int:
    """Cross-build for Linux inside the pre-baked 'harnless-builder' image."""
    engine = find_container_engine()
    if engine is None:
        print("error: no container engine found (need podman or docker on PATH)",
              file=sys.stderr)
        return 1

    probe = subprocess.run(
        [engine, "image", "inspect", "harnless-builder"],
        capture_output=True,
    )
    if probe.returncode != 0:
        print("error: 'harnless-builder' image not found. Build it first from the "
              f"repo root:\n  {engine} build -t harnless-builder .",
              file=sys.stderr)
        return 1

    cmd = [
        engine, "run", "--rm",
        "-v", f"{ROOT.as_posix()}:/src",
        "-w", "/src",
        "harnless-builder",
        "python", "build.py", *extra,
    ]
    print(f"Building harnless for linux in a container ({engine}):")
    print("  " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print(f"\nbuild failed (exit code {result.returncode})", file=sys.stderr)
        return result.returncode

    artifact = DIST / "harnless"
    if not artifact.exists():
        print(f"error: build reported success but {artifact} is missing", file=sys.stderr)
        return 1

    print(f"\nDone: {artifact}")
    print("Try it on a Linux host: harnless --help")
    return 0


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
    parser.add_argument("--target", metavar="OS",
                        help="cross-build for OS in a container using the "
                             "'harnless-builder' image (currently: linux)")
    args = parser.parse_args()

    if args.target:
        if args.target != "linux":
            print(f"error: unsupported target '{args.target}' "
                  "(only 'linux' is supported)", file=sys.stderr)
            return 1
        extra = []
        if args.onedir:
            extra.append("--onedir")
        if args.lto:
            extra.append("--lto")
        return build_in_container(extra)

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
    exe = "harnless.exe" if platform == "windows" else "harnless"
    cmd = [
        sys.executable, "-m", "nuitka",
        f"--{mode}",
        f"--output-dir={DIST}",
        "--remove-output",
        "--assume-yes-for-downloads",
        # Pin the artifact name: Nuitka 4.x otherwise emits '<name>.bin' on
        # Linux, which would not match the expected 'harnless'.
        f"--output-filename={exe}",
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

    artifact = DIST / "harnless" / exe if args.onedir else DIST / exe
    if not artifact.exists():
        print(f"error: build reported success but {artifact} is missing", file=sys.stderr)
        return 1

    print(f"\nDone: {artifact}")
    print(f"Try it: {artifact} --help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
