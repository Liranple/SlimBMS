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

import atexit
import os
import shutil
import tempfile
import threading
import time
from typing import List, Optional

try:
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

MIN_SPEED = 0.25
MAX_SPEED = 2.0

# Per-song stretch caches live in temp dirs; track them so a clean process exit
# removes whatever the last-loaded song left behind (load/unload remove earlier
# ones as they go).
_CACHE_DIRS: set = set()


@atexit.register
def _cleanup_cache_dirs() -> None:
    for d in list(_CACHE_DIRS):
        shutil.rmtree(d, ignore_errors=True)
    _CACHE_DIRS.clear()

# Phase-vocoder parameters. hop = n_fft/4 (75% overlap) — the standard overlap
# for good reconstruction quality; less time-smearing / overshoot than 50%.
# It's ~2x slower, but the stretch runs in the background and is cached.
_N_FFT = 2048
_HOP = 512


# Output frames processed per block. The old code built the whole song's STFT,
# interpolated magnitude/phase and inverse-transformed it all at once; at slow
# speeds that meant multi-gigabyte transient arrays that ran the machine out of
# memory (the occasional crash). Streaming a bounded block of output frames at a
# time caps the transient to a small multiple of the block, with identical
# output. Kept modest so the per-block STFT/ISTFT temporaries stay small; the
# per-call overhead of more, smaller blocks is negligible next to the FFTs.
_STRETCH_BLOCK = 512


def _time_stretch(x, speed: float):
    """Pitch-preserving time-stretch of mono float32 ``x`` by ``speed`` (``>1`` =
    faster/shorter), via a phase vocoder. **Yields** the output as a sequence of
    consecutive float32 chunks — concatenating them gives the whole stretched
    signal.

    Streaming keeps peak memory bounded regardless of song length or how slow
    the speed is: the old code allocated the whole song's output/overlap-add
    buffers up front, which at slow speeds (which produce many output frames)
    reached multiple gigabytes and ran the machine out of memory. Here only a
    block of output frames plus a small overlap tail are held at once. The
    running phase accumulator and the overlap-add tail are carried across block
    boundaries, so the concatenation matches a whole-song transform exactly."""
    n_fft, hop = _N_FFT, _HOP
    win = _np.hanning(n_fft).astype(_np.float32)
    xp = _np.concatenate([_np.zeros(n_fft // 2, _np.float32), x,
                          _np.zeros(n_fft, _np.float32)])
    n_frames = 1 + (len(xp) - n_fft) // hop
    if n_frames < 2:
        yield x.copy()
        return
    n_bins = n_fft // 2 + 1
    phi = (2 * _np.pi * hop * _np.arange(n_bins) / n_fft).astype(_np.float32)[:, None]

    steps = _np.arange(0, n_frames, speed)
    n_out = len(steps)
    w2 = win ** 2
    ov = n_fft // hop

    def frame_stft(f0: int, f1: int):
        """STFT columns for input frames ``[f0, f1)``; frames past the signal
        end are zero (matches the old whole-song zero-padding)."""
        st = _np.zeros((n_bins, f1 - f0), _np.complex128)
        avail = min(f1, n_frames) - f0
        if avail > 0:
            starts = f0 + _np.arange(avail)
            idx = _np.arange(n_fft)[None, :] + hop * starts[:, None]
            st[:, :avail] = _np.fft.rfft(xp[idx] * win, axis=1).T
        return st

    # ``prev_phase`` is the accumulated output phase for the next output frame,
    # seeded (like the original) with the first input frame's phase.
    prev_phase = None
    # Sliding overlap-add buffer. ``buf``/``bw`` cover output samples starting at
    # ``buf_start``; once frames up to ``c1`` are added, samples before
    # ``c1 * hop`` are final (no later frame reaches them) and are emitted.
    buf = _np.zeros(0, _np.float32)
    bw = _np.zeros(0, _np.float32)
    buf_start = 0
    for c0 in range(0, n_out, _STRETCH_BLOCK):
        c1 = min(n_out, c0 + _STRETCH_BLOCK)
        s = steps[c0:c1]
        j = s.astype(_np.int64)
        jmin, jmax = int(j[0]), int(j[-1])
        st = frame_stft(jmin, jmax + 2)            # need j and j+1
        mag = _np.abs(st).astype(_np.float32)
        ang = _np.angle(st).astype(_np.float32)
        jj = j - jmin
        frac = (s - j).astype(_np.float32)[None, :]
        out_mag = (1 - frac) * mag[:, jj] + frac * mag[:, jj + 1]
        dphase = ang[:, jj + 1] - ang[:, jj] - phi
        dphase -= 2 * _np.pi * _np.round(dphase / (2 * _np.pi))
        # Accumulate phase in float64: over a long, slow-speed song there are
        # hundreds of thousands of output frames, and float32 cumulative phase
        # would drift audibly.
        inc = (phi + dphase).astype(_np.float64)
        if prev_phase is None:
            prev_phase = ang[:, 0].astype(_np.float64)
        csum = _np.cumsum(inc, axis=1)
        acc = _np.empty_like(inc)
        acc[:, 0] = prev_phase
        acc[:, 1:] = prev_phase[:, None] + csum[:, :-1]
        prev_phase = prev_phase + csum[:, -1]      # carry into the next block
        spec = out_mag * _np.exp(1j * acc)
        frames = (_np.fft.irfft(spec.T, n=n_fft, axis=1).astype(_np.float32)) * win

        # Inverse STFT overlap-add into the sliding buffer, grouped by overlap
        # phase so each group is internally non-overlapping and adds in one
        # vectorised shot. Frame ``c0 + t`` lands at output sample
        # ``(c0 + t) * hop``.
        block_start = c0 * hop
        block_end = (c1 - 1) * hop + n_fft
        need = block_end - buf_start
        if need > len(buf):
            buf = _np.concatenate([buf, _np.zeros(need - len(buf), _np.float32)])
            bw = _np.concatenate([bw, _np.zeros(need - len(bw), _np.float32)])
        for r in range(ov):
            grp = frames[r::ov]
            block = grp.reshape(-1)
            start = block_start + r * hop - buf_start
            length = min(len(block), len(buf) - start)
            buf[start:start + length] += block[:length]
            bw[start:start + length] += _np.tile(w2, grp.shape[0])[:length]

        # Emit everything finalised: samples before the next block's first frame
        # (``c1 * hop``), or the whole buffer on the last block.
        cut = len(buf) if c1 >= n_out else c1 * hop - buf_start
        cut = max(0, min(cut, len(buf)))
        if cut > 0:
            w = bw[:cut].copy()
            w[w < 1e-8] = 1.0
            yield buf[:cut] / w
            buf = buf[cut:].copy()
            bw = bw[cut:].copy()
            buf_start += cut


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
        self._build_failed = False
        self._build_lock = threading.Lock()
        # Per-song temp dir holding the built stretch for each speed (as raw
        # int16 PCM). Lets a re-selected speed load instantly instead of
        # rebuilding, and keeps only the current speed's buffer in RAM.
        self._cache_dir: Optional[str] = None

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

    # -- stretch cache (per-song temp dir) ---------------------------------- #

    def _reset_cache(self) -> None:
        """Drop the previous song's stretch cache and start a fresh temp dir for
        the newly-loaded song."""
        self._remove_cache()
        if _np is None:
            return
        try:
            d = tempfile.mkdtemp(prefix=f"slimbms_stretch_{os.getpid()}_")
            _CACHE_DIRS.add(d)
            self._cache_dir = d
        except OSError:
            self._cache_dir = None

    def _remove_cache(self) -> None:
        if self._cache_dir:
            shutil.rmtree(self._cache_dir, ignore_errors=True)
            _CACHE_DIRS.discard(self._cache_dir)
        self._cache_dir = None

    def _cache_path(self, speed: float) -> Optional[str]:
        if not self._cache_dir:
            return None
        return os.path.join(self._cache_dir, f"{speed:.2f}.pcm")

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
        self._reset_cache()            # previous song's stretches no longer apply
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
        """Make the current speed's stretch the active playback buffer, building
        it (streamed to the on-disk cache) if it isn't cached yet. Heavy for a
        first build at a slow speed; call from a worker thread. Safe to call
        repeatedly; a re-selected (already-cached) speed loads instantly.

        If the stretch can't be built (e.g. the machine is out of memory), it
        degrades to 1.0x rather than crashing or leaving playback in an
        inconsistent state — see :meth:`build_failed`."""
        with self._build_lock:
            speed = self._speed
            if self._stretched_speed == speed:
                return
            if abs(speed - 1.0) < 1e-9 or not self._can_stretch():
                # 1.0x needs no work; without numpy we can't preserve pitch, so
                # just play unstretched (mark ready to avoid retrying).
                self._stretched = self._raw
                self._stretched_speed = speed
                self._build_failed = False
                return
            try:
                self._stretched = self._load_or_build(speed)
                self._stretched_speed = speed
                self._build_failed = False
            except (MemoryError, Exception):  # noqa: BLE001
                # Fall back to unprocessed audio at normal speed so the clock,
                # buffer and pitch all stay consistent (a silent, safe 1.0x
                # instead of a crash). The UI resyncs its speed gauge to match.
                self._speed = 1.0
                self._stretched = self._raw
                self._stretched_speed = 1.0
                self._build_failed = True

    def precompute_speeds(self, speeds) -> None:
        """Best-effort background pre-build of the stretch cache for ``speeds``
        so switching to them later is instant. Skips 1.0x and already-cached
        speeds; never touches the current playback buffer/speed."""
        if not self._can_stretch():
            return
        for speed in speeds:
            speed = max(MIN_SPEED, min(MAX_SPEED, float(speed)))
            if abs(speed - 1.0) < 1e-9:
                continue
            with self._build_lock:
                path = self._cache_path(speed)
                if path is None or os.path.exists(path):
                    continue
                try:
                    self._render_to_file(speed, path)
                except Exception:  # noqa: BLE001
                    pass  # a failed pre-build just isn't cached; not fatal

    def _load_or_build(self, speed: float) -> bytes:
        """Return the stretched int16 PCM for ``speed``, using the on-disk cache
        when present (instant) and otherwise streaming the build into it."""
        path = self._cache_path(speed)
        if path is None:
            # No cache dir (e.g. temp unavailable): build to a throwaway file.
            fd, tmp = tempfile.mkstemp(suffix=".pcm")
            os.close(fd)
            try:
                self._render_to_file(speed, tmp)
                with open(tmp, "rb") as f:
                    return f.read()
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if not os.path.exists(path):
            self._render_to_file(speed, path)
        with open(path, "rb") as f:
            return f.read()

    def _render_to_file(self, speed: float, out_path: str) -> None:
        """Time-stretch every channel to ``speed`` and write interleaved int16
        PCM to ``out_path``. Streams in bounded blocks so peak memory stays small
        at any speed/length: channel stretches are consumed chunk-by-chunk into a
        float32 scratch file (to measure loudness), then rescaled to int16 in a
        second streaming pass. Output matches the old whole-song render, RMS
        loudness-match and hard-limit included."""
        chans = self._chans
        channels = self._channels
        # Input loudness is known up front (mean square, averaged over channels).
        in_ms = sum(float((ch.astype(_np.float64) ** 2).mean()) for ch in chans)
        in_ms /= len(chans)

        scratch = out_path + ".f32"
        out_sumsq = 0.0
        n_samples = 0
        try:
            gens = [_time_stretch(ch, speed) for ch in chans]
            with open(scratch, "wb") as f:
                for parts in zip(*gens):
                    m = min(len(p) for p in parts)  # channels are equal-length
                    block = _np.empty((m, channels), _np.float32)
                    for c, p in enumerate(parts):
                        block[:, c] = p[:m]
                    f.write(block.tobytes())
                    out_sumsq += float((block.astype(_np.float64) ** 2).sum())
                    n_samples += m * channels
            # Match the original's loudness (RMS), then hard-limit: the phase
            # vocoder overshoots (esp. on transients), and peak-normalising would
            # make it far too quiet, so scale by RMS and clip the few remaining
            # peaks.
            out_ms = out_sumsq / n_samples if n_samples else 0.0
            scale = (float(_np.sqrt(in_ms / out_ms))
                     if out_ms > 1e-12 and in_ms > 0.0 else 1.0)
            with open(scratch, "rb") as fin, open(out_path, "wb") as fout:
                while True:
                    raw = fin.read(1 << 20)
                    if not raw:
                        break
                    arr = _np.frombuffer(raw, _np.float32) * scale
                    pcm = _np.clip(arr * 32768.0, -32768, 32767).astype(_np.int16)
                    fout.write(pcm.tobytes())
        except BaseException:
            for p in (scratch, out_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            raise
        finally:
            try:
                os.remove(scratch)
            except OSError:
                pass

    def build_failed(self) -> bool:
        """True if the last :meth:`build_stretch` fell back to 1.0x on error."""
        return self._build_failed

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
        self._remove_cache()
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
