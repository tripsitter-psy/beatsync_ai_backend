# BeatSync engine — RunPod Serverless worker (CPU), self-building.
#
# Multi-stage: stage 1 compiles the Linux engine from source on Ubuntu 24.04
# (ffmpeg 6.1, cmake 3.28); stage 2 is the slim runtime on the SAME base so the
# engine's ffmpeg linkage matches at runtime. Build context = this directory.
#
# Build via RunPod's GitHub integration (point it at this Dockerfile) or:
#   docker build -t <registry>/beatsync-worker:latest runpod_worker/

# ──────────────────────────── builder ────────────────────────────
FROM ubuntu:24.04 AS builder
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake ninja-build git pkg-config python3 python3-pip \
        libavcodec-dev libavformat-dev libavutil-dev \
        libswresample-dev libswscale-dev libavfilter-dev \
        libsamplerate0-dev libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Engine source — the imgui-port branch is the one that compiles.
ARG ENGINE_REPO=https://github.com/tripsitter-psy/tripsitters_audio_beatsync_GUI.git
ARG ENGINE_BRANCH=imgui-port
RUN git clone --depth 1 --branch ${ENGINE_BRANCH} ${ENGINE_REPO} engine

# AudioFlux: the engine's beat detector. The repo ships neither the Linux .so nor
# the C headers, so fetch both at the version that matches (0.1.9): the prebuilt
# native lib from the pip wheel, and the headers from the AudioFlux repo tag.
RUN set -eux; \
    mkdir -p /tmp/af engine/third_party/audioflux/lib engine/third_party/audioflux/include; \
    pip3 download audioflux==0.1.9 --no-deps --only-binary=:all: \
        --platform manylinux2014_x86_64 --python-version 38 -d /tmp/af; \
    cd /tmp/af; for w in *.whl; do python3 -c "import zipfile,sys;zipfile.ZipFile(sys.argv[1]).extractall('x')" "$w"; done; \
    cp "$(find /tmp/af -name 'libaudioflux*.so*' | head -1)" /build/engine/third_party/audioflux/lib/libaudioflux.so; \
    git clone --depth 1 --branch v0.1.9 https://github.com/libAudioFlux/audioFlux.git /tmp/afsrc; \
    cp -r /tmp/afsrc/include/* /build/engine/third_party/audioflux/include/

# Configure + build only the shared backend lib (no GUI, no ONNX on the CPU path).
RUN cmake -S engine -B engine/build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DBEATSYNC_BUILD_SHARED=ON \
        -DUSE_ONNX=OFF \
        -DUSE_AUDIOFLUX=ON \
        -DAUDIOFLUX_ROOT=/build/engine/third_party/audioflux \
        -DFFMPEG_ROOT=/usr \
        -DOPTION_REQUIRE_TRIPSITTER=OFF \
 && cmake --build engine/build --target beatsync_backend_shared -j"$(nproc)"

# Ship the engine .so, libaudioflux.so, and AudioFlux's bundled MKL+OpenMP libs
# (the wheel keeps them in audioflux/lib/lib/ — libaudioflux.so needs them at runtime).
RUN mkdir -p /out/lib \
 && cp "$(find engine/build -name 'libbeatsync_backend_shared.so' | head -1)" /out/ \
 && cp engine/third_party/audioflux/lib/libaudioflux.so /out/ \
 && cp -r /tmp/af/x/audioflux/lib/lib/. /out/lib/ \
 && ldd /out/libbeatsync_backend_shared.so || true

# ──────────────────────────── runtime ────────────────────────────
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# Same base → ffmpeg 6.x runtime libs match what the engine was built against.
# libfftw3-single3 = libfftw3f.so.3 that AudioFlux needs at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ffmpeg \
        libsamplerate0 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /out/ /opt/beatsync/
# Engine loads libaudioflux.so, which needs its bundled MKL+OpenMP libs in ./lib.
ENV BEATSYNC_DYLIB_DIR=/opt/beatsync
ENV LD_LIBRARY_PATH=/opt/beatsync:/opt/beatsync/lib

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# beatsync_engine.py is vendored here (kept in sync with app/services/).
COPY beatsync_engine.py handler.py ./

CMD ["python3", "-u", "handler.py"]
