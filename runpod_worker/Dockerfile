# BeatSync engine — RunPod Serverless worker (CPU).
#
# Ships a PREBUILT Linux engine (libbeatsync_backend_shared.so + libaudioflux.so).
# Build those once with build_engine.sh and drop them in ./engine_libs before
# building this image (keeps the slow C++ compile out of every image build).
FROM python:3.11-slim

# Runtime deps: ffmpeg shared libs (engine links avcodec/avformat/...), the
# resampler, and OpenMP for AudioFlux. ffmpeg CLI is handy for the Python fallback.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libavcodec-dev libavformat-dev libavutil-dev \
        libswresample-dev libswscale-dev libavfilter-dev \
        libsamplerate0 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Prebuilt engine libraries (see build_engine.sh).
COPY engine_libs/ /opt/beatsync/
ENV BEATSYNC_DYLIB_DIR=/opt/beatsync

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Reuse the exact binding the main backend uses. Copy it in at image-build time:
#   cp ../app/services/beatsync_engine.py .
COPY beatsync_engine.py handler.py ./

CMD ["python", "-u", "handler.py"]
