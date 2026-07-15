"""BGM audio playback.

Wraps ``pygame.mixer`` and exposes a smooth playback clock for the preview.
The whole BGM is decoded into memory once so playback can run at an adjustable
speed: the raw samples are resampled by the speed factor (pitch shifts with it,
like a tape speed change) and played as a :class:`pygame.mixer.Sound`. The clock
is anchored to a monotonic timer at each play/seek and advances at the current
speed so the chart scroll stays in sync.

Degrades gracefully: if no audio device is available (e.g. headless CI) every
method is a safe no-op and :attr:`available` is ``False``; the clock still runs
so the visual preview scrolls.
"""

from __future__ import annotations

import time
import warnings
from typing import Optional

# ``audioop`` (stdlib) does the sample-rate conversion for speed changes. It is
# deprecated and dropped in Python 3.13; if it is gone, speed falls back to 1.0.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        import audioop as _audioop
    except Exception:  # noqa: BLE001
        _audioop = None

MIN_SPEED = 0.25
MAX_SPEED = 2.0


class AudioPlayer:
    def __init__(self) -> None:
        self.available = False
        self.loaded = False
        self.path: Optional[str] = None
        self.duration = 0.0            # seconds at 1.0x, 0 if unknown
        self._playing = False
        self._paused = False           # stream is paused and can unpause (no rebuild)
        self._anchor_pos = 0.0         # audio seconds at the anchor moment
        self._anchor_t = 0.0           # monotonic() at the anchor moment
        self._paused_pos = 0.0
        self._speed = 1.0

        # Decoded BGM samples (mixer format) and the sound/channel in flight.
        self._raw = b""
        self._rate = 44100
        self._channels = 2
        self._width = 2                # bytes per sample
        self._sound = None
        self._channel = None
        self._init_mixer()

    def _init_mixer(self) -> None:
        try:
            import pygame

            pygame.mixer.init()
            self._pygame = pygame
            self.available = True
        except Exception:  # noqa: BLE001 — no device, missing lib, etc.
            self._pygame = None
            self.available = False

    # -- loading ------------------------------------------------------------ #

    def load(self, path: str) -> bool:
        if not self.available:
            self.path = path
            return False
        try:
            snd = self._pygame.mixer.Sound(path)
            self.duration = float(snd.get_length())
            self._raw = snd.get_raw()
            freq, size, channels = self._pygame.mixer.get_init()
            self._rate = freq
            self._channels = channels
            self._width = abs(size) // 8
            self.path = path
            self.loaded = True
            self._playing = False
            self._paused = False
            self._paused_pos = 0.0
            self._sound = None
            self._channel = None
            return True
        except Exception:  # noqa: BLE001
            self.loaded = False
            return False

    # -- stream helpers ----------------------------------------------------- #

    def _frame_bytes(self) -> int:
        return self._channels * self._width

    def _build_sound(self, at_seconds: float):
        """A Sound for the tail of the song from ``at_seconds``, resampled to the
        current speed (empty tail -> ``None``)."""
        fs = self._frame_bytes()
        start = int(max(0.0, at_seconds) * self._rate) * fs
        seg = self._raw[start:]
        if not seg:
            return None
        if abs(self._speed - 1.0) > 1e-6 and _audioop is not None:
            try:
                out_rate = max(1, int(round(self._rate / self._speed)))
                seg, _ = _audioop.ratecv(seg, self._width, self._channels,
                                         self._rate, out_rate, None)
            except Exception:  # noqa: BLE001 — fall back to normal speed
                pass
        try:
            return self._pygame.mixer.Sound(buffer=seg)
        except Exception:  # noqa: BLE001
            return None

    def _start_stream(self, at_seconds: float) -> None:
        if not (self.available and self.loaded):
            return
        self._stop_channel()
        snd = self._build_sound(at_seconds)
        if snd is None:
            return
        self._sound = snd
        try:
            self._channel = snd.play()
        except Exception:  # noqa: BLE001
            self._channel = None

    def _stop_channel(self) -> None:
        if self._channel is not None:
            try:
                self._channel.stop()
            except Exception:  # noqa: BLE001
                pass
        self._channel = None

    # -- transport ---------------------------------------------------------- #

    def play(self, at_seconds: float = 0.0) -> None:
        """Start (or restart) playback from ``at_seconds`` — this rebuilds the
        stream, so use :meth:`resume` for un-pausing to avoid the rebuild."""
        at_seconds = max(0.0, at_seconds)
        self._anchor_pos = at_seconds
        self._anchor_t = time.monotonic()
        self._playing = True
        self._paused = False
        self._start_stream(at_seconds)

    def pause(self) -> None:
        if not self._playing:
            return
        self._paused_pos = self.position()
        self._playing = False
        self._paused = True
        if self._channel is not None:
            try:
                self._channel.pause()   # halt in place; no rebuild
            except Exception:  # noqa: BLE001
                pass

    def resume(self) -> None:
        """Resume a paused stream WITHOUT rebuilding it, so the audio continues
        exactly where it stopped and no start-up latency accumulates across
        pause/resume cycles. Falls back to a fresh start if nothing is paused."""
        if not self._paused or self._channel is None:
            self.play(self._paused_pos)
            return
        self._anchor_pos = self._paused_pos
        self._anchor_t = time.monotonic()
        self._playing = True
        self._paused = False
        try:
            self._channel.unpause()
        except Exception:  # noqa: BLE001
            self.play(self._paused_pos)

    def toggle(self) -> bool:
        """Play/pause; returns the new playing state."""
        if self._playing:
            self.pause()
        else:
            self.resume()
        return self._playing

    def stop(self) -> None:
        self._playing = False
        self._paused = False
        self._paused_pos = 0.0
        self._stop_channel()

    def seek(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        if self._playing:
            self.play(seconds)
        else:
            # A real rebuild is needed on next play, so drop the paused stream.
            self._paused_pos = seconds
            self._paused = False
            self._stop_channel()

    def set_speed(self, speed: float) -> None:
        """Set playback speed (0.25–2.0). Restarts the stream in place while
        playing; a paused/stopped stream picks it up on the next play."""
        speed = max(MIN_SPEED, min(MAX_SPEED, speed))
        if abs(speed - self._speed) < 1e-9:
            return
        pos = self.position()          # capture at the OLD speed first
        self._speed = speed
        if self._playing:
            self.play(pos)             # re-anchor + rebuild at the new speed
        elif self._paused:
            # Drop the paused stream so the next play rebuilds at the new speed.
            self._paused = False
            self._paused_pos = pos
            self._stop_channel()

    # -- clock -------------------------------------------------------------- #

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def paused(self) -> bool:
        """True when a stream is paused and can be resumed without a rebuild."""
        return self._paused

    @property
    def speed(self) -> float:
        return self._speed

    def position(self) -> float:
        """Current playback position in song seconds (advances at the speed)."""
        if self._playing:
            return self._anchor_pos + self._speed * (time.monotonic() - self._anchor_t)
        return self._paused_pos
