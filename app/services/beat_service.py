import asyncio
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

# Deterministic BPM library — no audio analysis needed.
# Keys are song titles (matching the frontend's song picker).
SONG_LIBRARY = {
    "Neon Drift": 110,
    "Cybernetics": 140,
    "Galactic Bounce": 128,
    "Future Seoul": 95,
    "Hyperdrive": 150,
}

DEFAULT_BPM = 120
MONTAGE_DURATION_SECS = 30.0  # target montage length


async def analyze_audio_beats(song_id: str, clip_urls: List[str]) -> Dict[str, any]:
    """
    Builds a deterministic beat grid from a known BPM for *song_id*.

    The grid covers ~30 s of output so the montage is concise for social
    sharing.  Each entry in ``beats`` is the timestamp (in seconds) of a
    beat hit; the gap between consecutive entries equals ``beat_interval``.

    Parameters
    ----------
    song_id : str
        Song title (looked up in ``SONG_LIBRARY``; defaults to 120 BPM).
    clip_urls : List[str]
        AI-generated clip URLs — passed through for context but not used
        for beat detection.

    Returns
    -------
    dict
        ``{"bpm": int, "beats": List[float], "beat_interval": float}``
    """
    bpm = SONG_LIBRARY.get(song_id, DEFAULT_BPM)
    beat_interval = 60.0 / bpm

    logger.info(
        f"Building beat grid for '{song_id}' — {bpm} BPM, "
        f"interval={beat_interval:.4f}s, target={MONTAGE_DURATION_SECS}s"
    )

    # Build evenly-spaced beat timestamps from 0 up to the target duration.
    beats: List[float] = []
    t = 0.0
    while t < MONTAGE_DURATION_SECS:
        beats.append(round(t, 4))
        t += beat_interval

    logger.info(f"Beat grid: {len(beats)} beats over {beats[-1]:.2f}s")

    return {
        "bpm": bpm,
        "beats": beats,
        "beat_interval": round(beat_interval, 4),
    }
