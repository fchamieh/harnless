# harnless-builder: pre-baked toolchain for cross-building harnless on Linux.
#
# Built on manylinux2014 (glibc 2.17) so the resulting binary runs on any
# modern Linux: Ubuntu 20.04+, Debian 10+, RHEL/CentOS 7+, Fedora, Arch, ...
# (A binary's minimum glibc is set by the build environment's glibc, so an old
# base = a portable binary.)
#
# Build the image (from the repo root):
#   podman build -t harnless-builder .
#   (or: docker build -t harnless-builder .)
#
# Use it (from the repo root):
#   python build.py --target linux
#   (or manually: podman run --rm -v ${PWD}:/src -w /src harnless-builder python build.py)
#
# The image ships Python 3.12 + Nuitka + SCons + gcc + patchelf, so container
# runs skip the pip install and compiler download entirely.

FROM quay.io/pypa/manylinux2014_x86_64

# Use the bundled Python 3.12 (this is the interpreter that gets frozen into
# the binary). gcc (devtoolset-10) and patchelf are already in the image.
ENV PATH=/opt/python/cp312-cp312/bin:$PATH

# Nuitka with the [onefile] extra (pulls in zstandard so onefile binaries are
# compressed; without it Nuitka warns and emits an uncompressed, larger binary).
# scons: a complete SCons, which build.py makes Nuitka use on Linux (see below).
RUN python -m pip install --no-cache-dir "nuitka[onefile]" scons

# Nuitka 4.3+ bundles a *stripped* SCons that is missing the 'ilink' tool module
# SCons probes on Linux (its linker search order is ['gnulink', 'ilink']). When
# the gnulink probe fails, the lookup of the missing module aborts the C build
# with: scons: *** No tool module 'ilink' found  (Nuitka issue #3734).
# Guard 1: give the probe a module that politely declines. (Guard 2 is in
# build.py: --experimental=force-system-scons makes Nuitka use the full SCons.)
RUN for tool_dir in /opt/python/cp312-cp312/lib/python3.12/site-packages/nuitka/build/inline_copy/lib/scons-*/SCons/Tool; do \
        [ -f "$tool_dir/ilink.py" ] || printf '"""Nuitka strips the real ilink (OS/2 linker) tool; this stub keeps the SCons linker probe from crashing."""\ndef exists(env):\n    return False\n\ndef generate(env):\n    pass\n' > "$tool_dir/ilink.py"; \
    done

# manylinux ships Python without shared libraries; Nuitka needs the static
# libpython to embed the interpreter. It's bundled in the image, just not
# extracted yet.
RUN cd /opt/_internal && tar xf static-libs-for-embedding-only.tar.xz

# Warm Nuitka's toolchain and prove it works end-to-end with a throwaway
# standalone build (the same mode build.py uses, including the system-SCons
# guard). Fails the image build if the toolchain is broken, instead of failing
# at first use.
RUN printf 'print("ok")\n' > /tmp/warmup.py \
    && python -m nuitka --standalone --output-dir=/tmp/warmup --remove-output \
        --experimental=force-system-scons /tmp/warmup.py \
    && rm -rf /tmp/warmup /tmp/warmup.py

WORKDIR /src
