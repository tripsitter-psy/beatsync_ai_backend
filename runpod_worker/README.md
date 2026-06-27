# BeatSync Engine — RunPod Serverless Worker

Runs the real C++ beat-sync engine on RunPod (Linux) so the **DJ effects + real
beat detection** work in production. Railway stays the API; it sends each
beat-sync job here and the worker returns the finished montage URL.

```
App → Railway /beat_sync → RunPod worker (engine) → montage URL → Railway → app
```

Only used when `RUNPOD_ENDPOINT_ID` + `RUNPOD_KEY` are set on Railway. Until
then, beat-sync falls back to the local engine (macOS dev) and then the
Python/ffmpeg path — nothing breaks.

The `Dockerfile` **compiles the engine itself** (multi-stage, Ubuntu 24.04) from
the engine repo's `imgui-port` branch, so build- and run-time ffmpeg match. No
prebuilt `.so` to ship. CPU is enough — AudioFlux beat detection + ffmpeg effects
are CPU-only. (GPU/ONNX kick-only is a later add; see bottom.)

---

## Phase A — build the image

**Option 1 — RunPod GitHub integration (no local Docker):**
RunPod console → Serverless → New Endpoint → *Deploy from GitHub*. Point it at the
**backend repo**, Dockerfile path `runpod_worker/Dockerfile`. RunPod builds it
(clones the engine `imgui-port` branch + AudioFlux, compiles, ~several min).

**Option 2 — local Docker → registry:**
```bash
docker build -t <registry>/beatsync-worker:latest runpod_worker/
docker push <registry>/beatsync-worker:latest
```

Prereqs: both repos pushed to GitHub, and the engine `imgui-port` branch present
(it is). The build needs no secrets — engine + AudioFlux are public.

## Phase B — create the endpoint

In the RunPod endpoint settings:
- Worker type: **CPU** (e.g. 4 vCPU / 8 GB)
- Min workers `0` (scale to zero), Max `1–2`
- Env var: `RESULT_UPLOAD_URL = https://<your-railway-domain>/upload`

## Phase C — point Railway at it

```
RUNPOD_ENDPOINT_ID = <endpoint id from the console>
RUNPOD_KEY         = <RunPod API key>        # already set
```
Redeploy Railway. Next beat-sync job runs on the engine for real.

---

## Job contract

Railway → worker (`{"input": {...}}`):

| field | meaning |
|---|---|
| `clip_urls` | ordered clips to cut between (URL-reachable) |
| `song_url` | `${PUBLIC_BASE_URL}/songs/<file>` |
| `bpm` | BPM hint (0 = auto) |
| `start_sec` | user-chosen montage start, or null |
| `intro_skip` | fallback start when `start_sec` is null |
| `montage_seconds` | window length (`MONTAGE_SECONDS`) |
| `effects` | DJ panel: enabled/flash_intensity/zoom_intensity/transition/color_preset/beat_divisor |

Worker → Railway: `{"url": "<montage url>"}` or `{"error": "..."}`.

## Notes / future

- `beatsync_engine.py` here is **vendored** from `app/services/beatsync_engine.py`
  — keep them in sync if you change the binding.
- `build_engine.sh` is a standalone "build the `.so` on a bare Ubuntu box"
  helper; the Dockerfile is the primary path and supersedes it.
- **Durability:** results are published via Railway's `/upload`. For permanence,
  switch `_upload_result` in `handler.py` to Supabase Storage.
- **Kick-only / psytrance (GPU):** needs the engine's ONNX path (`bs_ai_analyze_*`
  + the `.onnx` models) and a GPU endpoint. Rebuild with `-DUSE_ONNX=ON` against
  onnxruntime, ship the models, add a `kick_only` job field + binding.

## Engine build gotchas (learned the hard way)

- Use the **`imgui-port`** branch — `main-new` is stale and won't compile.
- Engine needs **ffmpeg ≥ 5.1** (new `AVChannelLayout` API). Ubuntu 24.04 (6.1) is
  fine; 20.04/22.04 are not.
- AudioFlux: the repo has neither the Linux `.so` nor C headers — the Dockerfile
  fetches the `.so` from the pip wheel and headers from the AudioFlux repo (0.1.9).
