"""BGM audio playback with pitch-preserving speed control.

Wraps ``pygame.mixer`` and exposes a smooth playback clock for the preview.
The whole BGM is decoded into memory once. Playback speed is changed with a
phase-vocoder time-stretch (tempo changes, **pitch is preserved**), computed
over the whole song and cached per speed so seeking within a speed is instant.
The stretch is heavy (a few seconds for a long song), so callers should build it
off the UI thread via :meth:`build_stretch`; :meth:`play` will build on demand
as a fallback.

Degrades gracefully: with no audio device (headless CI) every method is a safe
no-op and :attr:`available` is ``False``; the clock still runs so the visual
preview scrolls. Without numpy the speed feature is disabled (stays at 1.0x).
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

try:
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

MIN_SPEED = 0.25
MAX_SPEED = 2.0

# Phase-vocoder parameters. hop = n_fft/4 (75% overlap) — the standard overlap
# for good reconstruction quality; less time-smearing / overshoot than 50%.
# It's ~2x slower, but the stretch runs in the background and is cached.
_N_FFT = 2048
_HOP = 512


def _time_stretch(x, speed: float):
    """Time-stretch mono float32 ``x`` by ``speed`` (``>1`` = faster/shorter)
    while preserving pitch, via a phase vocoder. Returns a float32 array."""
    n_fft, hop = _N_FFT, _HOP
    win = _np.hanning(n_fft).astype(_np.float32)
    xp = _np.concatenate([_np.zeros(n_fft // 2, _np.float32), x,
                          _np.zeros(n_fft, _np.float32)])
    n_frames = 1 + (len(xp) - n_fft) // hop
    if n_frames < 2:
        return x.copy()
    idx = _np.arange(n_fft)[None, :] + hop * _np.arange(n_frames)[:, None]
    stft = _np.fft.rfft(xp[idx] * win, axis=1).T          # (bins, frames)
    mag = _np.abs(stft).astype(_np.float32)
    ang = _np.angle(stft).astype(_np.float32)
    n_bins = mag.shape[0]

    steps = _np.arange(0, n_frames, speed)
    phi = (2 * _np.pi * hop * _np.arange(n_bins) / n_fft).astype(_np.float32)[:, None]
    mag = _np.pad(mag, [(0, 0), (0, 2)])
    ang = _np.pad(ang, [(0, 0), (0, 2)])
    j = steps.astype(_np.int64)
    frac = (steps - j).astype(_np.float32)[None, :]
    out_mag = (1 - frac) * mag[:, j] + frac * mag[:, j + 1]
    dphase = ang[:, j + 1] - ang[:, j] - phi
    dphase -= 2 * _np.pi * _np.round(dphase / (2 * _np.pi))
    inc = phi + dphase
    acc = _np.empty_like(out_mag)
    acc[:, 0] = ang[:, 0]
    acc[:, 1:] = ang[:, 0][:, None] + _np.cumsum(inc, axis=1)[:, :-1]
    spec = out_mag * _np.exp(1j * acc)

    # Inverse STFT with overlap-add, grouped by overlap phase so each group is
    # non-overlapping and can be added in one vectorised shot.
    frames = (_np.fft.irfft(spec.T, n=n_fft, axis=1).astype(_np.float32)) * win
    nf = frames.shape[0]
    n = n_fft + hop * (nf - 1)
    out = _np.zeros(n, _np.float32)
    wsum = _np.zeros(n, _np.float32)
    w2 = win ** 2
    ov = n_fft // hop
    for r in range(ov):
        grp = frames[r::ov]
        block = grp.reshape(-1)
        start = r * hop
        length = min(len(block), n - start)
        out[start:start + length] += block[:length]
        wsum[start:start + length] += _np.tile(w2, grp.shape[0])[:length]
    wsum[wsum < 1e-8] = 1.0
    return out / wsum


class AudioPlayer:
    def __init__(self) -> None:
        self.available = False
        self.loaded = False
        self.path: Optional[str] = None
        self.duration = 0.0            # seconds at 1.0x, 0 if unknown
        self._playing = False
        self._paused = False           # stream is paused and can unpause (no rebuild)
        self._anchor_pos = 0.0
        self._anchor_t = 0.0
        self._paused_pos = 0.0
        self._speed = 1.0
        self._volume = 1.0

        # Decoded BGM: original int16 bytes + per-channel float arrays (for the
        # stretch), plus the cached stretched bytes for the current speed.
        self._raw = b""
        self._chans: List = []
        self._rate = 44100
        self._channels = 2
        self._width = 2                # bytes per sample
        self._stretched = b""
        self._stretched_speed: Optional[float] = None
        self._build_lock = threading.Lock()

        self._sound = None
        self._channel = None
        self._click = None
        self._click_accent = None
        self._init_mixer()
        self._make_click()

    def _init_mixer(self) -> None:
        try:
            import pygame

            pygame.mixer.init()
            self._pygame = pygame
            self.available = True
        except Exception:  # noqa: BLE001
            self._pygame = None
            self.available = False

    def _make_click(self) -> None:
        """Pre-render short metronome clicks (a plain tick and an accented one)."""
        if not self.available or _np is None:
            return
        freq, size, channels = self._pygame.mixer.get_init()
        for attr, hz in (("_click", 1500.0), ("_click_accent", 2200.0)):
            n = int(freq * 0.045)
            t = _np.arange(n) / freq
            tone = _np.sin(2 * _np.pi * hz * t) * _np.exp(-t * 55.0) * 0.5
            pcm = _np.clip(tone * 32767, -32768, 32767).astype(_np.int16)
            if channels > 1:
                pcm = _np.repeat(pcm[:, None], channels, axis=1)
            try:
                setattr(self, attr, self._pygame.mixer.Sound(buffer=pcm.tobytes()))
            except Exception:  # noqa: BLE001
                setattr(self, attr, None)

    def waveform_peaks(self, buckets_per_sec: int = 200):
        """Down-sampled absolute-peak envelope of the BGM (normalised 0..1) plus
        the bucket rate, for drawing a waveform. Returns (None, bps) if there is
        no decoded audio."""
        if _np is None or not self._chans:
            return None, buckets_per_sec
        ch = self._chans
        mono = ch[0] if len(ch) == 1 else (ch[0] + ch[1]) * 0.5
        bucket = max(1, self._rate // buckets_per_sec)
        n = len(mono) // bucket
        if n == 0:
            return None, buckets_per_sec
        peaks = _np.abs(mono[:n * bucket].reshape(n, bucket)).max(axis=1)
        mx = float(peaks.max())
        if mx > 0:
            peaks = peaks / mx
        return peaks, buckets_per_sec

    def play_click(self, accent: bool = False) -> None:
        snd = self._click_accent if accent else self._click
        if snd is None:
            return
        try:
            snd.play()
        except Exception:  # noqa: BLE001
            pass

    # -- loading ------------------------------------------------------------ #

    def load(self, path: str) -> bool:
        if not self.available:
            self.path = path
            return False
        # Decode into locals first and commit only once every step has
        # succeeded, so a mid-load failure leaves the previously-loaded song
        # fully intact instead of a mix of old and new state.
        try:
            snd = self._pygame.mixer.Sound(path)
            duration = float(snd.get_length())
            raw = snd.get_raw()
            freq, size, channels = self._pygame.mixer.get_init()
            width = abs(size) // 8
            chans = self._decode_channels(raw, channels, width)
        except Exception:  # noqa: BLE001
            return False   # leave all prior state (incl. current playback) intact

        # Success: stop any playback of the previous song before swapping it in,
        # otherwise the old channel keeps playing after we drop the reference.
        self._stop_channel()
        self.duration = duration
        self._raw = raw
        self._rate = freq
        self._channels = channels
        self._width = width
        self._chans = chans
        self.path = path
        self.loaded = True
        self._playing = False
        self._paused = False
        self._paused_pos = 0.0
        self._sound = None
        self._channel = None
        # Invalidate the stretch cache; 1.0x needs no processing.
        self._stretched = self._raw
        self._stretched_speed = 1.0 if abs(self._speed - 1.0) < 1e-9 else None
        return True

    def _decode_channels(self, raw: bytes, channels: int, width: int) -> List:
        if _np is None or width != 2:
            return []
        arr = _np.frombuffer(raw, dtype=_np.int16)
        if channels > 1:
            arr = arr.reshape(-1, channels)
            return [arr[:, c].astype(_np.float32) / 32768.0
                    for c in range(channels)]
        return [arr.astype(_np.float32) / 32768.0]

    # -- speed / stretch ---------------------------------------------------- #

    def _can_stretch(self) -> bool:
        return _np is not None and self._width == 2 and bool(self._chans)

    def build_stretch(self) -> None:
        """Build (and cache) the whole-song stretch for the current speed. Heavy
        for slow speeds; call from a worker thread. Safe to call repeatedly."""
        with self._build_lock:
            speed = self._speed
            if self._stretched_speed == speed:
                return
            if abs(speed - 1.0) < 1e-9 or not self._can_stretch():
                # 1.0x needs no work; without numpy we can't preserve pitch, so
                # just play unstretched (mark ready to avoid retrying).
                self._stretched = self._raw
                self._stretched_speed = speed
                return
            stretched = [_time_stretch(ch, speed) for ch in self._chans]
            length = min(len(c) for c in stretched)
            inter = _np.empty((length, self._channels), dtype=_np.float32)
            for c, ch in enumerate(stretched):
                inter[:, c] = ch[:length]
            # Match the original's loudness (RMS), then hard-limit: the phase
            # vocoder overshoots (esp. on transients), and peak-normalising would
            # make the whole thing far too quiet, so scale by RMS and clip only
            # the few remaining peaks.
            in_ms = _np.mean([float((ch.astype(_np.float64) ** 2).mean())
                              for ch in self._chans]) if self._chans else 0.0
            out_ms = float((inter.astype(_np.float64) ** 2).mean()) if inter.size else 0.0
            if out_ms > 1e-12 and in_ms > 0.0:
                inter *= float(_np.sqrt(in_ms / out_ms))
            inter = _np.clip(inter * 32768.0, -32768, 32767).astype(_np.int16)
            self._stretched = inter.tobytes()
            self._stretched_speed = speed

    def _ensure_stretched(self) -> None:
        if self._stretched_speed != self._speed:
            self.build_stretch()

    def stretch_ready(self) -> bool:
        return self._stretched_speed == self._speed

    def set_speed(self, speed: float) -> None:
        """Set playback speed (0.25–2.0). Only records the speed and invalidates
        the cache; rebuild with :meth:`build_stretch` and restart playback."""
        speed = max(MIN_SPEED, min(MAX_SPEED, speed))
        if _np is None:
            # Without numpy we can't time-stretch with pitch preserved, so speed
            # control is disabled (documented contract): stay at 1.0x instead of
            # playing raw audio against a speed-scaled clock (which would drift).
            speed = 1.0
        if abs(speed - self._speed) < 1e-9:
            return
        self._speed = speed
        if abs(speed - 1.0) < 1e-9:
            # Back to 1.0x: no stretch, but the cached buffer still holds the
            # previous speed's stretch. Reset it to the raw audio, otherwise
            # playback keeps using the stale stretched bytes (wrong speed/pitch).
            self._stretched = self._raw
            self._stretched_speed = 1.0
        else:
            self._stretched_speed = None
        self._stop_channel()

    # -- stream helpers ----------------------------------------------------- #

    def _frame_bytes(self) -> int:
        return self._channels * self._width

    def _start_stream(self, at_seconds: float) -> None:
        if not (self.available and self.loaded):
            return
        self._ensure_stretched()
        self._stop_channel()
        fs = self._frame_bytes()
        # Song second -> index in the stretched (duration/speed) buffer.
        start = int(max(0.0, at_seconds) / self._speed * self._rate) * fs
        seg = self._stretched[start:]
        if not seg:
            return
        try:
            snd = self._pygame.mixer.Sound(buffer=seg)
        except Exception:  # noqa: BLE001
            return
        self._sound = snd
        try:
            self._channel = snd.play()
            if self._channel is not None:
                self._channel.set_volume(self._volume)
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
        """Start (or restart) playback from ``at_seconds`` — rebuilds the stream,
        so use :meth:`resume` for un-pausing to avoid the rebuild."""
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
                self._channel.pause()
            except Exception:  # noqa: BLE001
                pass

    def resume(self) -> None:
        """Resume a paused stream WITHOUT rebuilding it (no drift). Falls back to
        a fresh start if nothing is paused."""
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

    def unload(self) -> None:
        """Drop the currently-loaded BGM entirely and reset to the no-song
        state (used when starting a fresh project)."""
        self.stop()
        self.loaded = False
        self.path = None
        self.duration = 0.0
        self._anchor_pos = 0.0
        self._anchor_t = 0.0
        self._raw = b""
        self._chans = []
        self._stretched = b""
        self._stretched_speed = None
        self._sound = None

    def seek(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        if self._playing:
            self.play(seconds)
        else:
            self._paused_pos = seconds
            self._paused = False
            self._stop_channel()

    # -- clock -------------------------------------------------------------- #

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def volume(self) -> float:
        return self._volume

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))
        if self._channel is not None:
            try:
                self._channel.set_volume(self._volume)
            except Exception:  # noqa: BLE001
                pass

    def position(self) -> float:
        """Current playback position in song seconds (advances at the speed)."""
        if self._playing:
            return self._anchor_pos + self._speed * (time.monotonic() - self._anchor_t)
        return self._paused_pos
