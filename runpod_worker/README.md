# BeatSync Engine — RunPod Serverless Worker

Runs the real C++ beat-sync engine on RunPod (Linux) so the **DJ effects + real
beat detection** work in production. Railway stays the API; it sends each
beat-sync job here and the worker returns the finished montage URL.

```
App → Railway /beat_sync → RunPod worker (engine) → montage URL → Railway → app
```

The worker is only used when `RUNPOD_ENDPOINT_ID` + `RUNPOD_API_KEY` are set on
Railway. Until then, beat-sync falls back to the local engine (macOS dev) and
then the Python/ffmpeg path — nothing breaks.

> **Compute:** CPU is enough for today's features (AudioFlux beat detection +
> ffmpeg effects). GPU/ONNX (kick-only/psytrance) is a later add — see bottom.

---

## Phase A — build the Linux engine once

The engine ships as macOS `.dylib` only; build the Linux `.so` once on an Ubuntu
22.04 box (a RunPod **CPU pod** with your credits works well).

```bash
# On the pod:
git clone <your engine repo>            # tripsitters_audio_beatsync_GUI
git clone <this backend repo>           # for runpod_worker/
cd <backend>/runpod_worker
ENGINE_SRC=/workspace/tripsitters_audio_beatsync_GUI ./build_engine.sh
# → produces engine_libs/libbeatsync_backend_shared.so + libaudioflux.so
```

⚠️ **Expected iteration point:** CMake's ffmpeg discovery on Linux. The macOS
build used homebrew ffmpeg; `build_engine.sh` passes `FFMPEG_ROOT=/usr` for the
apt `-dev` packages. If configure can't find the `avcodec/avformat/...` targets,
check the engine repo's ffmpeg find logic and adjust `FFMPEG_ROOT` /
`CMAKE_PREFIX_PATH`. This is the main thing to get right; everything else is wired.

Verify it loads before building the image:

```bash
ldd engine_libs/libbeatsync_backend_shared.so   # no "not found" lines
```

## Phase B — build & deploy the worker image

```bash
cd runpod_worker
cp ../app/services/beatsync_engine.py .         # reuse the exact binding
docker build -t <registry>/beatsync-worker:latest .
docker push <registry>/beatsync-worker:latest
```

Then in the **RunPod console → Serverless → New Endpoint**:
- Container image: `<registry>/beatsync-worker:latest`
- Worker type: **CPU** (e.g. 4 vCPU / 8 GB)
- Min workers `0` (scale to zero), Max workers `1–2`
- Env var: `RESULT_UPLOAD_URL = https://<your-railway-domain>/upload`

(Or skip Docker Hub and point RunPod's GitHub integration at this folder so RunPod
builds the image — same Dockerfile.)

## Phase C — point Railway at it

Set on the Railway service:
```
RUNPOD_ENDPOINT_ID = <the endpoint id from the RunPod console>
RUNPOD_KEY         = <a RunPod API key>   # RUNPOD_API_KEY also accepted
```
Redeploy. Next beat-sync job goes to the worker; the engine effects render for real.

---

## Job contract

Railway → worker (`{"input": {...}}`):

| field | meaning |
|---|---|
| `clip_urls` | ordered clips to cut between (must be URL-reachable) |
| `song_url` | `${PUBLIC_BASE_URL}/songs/<file>` |
| `bpm` | BPM hint (0 = auto) |
| `start_sec` | user-chosen montage start, or null |
| `intro_skip` | fallback start when `start_sec` is null |
| `montage_seconds` | window length (`MONTAGE_SECONDS`) |
| `effects` | DJ panel: enabled/flash_intensity/zoom_intensity/transition/color_preset/beat_divisor |

Worker → Railway: `{"url": "<montage url>"}` or `{"error": "..."}`.

## Notes / future

- **Durability:** results are published via Railway's `/upload` (served from its
  disk) to reuse existing infra. For permanence, switch `_upload_result` in
  `handler.py` to Supabase Storage.
- **Kick-only / psytrance (GPU):** needs the engine's ONNX path bound
  (`bs_ai_analyze_*` + the `.onnx` models) and a GPU endpoint. Rebuild the `.so`
  with `-DUSE_ONNX=ON` against onnxruntime, ship the models, and add a
  `kick_only` field to the job + the binding.
