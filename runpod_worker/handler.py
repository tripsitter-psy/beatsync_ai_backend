"""
RunPod Serverless handler for the BeatSync C++ engine.

Railway's `beat_sync_pipeline` POSTs a montage job here; this worker downloads
the clips + song, runs real beat detection + the multi-clip beat cut + effects
+ audio mux via the engine, uploads the finished montage, and returns its URL.

The image ships a prebuilt Linux engine (see build_engine.sh) at
BEATSYNC_DYLIB_DIR; `beatsync_engine` is the same binding the main backend uses.

Input (job["input"]):
    clip_urls       list[str]   ordered clips to cut between
    song_url        str         fetchable audio URL (library track or upload)
    bpm             float       BPM hint (0 = auto)
    start_sec       float|None  user-chosen montage start; None = intro skip
    intro_skip      float       fallback start when start_sec is None
    montage_seconds float       montage window length
    effects         dict|None   DJ-style effects (flash/zoom/transition/...)

Output: {"url": "<montage url>"} or {"error": "..."}.
"""
import os
import logging
import tempfile
import shutil

import requests
import runpod

import beatsync_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("beatsync_worker")

# Where the worker POSTs the finished montage to get back a served URL. Defaults
# to the main backend's /upload endpoint so results are served the same way as
# every other render (set RESULT_UPLOAD_URL = ${PUBLIC_BASE_URL}/upload).
RESULT_UPLOAD_URL = os.environ.get("RESULT_UPLOAD_URL", "")


def _download(url: str, dest: str) -> bool:
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        return os.path.getsize(dest) > 0
    except Exception as e:
        logger.error(f"download failed {url}: {e}")
        return False


def _upload_result(path: str) -> str | None:
    """POST the montage to the main backend's /upload and return the served URL."""
    if not RESULT_UPLOAD_URL:
        logger.error("RESULT_UPLOAD_URL not set; cannot publish montage")
        return None
    try:
        with open(path, "rb") as f:
            r = requests.post(
                RESULT_UPLOAD_URL,
                files={"file": (os.path.basename(path), f, "video/mp4")},
                timeout=180,
            )
        r.raise_for_status()
        data = r.json()
        # /upload returns the served URL (key may be 'url' or 'file_url').
        return data.get("url") or data.get("file_url") or data.get("video_url")
    except Exception as e:
        logger.error(f"result upload failed: {e}")
        return None


def handler(job):
    if not beatsync_engine.is_available():
        return {"error": "engine unavailable in worker (check BEATSYNC_DYLIB_DIR / .so deps)"}

    inp = job.get("input") or {}
    clip_urls = inp.get("clip_urls") or []
    song_url = inp.get("song_url")
    if len(clip_urls) < 2 or not song_url:
        return {"error": "need >=2 clip_urls and a song_url"}

    bpm = float(inp.get("bpm") or 0)
    start_sec = inp.get("start_sec")
    intro_skip = float(inp.get("intro_skip") or 60)
    montage_seconds = float(inp.get("montage_seconds") or 30)
    effects = inp.get("effects")

    work = tempfile.mkdtemp(prefix="bs_")
    try:
        # 1. Fetch audio + clips locally.
        song_path = os.path.join(work, "song" + os.path.splitext(song_url)[1][:5] or ".m4a")
        if not _download(song_url, song_path):
            return {"error": "could not download song"}
        local_clips = []
        for i, url in enumerate(clip_urls):
            p = os.path.join(work, f"clip_{i:02d}.mp4")
            if _download(url, p):
                local_clips.append(p)
        if len(local_clips) < 2:
            return {"error": "could not download enough clips"}

        # 2. Real beat detection on the actual track.
        grid = beatsync_engine.analyze_audio(song_path, bpm)
        if not grid or not grid.get("beats"):
            return {"error": "beat detection returned no beats"}
        all_beats = grid["beats"]

        # 3. Pick the montage window — user section, else skip the intro to the drop.
        skip = float(start_sec) if start_sec is not None else intro_skip
        start_beat = next((b for b in all_beats if b >= skip), all_beats[0])
        window = [b for b in all_beats if start_beat <= b <= start_beat + montage_seconds]
        beats = [b - start_beat for b in window]  # normalize montage to t=0
        if len(beats) < 2:
            return {"error": "not enough beats in the chosen window"}

        # 4. Cut + effects + audio mux via the engine.
        out_path = os.path.join(work, "montage.mp4")
        result = beatsync_engine.build_montage(
            local_clips, beats, out_path, 0.0, song_path, True, start_beat, effects,
        )
        if not result or not os.path.exists(out_path):
            return {"error": "engine build_montage failed"}

        # 5. Publish and hand the URL back to Railway.
        url = _upload_result(out_path)
        if not url:
            return {"error": "montage built but upload failed"}
        return {"url": url}
    finally:
        shutil.rmtree(work, ignore_errors=True)


runpod.serverless.start({"handler": handler})
