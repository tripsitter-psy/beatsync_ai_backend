"""
ctypes binding to the real BeatSync C++ engine (libbeatsync_backend_shared.dylib).

This is the engine the app was always designed around (see beatsync_capi.h). It
provides real audio beat detection (AudioFlux/ONNX) + multi-clip beat cutting +
audio muxing + beat-synced effects — replacing the Python/ffmpeg stand-in in
beat_service.py / ffmpeg_service.py for the beat-sync stage.

If the dylib can't be loaded (wrong platform, missing build), `is_available()`
returns False and callers fall back to the Python path. Nothing here raises at
import time.
"""
import os
import ctypes
import logging

logger = logging.getLogger(__name__)

# Where the built engine lives. Override with BEATSYNC_DYLIB_DIR for other hosts.
_DEFAULT_DIR = "/Users/tripsitter/Documents/tripsitters_audio_beatsync_GUI/build-mac"
_ENGINE_DIR = os.environ.get("BEATSYNC_DYLIB_DIR", _DEFAULT_DIR)
_LIB_NAME = "libbeatsync_backend_shared.dylib"


class _BeatGrid(ctypes.Structure):
    _fields_ = [
        ("beats", ctypes.POINTER(ctypes.c_double)),
        ("count", ctypes.c_size_t),
        ("bpm", ctypes.c_double),
        ("duration", ctypes.c_double),
    ]


class _EffectsConfig(ctypes.Structure):
    _fields_ = [
        ("enableTransitions", ctypes.c_int),
        ("transitionType", ctypes.c_char_p),
        ("transitionDuration", ctypes.c_double),
        ("enableColorGrade", ctypes.c_int),
        ("colorPreset", ctypes.c_char_p),
        ("enableVignette", ctypes.c_int),
        ("vignetteStrength", ctypes.c_double),
        ("enableBeatFlash", ctypes.c_int),
        ("flashIntensity", ctypes.c_double),
        ("enableBeatZoom", ctypes.c_int),
        ("zoomIntensity", ctypes.c_double),
        ("effectBeatDivisor", ctypes.c_int),
        ("effectStartTime", ctypes.c_double),
        ("effectEndTime", ctypes.c_double),
    ]


def _load_library():
    """Load the engine dylib + its sibling deps. Returns the CDLL or None."""
    try:
        # Preload @rpath siblings so the loader resolves them.
        sibling = os.path.join(_ENGINE_DIR, "libaudioflux.dylib")
        if os.path.exists(sibling):
            ctypes.CDLL(sibling, mode=ctypes.RTLD_GLOBAL)
        lib_path = os.path.join(_ENGINE_DIR, _LIB_NAME)
        if not os.path.exists(lib_path):
            logger.warning(f"BeatSync engine not found at {lib_path}; using Python fallback.")
            return None
        lib = ctypes.CDLL(lib_path)
        _bind_signatures(lib)
        if lib.bs_init() != 0:
            logger.warning("bs_init() failed; using Python fallback.")
            return None
        ver = lib.bs_get_version()
        logger.info(f"BeatSync engine loaded: v{ver.decode() if ver else '?'}")
        return lib
    except Exception as e:
        logger.warning(f"Could not load BeatSync engine ({e}); using Python fallback.")
        return None


def _bind_signatures(lib):
    c, p = ctypes, ctypes.POINTER
    lib.bs_get_version.restype = c.c_char_p
    lib.bs_init.restype = c.c_int
    lib.bs_shutdown.restype = None

    lib.bs_create_audio_analyzer.restype = c.c_void_p
    lib.bs_destroy_audio_analyzer.argtypes = [c.c_void_p]
    lib.bs_set_bpm_hint.argtypes = [c.c_void_p, c.c_double]
    lib.bs_analyze_audio.argtypes = [c.c_void_p, c.c_char_p, p(_BeatGrid)]
    lib.bs_analyze_audio.restype = c.c_int
    lib.bs_free_beatgrid.argtypes = [p(_BeatGrid)]
    lib.bs_get_waveform.argtypes = [
        c.c_void_p, c.c_char_p, p(p(c.c_float)), p(c.c_size_t), p(c.c_double)
    ]
    lib.bs_get_waveform.restype = c.c_int
    lib.bs_free_waveform.argtypes = [p(c.c_float)]

    lib.bs_create_video_writer.restype = c.c_void_p
    lib.bs_destroy_video_writer.argtypes = [c.c_void_p]
    lib.bs_video_get_last_error.argtypes = [c.c_void_p]
    lib.bs_video_get_last_error.restype = c.c_char_p
    lib.bs_video_cut_at_beats_multi.argtypes = [
        c.c_void_p, p(c.c_char_p), c.c_size_t,
        p(c.c_double), c.c_size_t, c.c_char_p, c.c_double,
    ]
    lib.bs_video_cut_at_beats_multi.restype = c.c_int
    lib.bs_video_add_audio_track.argtypes = [
        c.c_void_p, c.c_char_p, c.c_char_p, c.c_char_p, c.c_int, c.c_double, c.c_double,
    ]
    lib.bs_video_add_audio_track.restype = c.c_int
    lib.bs_video_set_effects_config.argtypes = [c.c_void_p, p(_EffectsConfig)]
    lib.bs_video_set_effects_config.restype = c.c_int
    lib.bs_video_apply_effects.argtypes = [c.c_void_p, c.c_char_p, c.c_char_p, p(c.c_double), c.c_size_t]
    lib.bs_video_apply_effects.restype = c.c_int


_LIB = _load_library()


def is_available() -> bool:
    return _LIB is not None


def analyze_audio(audio_path: str, bpm_hint: float = 0.0) -> dict | None:
    """Detect beats in an audio file. Returns {bpm, beats, duration} or None."""
    if _LIB is None:
        return None
    analyzer = _LIB.bs_create_audio_analyzer()
    if not analyzer:
        return None
    try:
        if bpm_hint > 0:
            _LIB.bs_set_bpm_hint(analyzer, ctypes.c_double(bpm_hint))
        grid = _BeatGrid()
        rc = _LIB.bs_analyze_audio(analyzer, audio_path.encode(), ctypes.byref(grid))
        if rc != 0:
            err = _LIB.bs_get_analyzer_last_error(analyzer)
            logger.error(f"bs_analyze_audio failed: {err.decode() if err else rc}")
            return None
        beats = [grid.beats[i] for i in range(grid.count)]
        result = {"bpm": grid.bpm, "beats": beats, "duration": grid.duration}
        _LIB.bs_free_beatgrid(ctypes.byref(grid))
        return result
    finally:
        _LIB.bs_destroy_audio_analyzer(analyzer)


def get_waveform(audio_path: str, points: int = 400) -> dict | None:
    """Downsampled, normalized (0..1) waveform peaks for the section scrubber.
    Returns {peaks: [...], duration} or None."""
    if _LIB is None:
        return None
    analyzer = _LIB.bs_create_audio_analyzer()
    if not analyzer:
        return None
    try:
        out_peaks = ctypes.POINTER(ctypes.c_float)()
        out_count = ctypes.c_size_t()
        out_dur = ctypes.c_double()
        rc = _LIB.bs_get_waveform(
            analyzer, audio_path.encode(),
            ctypes.byref(out_peaks), ctypes.byref(out_count), ctypes.byref(out_dur),
        )
        if rc != 0 or not out_peaks:
            return None
        n = out_count.value
        raw = [abs(out_peaks[i]) for i in range(n)]
        _LIB.bs_free_waveform(out_peaks)
        # Downsample to `points` buckets (peak of each bucket) for the UI.
        if n > points > 0:
            step = n / points
            peaks = [max(raw[int(i * step):int((i + 1) * step)] or [0.0]) for i in range(points)]
        else:
            peaks = raw
        mx = max(peaks) or 1.0
        return {"peaks": [round(x / mx, 3) for x in peaks], "duration": out_dur.value}
    finally:
        _LIB.bs_destroy_audio_analyzer(analyzer)


def build_montage(
    clip_paths: list[str],
    beats: list[float],
    output_path: str,
    clip_duration: float = 0.0,
    audio_path: str | None = None,
    effects: bool = True,
    audio_start: float = 0.0,
) -> str | None:
    """
    Cut local clips at the beat times into a montage, optionally apply
    beat-synced effects, and mux the song audio. Returns output_path or None.
    All inputs must be LOCAL file paths (download remote clips first).

    audio_start: seconds into the song to begin the muxed audio — psytrance
    tracks have long dancefloor intros, so the montage starts at the drop.
    """
    if _LIB is None or not clip_paths or len(beats) < 2:
        return None
    # Each cut runs for one beat. If no duration given, derive it from the
    # median gap between beats (0 would make the engine cut empty segments).
    if clip_duration <= 0:
        gaps = sorted(beats[i + 1] - beats[i] for i in range(len(beats) - 1))
        clip_duration = gaps[len(gaps) // 2] if gaps else 0.4
    writer = _LIB.bs_create_video_writer()
    if not writer:
        return None
    tmp_cut = output_path + ".cut.mp4"
    tmp_fx = output_path + ".fx.mp4"
    try:
        arr = (ctypes.c_char_p * len(clip_paths))(*[p.encode() for p in clip_paths])
        beat_arr = (ctypes.c_double * len(beats))(*beats)

        rc = _LIB.bs_video_cut_at_beats_multi(
            writer, arr, len(clip_paths), beat_arr, len(beats),
            tmp_cut.encode(), ctypes.c_double(clip_duration),
        )
        if rc != 0 or not os.path.exists(tmp_cut):
            err = _LIB.bs_video_get_last_error(writer)
            logger.error(f"cut_at_beats_multi failed: {err.decode() if err else rc}")
            return None

        stage = tmp_cut
        if effects:
            cfg = _EffectsConfig(
                enableTransitions=0, transitionType=b"fade", transitionDuration=0.12,
                enableColorGrade=1, colorPreset=b"vibrant",
                enableVignette=1, vignetteStrength=0.25,
                enableBeatFlash=1, flashIntensity=0.35,
                enableBeatZoom=1, zoomIntensity=0.12,
                effectBeatDivisor=1, effectStartTime=0.0, effectEndTime=-1.0,
            )
            if _LIB.bs_video_set_effects_config(writer, ctypes.byref(cfg)) == 0:
                rc = _LIB.bs_video_apply_effects(writer, tmp_cut.encode(), tmp_fx.encode(), beat_arr, len(beats))
                if rc == 0 and os.path.exists(tmp_fx):
                    stage = tmp_fx
                else:
                    logger.warning("effects pass failed; using un-effected cut")

        if audio_path and os.path.exists(audio_path):
            # Start the audio at the drop. NOTE: the engine ignores audioStart
            # unless audioEnd is also a real value (-1 falls back to 0), so we
            # always pass an explicit end = start + montage length (+buffer).
            montage_len = (beats[-1] - beats[0]) + clip_duration + 1.0
            audio_end = audio_start + montage_len
            rc = _LIB.bs_video_add_audio_track(
                writer, stage.encode(), audio_path.encode(), output_path.encode(),
                1, ctypes.c_double(audio_start), ctypes.c_double(audio_end),
            )
            if rc == 0 and os.path.exists(output_path):
                return output_path
            logger.warning("audio mux failed; returning silent montage")

        # No audio (or mux failed): the effected/cut file is the result.
        os.replace(stage, output_path)
        return output_path
    finally:
        for f in (tmp_cut, tmp_fx):
            if os.path.exists(f) and f != output_path:
                try:
                    os.remove(f)
                except OSError:
                    pass
        _LIB.bs_destroy_video_writer(writer)
