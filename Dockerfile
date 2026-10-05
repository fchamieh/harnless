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
# The image ships Python 3.12 + Nuitka + gcc + patchelf, so container runs skip
# the pip install and compiler download entirely.

FROM quay.io/pypa/manylinux2014_x86_64

# Use the bundled Python 3.12 (this is the interpreter that gets frozen into
# the binary). gcc (devtoolset-10) and patchelf are already in the image.
ENV PATH=/opt/python/cp312-cp312/bin:$PATH

# Nuitka with the [onefile] extra (pulls in zstandard so onefile binaries are
# compressed; without it Nuitka warns and emits an uncompressed, larger binary).
RUN python -m pip install --no-cache-dir "nuitka[onefile]"

# manylinux ships Python without shared libraries; Nuitka needs the static
# libpython to embed the interpreter. It's bundled in the image, just not
# extracted yet.
RUN cd /opt/_internal && tar xf static-libs-for-embedding-only.tar.xz

# Warm Nuitka's toolchain and prove it works end-to-end with a throwaway
# standalone build (the same mode build.py uses). Fails the image build if the
# toolchain is broken, instead of failing at first use.
RUN printf 'print("ok")\n' > /tmp/warmup.py \
    && python -m nuitka --standalone --output-dir=/tmp/warmup --remove-output /tmp/warmup.py \
    && rm -rf /tmp/warmup /tmp/warmup.py

WORKDIR /src
