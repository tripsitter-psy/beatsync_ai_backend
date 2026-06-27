#!/usr/bin/env bash
# Build the Linux BeatSync engine ONCE (CPU path), producing:
#   engine_libs/libbeatsync_backend_shared.so
#   engine_libs/libaudioflux.so
#
# Run on Ubuntu 22.04 (e.g. a RunPod CPU pod). Point ENGINE_SRC at a checkout of
# the desktop engine repo (tripsitters_audio_beatsync_GUI). Then copy engine_libs/
# next to the Dockerfile and build the worker image.
#
#   ENGINE_SRC=/workspace/tripsitters_audio_beatsync_GUI ./build_engine.sh
#
# NOTE: the #1 iteration point is how CMake locates ffmpeg on Linux. The macOS
# build set FFMPEG_ROOT=/opt/homebrew/opt/ffmpeg; on Ubuntu the apt -dev packages
# install into /usr, so we pass FFMPEG_ROOT=/usr. If configure can't find the
# avcodec/avformat targets, check the repo's ffmpeg discovery (FindFFmpeg / pkg-config)
# and adjust FFMPEG_ROOT or add -DCMAKE_PREFIX_PATH accordingly.
set -euo pipefail

ENGINE_SRC="${ENGINE_SRC:-/workspace/tripsitters_audio_beatsync_GUI}"
OUT="${OUT:-$(pwd)/engine_libs}"
BUILD_DIR="$ENGINE_SRC/build-linux"

echo ">> Installing build + runtime deps"
apt-get update
apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build git pkg-config python3-pip \
    libavcodec-dev libavformat-dev libavutil-dev \
    libswresample-dev libswscale-dev libavfilter-dev \
    libsamplerate0-dev libgomp1

echo ">> Fetching a Linux libaudioflux.so (vendored lib dir is macOS-only)"
pip3 install --no-cache-dir audioflux
AF_DIR="$(python3 -c 'import audioflux, os; print(os.path.dirname(audioflux.__file__))')"
mkdir -p "$ENGINE_SRC/third_party/audioflux/lib"
AF_SO="$(find "$AF_DIR" -name 'libaudioflux*.so*' | head -n1 || true)"
if [ -z "$AF_SO" ]; then
    echo "!! Could not find libaudioflux*.so in the pip wheel ($AF_DIR)"; exit 1
fi
cp "$AF_SO" "$ENGINE_SRC/third_party/audioflux/lib/libaudioflux.so"

echo ">> Configuring (shared backend lib only; no GUI, no ONNX for the CPU path)"
cmake -S "$ENGINE_SRC" -B "$BUILD_DIR" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DBEATSYNC_BUILD_SHARED=ON \
    -DUSE_ONNX=OFF \
    -DUSE_AUDIOFLUX=ON \
    -DAUDIOFLUX_ROOT="$ENGINE_SRC/third_party/audioflux" \
    -DFFMPEG_ROOT=/usr \
    -DOPTION_REQUIRE_TRIPSITTER=OFF

echo ">> Building beatsync_backend_shared"
cmake --build "$BUILD_DIR" --target beatsync_backend_shared -j"$(nproc)"

echo ">> Collecting artifacts into $OUT"
mkdir -p "$OUT"
find "$BUILD_DIR" -name 'libbeatsync_backend_shared.so' -exec cp {} "$OUT/" \;
cp "$ENGINE_SRC/third_party/audioflux/lib/libaudioflux.so" "$OUT/"

echo ">> Done. Linkage:"
ldd "$OUT/libbeatsync_backend_shared.so" || true
ls -la "$OUT"
