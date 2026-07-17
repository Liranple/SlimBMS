"""Regression tests for the audio player's transport/clock/lifecycle.

These lock in the CURRENT, deliberately-tuned behaviour of :class:`AudioPlayer`
so refactors can't silently regress the hard-won sync fixes (v0.22 in-place
pause/resume, v0.24 Sound-channel backend, per-speed stretch cache). They run on
the SDL ``dummy`` audio driver used in headless CI, where ``available`` is True
and Sound buffers/channels behave normally but make no sound.

Run: SDL_AUDIODRIVER=dummy QT_QPA_PLATFORM=offscreen python tests/test_audio.py
"""

import atexit
import os
import sys
import tempfile
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is a hard dependency in practice
    np = None

from slimbms.audio import AudioPlayer  # noqa: E402

_TMP_FILES = []


def _make_wav(seconds: float = 2.0, rate: int = 44100) -> str:
    """Write a short stereo 16-bit WAV and return its path (cleaned up atexit)."""
    n = int(seconds * rate)
    t = np.arange(n) / rate
    tone = (0.2 * np.sin(2 * np.pi * 220.0 * t) * 32767).astype(np.int16)
    stereo = np.repeat(tone[:, None], 2, axis=1)
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(stereo.tobytes())
    _TMP_FILES.append(path)
    return path


@atexit.register
def _cleanup():
    for p in _TMP_FILES:
        try:
            os.remove(p)
        except OSError:
            pass


def _player():
    """A loaded player, or None when there is no usable audio device/numpy."""
    if np is None:
        return None
    a = AudioPlayer()
    if not a.available:
        return None
    a.load(_make_wav())
    if not a.loaded:
        return None
    return a


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def test_load_decodes_and_sets_state():
    a = _player()
    if a is None:
        return  # headless-without-device: skip (counts as pass)
    assert a.loaded is True
    assert a.duration > 0.0
    assert a._chans and len(a._chans) == a._channels
    # At 1.0x the stretch cache is just the raw audio, marked ready.
    assert a._stretched == a._raw
    assert a.stretch_ready() is True


def test_load_stops_previous_channel():
    """F5: loading a new BGM while one is playing must stop the old channel."""
    a = _player()
    if a is None:
        return
    a.play(0.0)
    old = a._channel
    assert old is not None and old.get_busy()
    a.load(_make_wav())
    # The previously-playing channel must be silenced, not left running.
    assert not old.get_busy()
    assert a._channel is None


def test_load_failure_rolls_back_state():
    """F6: a mid-load failure must not leave a mix of old and new song state."""
    a = _player()
    if a is None:
        return
    good_dur = a.duration
    good_raw = a._raw
    good_path = a.path
    # Force a failure partway through load (after Sound() succeeds).
    orig = a._decode_channels
    a._decode_channels = lambda *args: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        ok = a.load(_make_wav())
    finally:
        a._decode_channels = orig
    assert ok is False
    # State must be exactly the previously-loaded song, not partially updated.
    assert a.loaded is True
    assert a.duration == good_dur
    assert a._raw == good_raw
    assert a.path == good_path


# --------------------------------------------------------------------------- #
# transport clock (v0.22 in-place pause/resume — must NOT re-seek)
# --------------------------------------------------------------------------- #

def test_pause_resume_is_continuous_no_reseek():
    a = _player()
    if a is None:
        return
    a.play(1.0)
    # Simulate 0.5s of elapsed wall-clock without sleeping.
    a._anchor_t -= 0.5
    pos_before = a.position()
    assert abs(pos_before - 1.5) < 0.05
    a.pause()
    assert a.paused is True and a.playing is False
    assert abs(a._paused_pos - pos_before) < 0.02
    a.resume()
    # Resume continues from the paused position (no fresh seek/rebuild).
    assert a.paused is False and a.playing is True
    assert abs(a._anchor_pos - pos_before) < 0.02
    assert abs(a.position() - pos_before) < 0.05


def test_resume_without_pause_falls_back_to_play():
    a = _player()
    if a is None:
        return
    a.stop()
    a._paused_pos = 2.0
    a.resume()  # nothing paused -> fresh start from paused_pos
    assert a.playing is True
    assert abs(a._anchor_pos - 2.0) < 1e-9


def test_seek_playing_reanchors_paused_stores():
    a = _player()
    if a is None:
        return
    a.play(0.0)
    a.seek(5.0)
    assert a.playing is True
    assert abs(a._anchor_pos - 5.0) < 1e-9
    a.stop()
    a.seek(3.0)
    assert a.playing is False
    assert abs(a._paused_pos - 3.0) < 1e-9


def test_position_when_stopped_is_paused_pos():
    a = _player()
    if a is None:
        return
    a.stop()
    a._paused_pos = 2.5
    assert a.position() == 2.5


# --------------------------------------------------------------------------- #
# speed / stretch cache
# --------------------------------------------------------------------------- #

def test_set_speed_invalidates_cache_and_stops():
    a = _player()
    if a is None:
        return
    a.play(0.0)
    assert a._channel is not None
    a.set_speed(0.5)
    assert abs(a.speed - 0.5) < 1e-9
    assert a._stretched_speed is None          # cache invalidated
    assert a._channel is None                  # playback stopped
    a.set_speed(1.0)
    assert abs(a.speed - 1.0) < 1e-9
    assert a._stretched_speed == 1.0           # 1.0x needs no build


def test_build_stretch_caches_per_speed():
    a = _player()
    if a is None:
        return
    a.set_speed(0.5)
    assert a.stretch_ready() is False
    a.build_stretch()
    assert a.stretch_ready() is True
    assert a._stretched_speed == 0.5
    assert len(a._stretched) > 0
    # A faster-than-1 speed yields a shorter buffer than the raw song.
    a.set_speed(2.0)
    a.build_stretch()
    assert a.stretch_ready() is True
    assert len(a._stretched) < len(a._raw)


def test_build_stretch_falls_back_on_failure():
    """A stretch that can't be built (e.g. out of memory) must degrade to a
    consistent 1.0x instead of crashing or leaving a stale/mismatched buffer."""
    a = _player()
    if a is None:
        return
    import slimbms.audio as audio_mod
    a.set_speed(0.5)
    saved = audio_mod._time_stretch
    audio_mod._time_stretch = lambda *args: (_ for _ in ()).throw(MemoryError("boom"))
    try:
        a.build_stretch()
    finally:
        audio_mod._time_stretch = saved
    assert a.build_failed() is True
    assert abs(a.speed - 1.0) < 1e-9          # reverted to normal speed
    assert a._stretched == a._raw             # buffer matches the 1.0x clock
    assert a.stretch_ready() is True          # won't retry (no UI-thread rebuild)


def test_speed_disabled_without_numpy():
    """Documented contract: with no numpy, speed control stays at 1.0x."""
    import slimbms.audio as audio_mod
    if audio_mod._np is None:
        return
    saved = audio_mod._np
    audio_mod._np = None
    try:
        a = AudioPlayer()
        if not a.available:
            return
        a.set_speed(0.5)
        assert abs(a.speed - 1.0) < 1e-9
    finally:
        audio_mod._np = saved


def test_set_volume_clamped():
    a = _player()
    if a is None:
        return
    a.set_volume(5.0)
    assert a.volume == 1.0
    a.set_volume(-1.0)
    assert a.volume == 0.0


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
