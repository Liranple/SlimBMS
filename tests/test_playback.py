"""Transport/preview state-machine tests, driven synchronously (no event loop).

These lock the playback behaviour that the GUI smoke test doesn't cover — pause/
resume, seek, count-in, metronome beat tracking, stop-at-end — so the P5
PlaybackController extraction can be proven behaviour-preserving.

Run: SDL_AUDIODRIVER=dummy QT_QPA_PLATFORM=offscreen python tests/test_playback.py
"""

import atexit
import os
import sys
import tempfile
import time
import wave

os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import numpy as np
except Exception:
    np = None

from PySide6.QtWidgets import QApplication  # noqa: E402

from slimbms.model import Project  # noqa: E402
from slimbms.ui.main_window import MainWindow  # noqa: E402

_TMP = []


def _wav(seconds=3.0, rate=44100):
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
    _TMP.append(path)
    return path


@atexit.register
def _cleanup():
    for p in _TMP:
        try:
            os.remove(p)
        except OSError:
            pass


def _win():
    QApplication.instance() or QApplication([])
    win = MainWindow(Project(title="PB", bpm=120, measures=16))
    if np is not None and win.audio.available:
        win.audio.load(_wav())
    return win


def _clock_at(win, pos):
    """Force the audio clock to report ~pos seconds (deterministic, no sleep)."""
    win.audio._playing = True
    win.audio._anchor_pos = pos
    win.audio._anchor_t = time.monotonic()


def test_play_pause_resume_stop():
    win = _win()
    v = win.view
    win.rec_countin.setChecked(False)
    win._do_start_play()
    assert win.audio.playing and v.live_playing and win.playback._preview_active
    win._pause_play()
    assert win.audio.paused and not win.audio.playing
    assert not v.live_playing and v.playhead is not None
    win.toggle_play()                     # paused -> resume
    assert win.audio.playing
    win.stop_play()
    assert not win.audio.playing and not win.playback._preview_active and v.playhead is None


def test_toggle_from_stopped():
    win = _win()
    win.rec_countin.setChecked(False)
    win.toggle_play()
    assert win.audio.playing
    win.toggle_play()                     # playing -> pause
    assert win.audio.paused
    win.stop_play()


def test_seek_and_go_to_start():
    win = _win()
    win._seek_audio(2.0)
    assert win.playback._preview_active and abs(win.audio.position() - 2.0) < 0.1
    assert win.view.playhead is not None
    win.go_to_start()
    assert abs(win.audio.position()) < 0.1


def test_countin_then_starts():
    win = _win()
    win.rec_countin.setChecked(True)
    win._start_play()
    assert win.playback._counting_in and not win.audio.playing
    for _ in range(4):                    # first beat + 3 ticks -> real start
        win._countin_tick()
    assert not win.playback._counting_in and win.audio.playing
    win.stop_play()


def test_countin_cancelled_by_toggle():
    win = _win()
    win.rec_countin.setChecked(True)
    win._start_play()
    assert win.playback._counting_in
    win.toggle_play()                     # cancels the count-in and stops
    assert not win.playback._counting_in and not win.audio.playing


def test_metronome_tracks_beats():
    win = _win()
    win.rec_metronome.setChecked(True)
    win._do_start_play()
    win.playback._last_beat = -1
    _clock_at(win, 1.0)                    # 120bpm -> 0.5 measure -> beat 2
    win._on_play_tick()
    assert win.playback._last_beat >= 0
    win.stop_play()


def test_stops_at_end_of_timeline():
    win = _win()
    win.rec_countin.setChecked(False)
    win._do_start_play()
    _clock_at(win, 10_000.0)              # far past the timeline / audio end
    win._on_play_tick()
    assert not win.audio.playing and win.view.playhead is None


def test_speed_commit_unity_is_synchronous():
    win = _win()
    win.playback._pending_speed = 1.0
    win._commit_speed()                   # 1.0x needs no stretch build -> sync
    assert abs(win.audio.speed - 1.0) < 1e-9


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
