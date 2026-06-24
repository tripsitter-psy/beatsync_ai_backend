import asyncio
import logging
import os
import tempfile
import uuid
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://192.168.1.169:8000")

# Target output specs — normalise every segment before concat to avoid
# dimension / framerate mismatches that make ffmpeg concat barf.
OUT_WIDTH = 720
OUT_HEIGHT = 1280
OUT_FPS = 30

# ──────────────────────────────────────────────────────────────────────
# Beat-synced visual effects
# ──────────────────────────────────────────────────────────────────────
# Set MONTAGE_EFFECTS="off" to disable all effects and get plain hard cuts.
# Default is "on".
#
# Effect palette (applied deterministically by beat_idx):
#   0 — Zoom punch:       Quick 1.12x scale-in at segment start, eases to 1.0.
#   1 — Brightness flash: Brief exposure pop on the cut frame that decays.
#   2 — Hue pulse:        Trippy hue rotation for the "Trip Sitter" vibe.
#   3 — RGB shift:        Chromatic aberration — red/blue channel offset.
#   4 — Dip-to-black:     Fast fade-in from black at the segment head.
#
# If any effect filter causes ffmpeg to fail, the segment is re-extracted
# without effects (plain hard cut) so the montage never breaks.
# ──────────────────────────────────────────────────────────────────────

MONTAGE_EFFECTS = os.environ.get("MONTAGE_EFFECTS", "on").lower()
NUM_EFFECT_TYPES = 5


def _effect_filter_for_beat(beat_idx: int, seg_duration: float) -> Optional[str]:
    """
    Return an ffmpeg filter expression for the given beat index.

    The effect type cycles deterministically: ``beat_idx % NUM_EFFECT_TYPES``.
    *seg_duration* is used to time the easing/decay so effects fit the segment.
    Returns ``None`` when effects are disabled.
    """
    if MONTAGE_EFFECTS != "on":
        return None

    # Number of frames in this segment (for expression-based filters)
    n_frames = max(int(seg_duration * OUT_FPS), 1)
    effect_type = beat_idx % NUM_EFFECT_TYPES

    if effect_type == 0:
        # ── Zoom punch ────────────────────────────────────────────
        # Start at 1.12x zoom, ease back to 1.0 over the segment.
        # Uses zoompan: z starts at 1.12 and linearly decays to 1.0.
        # zoompan outputs at its own size, so we re-scale afterwards.
        return (
            f"zoompan=z='1.12-0.12*on/{n_frames}':"
            f"d={n_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"s={OUT_WIDTH}x{OUT_HEIGHT}:fps={OUT_FPS}"
        )

    elif effect_type == 1:
        # ── Brightness flash ──────────────────────────────────────
        # Pop brightness to 1.5x on frame 0, decay back to 1.0.
        # Uses the eq filter with a time-varying brightness expression.
        # eq brightness range is -1.0 to 1.0 (additive).
        decay_frames = min(n_frames, 8)  # flash lasts ~8 frames (~0.27s)
        return (
            f"eq=brightness='if(lt(n,{decay_frames}),"
            f"0.4*(1-n/{decay_frames}),0)'"
        )

    elif effect_type == 2:
        # ── Hue pulse ─────────────────────────────────────────────
        # Rotate hue by 60° at the start, ease back to 0 over the segment.
        # hue filter h= is in degrees.
        return (
            f"hue=h='60*(1-min(n/{n_frames},1))':s=1.2"
        )

    elif effect_type == 3:
        # ── RGB / chromatic shift ─────────────────────────────────
        # Offset red and blue channels horizontally for a glitchy vibe.
        # rgbashift rh/bh are in pixels; decay over first 6 frames.
        shift_frames = min(n_frames, 6)
        return (
            f"rgbashift=rh='if(lt(n,{shift_frames}),"
            f"8*(1-n/{shift_frames}),0)':"
            f"bh='if(lt(n,{shift_frames}),"
            f"-8*(1-n/{shift_frames}),0)'"
        )

    elif effect_type == 4:
        # ── Dip-to-black (fade in) ────────────────────────────────
        # Quick fade-in from black over the first ~6 frames.
        fade_frames = min(n_frames, 6)
        return f"fade=t=in:st=0:d={fade_frames / OUT_FPS:.4f}"

    return None


async def _download_clip(client: httpx.AsyncClient, url: str, dest: str) -> bool:
    """Download a clip from a URL to a local path. Returns True on success."""
    try:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 64):
                    f.write(chunk)
        logger.info(f"Downloaded {url} -> {dest}")
        return True
    except Exception as e:
        logger.error(f"Failed to download {url}: {e}")
        return False


async def _probe_duration(path: str) -> float:
    """Return the duration of a video file in seconds via ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except (ValueError, AttributeError):
        logger.warning(f"Could not probe duration of {path}, defaulting to 5.0s")
        return 5.0


async def _extract_segment(
    src: str,
    start: float,
    duration: float,
    dest: str,
    effect_filter: Optional[str] = None,
) -> bool:
    """
    Extract a segment from *src* starting at *start* for *duration* seconds,
    normalised to 720×1280 @ 30 fps.  Optionally applies *effect_filter*
    after normalisation.  Returns True on success.
    """
    # Base normalisation filter chain
    base_vf = (
        f"scale={OUT_WIDTH}:{OUT_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={OUT_WIDTH}:{OUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )

    # Append effect filter if provided
    if effect_filter:
        vf = f"{base_vf},{effect_filter}"
    else:
        vf = base_vf

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-ss", f"{start:.4f}",
        "-i", src,
        "-t", f"{duration:.4f}",
        "-vf", vf,
        "-r", str(OUT_FPS),
        "-an",  # strip audio for now — will mux song track later
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        dest,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error(
            f"ffmpeg extract failed ({src} @ {start:.2f}s, "
            f"effect={'yes' if effect_filter else 'no'}): "
            f"{stderr.decode()[-300:]}"
        )
        return False
    return True


async def compose_final_video(
    job_id: str, clip_urls: List[str], beat_data: dict, song_id: str
) -> str:
    """
    Stage 3 of the pipeline: FFmpeg beat-cut montage.

    Takes the user's selected AI clips and the beat timestamps from Stage 1,
    cuts BETWEEN the clips on each beat (rapid music-video montage), and
    concatenates the segments into a single output video.

    Clips are CYCLED with modulo to fill the entire beat grid regardless of
    how many clips exist.  On each replay of a clip a different in-clip
    offset is used so repeats read as new angles.  Adjacent beats never use
    the same clip when ``len(clip_urls) > 1``.

    Beat-synced visual effects (zoom punch, brightness flash, hue pulse,
    RGB shift, dip-to-black) are applied per-segment when MONTAGE_EFFECTS
    is "on" (default).  If an effect causes ffmpeg to error, the segment is
    re-extracted without effects so the montage never breaks.

    TODO: Mux the real song audio track once song audio files are available.
          Currently the output is video-only (silent).
    """
    if not clip_urls:
        logger.error("compose_final_video called with no clip_urls")
        return ""

    beats: List[float] = beat_data.get("beats", [])
    if len(beats) < 2:
        logger.warning("Not enough beats for a montage, returning first clip")
        return clip_urls[0]

    output_path = f"uploads/{job_id}_montage.mp4"
    os.makedirs("uploads", exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="beatsync_") as tmp:
            # ── 1. Download all clips ──────────────────────────────
            local_clips: List[str] = []
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                for i, url in enumerate(clip_urls):
                    dest = os.path.join(tmp, f"clip_{i}.mp4")
                    ok = await _download_clip(client, url, dest)
                    if ok:
                        local_clips.append(dest)

            if not local_clips:
                logger.error("All clip downloads failed, returning first URL as fallback")
                return clip_urls[0]

            # ── 2. Probe durations ─────────────────────────────────
            durations = {}
            for path in local_clips:
                durations[path] = await _probe_duration(path)

            # Track how many times each clip has been used so we can
            # advance the in-clip offset on each replay.
            replay_count = {path: 0 for path in local_clips}

            # ── 3. Cut one segment per beat interval ───────────────
            segment_paths: List[str] = []
            n_clips = len(local_clips)
            prev_clip_idx = -1  # for adjacency avoidance

            for beat_idx in range(len(beats) - 1):
                seg_start_time = beats[beat_idx]
                seg_duration = beats[beat_idx + 1] - seg_start_time
                if seg_duration <= 0:
                    continue

                # Pick clip index — cycle with modulo, skip adjacency
                clip_idx = beat_idx % n_clips
                if n_clips > 1 and clip_idx == prev_clip_idx:
                    clip_idx = (clip_idx + 1) % n_clips
                prev_clip_idx = clip_idx

                clip_path = local_clips[clip_idx]
                clip_dur = durations[clip_path]

                # Compute in-clip offset: advance by replay_count so
                # each time we revisit a clip we get a different part.
                replays = replay_count[clip_path]
                replay_count[clip_path] = replays + 1

                # Offset = replays * seg_duration, wrapped within clip length
                raw_offset = replays * seg_duration
                if clip_dur > seg_duration:
                    in_clip_start = raw_offset % (clip_dur - seg_duration)
                else:
                    in_clip_start = 0.0  # clip shorter than segment — start at 0

                seg_out = os.path.join(tmp, f"seg_{beat_idx:04d}.mp4")

                # Determine the effect for this beat
                effect = _effect_filter_for_beat(beat_idx, seg_duration)

                # Try with effects first; fall back to plain on failure
                ok = await _extract_segment(
                    clip_path, in_clip_start, seg_duration, seg_out,
                    effect_filter=effect,
                )
                if not ok and effect is not None:
                    logger.warning(
                        f"Effect failed for beat {beat_idx}, retrying plain"
                    )
                    ok = await _extract_segment(
                        clip_path, in_clip_start, seg_duration, seg_out,
                        effect_filter=None,
                    )

                if ok and os.path.exists(seg_out):
                    segment_paths.append(seg_out)

            if not segment_paths:
                logger.error("No segments extracted, returning first clip as fallback")
                return clip_urls[0]

            # ── 4. Concat all segments ─────────────────────────────
            concat_list = os.path.join(tmp, "concat.txt")
            with open(concat_list, "w") as f:
                for p in segment_paths:
                    f.write(f"file '{p}'\n")

            logger.info(
                f"Concatenating {len(segment_paths)} segments into {output_path} "
                f"(effects={'on' if MONTAGE_EFFECTS == 'on' else 'off'})"
            )

            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_list,
                "-c", "copy",
                "-movflags", "+faststart",
                output_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(f"ffmpeg concat failed: {stderr.decode()[-500:]}")
                return clip_urls[0]

            logger.info(f"Montage complete: {output_path}")
            return f"{PUBLIC_BASE_URL}/{output_path}"

    except Exception as e:
        logger.error(f"compose_final_video error: {e}", exc_info=True)
        return clip_urls[0]
