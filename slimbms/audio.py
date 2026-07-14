"""BGM audio playback.

Wraps ``pygame.mixer`` and exposes a smooth playback clock for the preview.
The clock is anchored to a monotonic timer at each play/seek so the position
advances smoothly for rendering; the audio itself streams via pygame.

Degrades gracefully: if no audio device is available (e.g. headless CI) every
method is a safe no-op and :attr:`available` is ``False``.
"""

from __future__ import annotations

import time
from typing import Optional


class AudioPlayer:
    def __init__(self) -> None:
        self.available = False
        self.loaded = False
        self.path: Optional[str] = None
        self.duration = 0.0            # seconds, 0 if unknown
        self._playing = False
        self._anchor_pos = 0.0         # audio seconds at the anchor moment
        self._anchor_t = 0.0           # monotonic() at the anchor moment
        self._paused_pos = 0.0
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
            self._pygame.mixer.music.load(path)
            self.duration = self._probe_duration(path)
            self.path = path
            self.loaded = True
            self._playing = False
            self._paused_pos = 0.0
            return True
        except Exception:  # noqa: BLE001
            self.loaded = False
            return False

    def _probe_duration(self, path: str) -> float:
        try:
            snd = self._pygame.mixer.Sound(path)
            return float(snd.get_length())
        except Exception:  # noqa: BLE001
            return 0.0

    # -- transport ---------------------------------------------------------- #

    def play(self, at_seconds: float = 0.0) -> None:
        at_seconds = max(0.0, at_seconds)
        self._anchor_pos = at_seconds
        self._anchor_t = time.monotonic()
        self._playing = True
        if self.available and self.loaded:
            try:
                self._pygame.mixer.music.play(start=at_seconds)
            except Exception:  # noqa: BLE001 — some formats reject start offset
                try:
                    self._pygame.mixer.music.play()
                    self._anchor_pos = 0.0
                except Exception:  # noqa: BLE001
                    pass

    def pause(self) -> None:
        if not self._playing:
            return
        self._paused_pos = self.position()
        self._playing = False
        if self.available and self.loaded:
            try:
                self._pygame.mixer.music.stop()
            except Exception:  # noqa: BLE001
                pass

    def toggle(self) -> bool:
        """Play/pause; returns the new playing state."""
        if self._playing:
            self.pause()
        else:
            self.play(self._paused_pos)
        return self._playing

    def stop(self) -> None:
        self._playing = False
        self._paused_pos = 0.0
        if self.available and self.loaded:
            try:
                self._pygame.mixer.music.stop()
            except Exception:  # noqa: BLE001
                pass

    def seek(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        if self._playing:
            self.play(seconds)
        else:
            self._paused_pos = seconds

    # -- clock -------------------------------------------------------------- #

    @property
    def playing(self) -> bool:
        return self._playing

    def position(self) -> float:
        """Current playback position in seconds."""
        if self._playing:
            return self._anchor_pos + (time.monotonic() - self._anchor_t)
        return self._paused_pos
