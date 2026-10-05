# harnless-builder: pre-baked toolchain for cross-building harnless on Linux.
#
# Build the image (from the repo root):
#   podman build -t harnless-builder .
#   (or: docker build -t harnless-builder .)
#
# Use it (from the repo root):
#   python build.py --target linux
#   (or manually: podman run --rm -v ${PWD}:/src -w /src harnless-builder python build.py)
#
# The image ships Python + Nuitka + a working C toolchain, so container runs
# skip the pip install and compiler download entirely.

FROM python:3.12

# Nuitka standalone mode on Linux needs patchelf (not in the base image).
# The full (non-slim) python image already includes gcc, which Nuitka uses on
# Linux (no MinGW64 needed off-Windows).
RUN apt-get update \
    && apt-get install -y --no-install-recommends patchelf \
    && rm -rf /var/lib/apt/lists/*

# Nuitka with the [onefile] extra (pulls in zstandard so onefile binaries are
# compressed; without it Nuitka warns and emits an uncompressed, larger binary).
RUN pip install --no-cache-dir "nuitka[onefile]"

# Warm Nuitka's toolchain and prove it works end-to-end with a throwaway
# standalone build (the same mode build.py uses). Fails the image build if the
# toolchain is broken, instead of failing at first use.
RUN printf 'print("ok")\n' > /tmp/warmup.py \
    && python -m nuitka --standalone --output-dir=/tmp/warmup --remove-output /tmp/warmup.py \
    && rm -rf /tmp/warmup /tmp/warmup.py

WORKDIR /src
