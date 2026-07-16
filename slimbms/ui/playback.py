"""Playback / preview transport, split out of MainWindow.

Owns the preview clock timer, the count-in and metronome, the debounced
pitch-preserving speed control, seeking, and the auto-scroll that keeps the
playhead in view. Holds a back-reference to the window for the widgets it drives
(audio, canvas, scroll area, toolbar action, recording-aid checkboxes)."""

from __future__ import annotations

from PySide6.QtCore import QTimer

from ..timing import TimeMap
from .worker import _Worker

PREVIEW_FPS = 60
# Where the playhead sits within the viewport (fraction from the top). Notes
# scroll upward, so keeping it low leaves upcoming notes visible above it.
PLAYHEAD_VIEWPORT_FRACTION = 0.72


class PlaybackController:
    """All of the window's playback state and transport logic."""

    def __init__(self, win) -> None:
        self.win = win
        self._timemap = None
        self._preview_active = False
        self._play_timer = QTimer(win)
        self._play_timer.setInterval(int(1000 / PREVIEW_FPS))
        self._play_timer.timeout.connect(self._on_play_tick)
        # Recording aids: count-in beats + metronome beat tracking.
        self._counting_in = False
        self._countin_left = 0
        self._last_beat = -1
        self._countin_timer = QTimer(win)
        self._countin_timer.timeout.connect(self._countin_tick)
        # Debounce speed-gauge drags: rebuild the stretch once the drag settles.
        self._pending_speed = 1.0
        self._speed_timer = QTimer(win)
        self._speed_timer.setSingleShot(True)
        self._speed_timer.timeout.connect(self._commit_speed)

    def _set_speed(self, factor: float) -> None:
        # Debounce: the gauge fires on every 0.05 step while dragging, but the
        # pitch-preserving stretch is heavy, so only rebuild once it settles.
        self._pending_speed = factor
        self._speed_timer.start(280)

    def _commit_speed(self) -> None:
        factor = self._pending_speed
        if abs(factor - self.win.audio.speed) < 1e-9:
            return
        was_playing = self.win.audio.playing
        pos = self.win.audio.position()
        # Stop audio + clock while we rebuild the stretch (pitch preserved).
        self._play_timer.stop()
        self.win.audio.stop()
        self.win.view.set_live(False)
        self.win.audio.set_speed(factor)
        if not self.win.audio.loaded or self.win.audio.stretch_ready():
            self._speed_ready(pos, was_playing)   # 1.0x / no audio: nothing to build
            return
        self.win.statusBar().showMessage(f"재생 속도 {factor:.2f}× 처리 중…")
        worker = self._speed_worker = _Worker(self.win.audio.build_stretch)
        worker.done.connect(lambda _=None: self._speed_ready(pos, was_playing))
        worker.failed.connect(lambda _msg: self._speed_ready(pos, was_playing))
        worker.start()

    def _speed_ready(self, pos: float, was_playing: bool) -> None:
        self.win.statusBar().clearMessage()
        self.win.audio.seek(pos)
        if was_playing:
            self._start_play()
        elif self._preview_active:
            self.win.view.set_playhead(self._ensure_timemap().chart_pos(pos))

    def _ensure_timemap(self) -> TimeMap:
        # Rebuilt on demand so BPM / BGM-offset edits always take effect.
        self._timemap = TimeMap(self.win.project)
        return self._timemap

    def _current_chart_pos(self) -> float:
        return self._ensure_timemap().chart_pos(self.win.audio.position())

    def _update_record_offset(self) -> None:
        # Convert the ms offset to measures at the current tempo for recording.
        mps = TimeMap(self.win.project).measures_per_second
        self.win.view.record_offset_measures = (self.win.rec_offset.value() / 1000.0) * mps

    def toggle_play(self) -> None:
        if self._counting_in:
            self._cancel_countin()
            self.stop_play()
        elif self.win.audio.playing:
            self._pause_play()
        else:
            self._start_play()

    def _start_play(self) -> None:
        # A count-in plays four beats before a fresh start (not when resuming).
        if self.win.rec_countin.isChecked() and not self.win.audio.paused and not self._counting_in:
            self._begin_countin()
            return
        self._do_start_play()

    def _do_start_play(self) -> None:
        self._ensure_timemap()
        self._preview_active = True
        self._update_record_offset()
        self._last_beat = -1
        # Un-pause in place when we were paused (no re-seek, so the audio stays
        # in sync); otherwise start/seek to the current position.
        if self.win.audio.paused:
            self.win.audio.resume()
        else:
            self.win.audio.play(self.win.audio.position())
        self.win.play_action.setIcon(self.win._icon_pause)
        self._play_timer.start()
        self.win.view.set_live(True)
        self.win.view.setFocus()   # so recording keys reach the canvas

    def _begin_countin(self) -> None:
        self._counting_in = True
        self._countin_left = 3          # first beat plays now, three more follow
        self.win.play_action.setIcon(self.win._icon_pause)
        self.win.statusBar().showMessage("카운트인…")
        self.win.audio.play_click(accent=True)
        interval = int(60000.0 / max(1.0, self.win.project.bpm))
        self._countin_timer.setInterval(interval)
        self._countin_timer.start()

    def _countin_tick(self) -> None:
        if not self._counting_in:
            self._countin_timer.stop()
            return
        if self._countin_left <= 0:
            self._countin_timer.stop()
            self._counting_in = False
            self.win.statusBar().clearMessage()
            self._do_start_play()
            return
        self.win.audio.play_click(accent=False)
        self._countin_left -= 1

    def _cancel_countin(self) -> None:
        self._countin_timer.stop()
        self._counting_in = False
        self.win.statusBar().clearMessage()

    def _pause_play(self) -> None:
        self.win.audio.pause()
        self._play_timer.stop()
        self.win.play_action.setIcon(self.win._icon_play)
        self.win.view.set_live(False)
        self.win._on_mode_changed(self.win.view.mode)
        # Keep the playhead visible where we paused so seeking has a reference.
        self.win.view.set_playhead(self._current_chart_pos())

    def stop_play(self) -> None:
        self._cancel_countin()
        self.win.audio.stop()
        self._play_timer.stop()
        self.win.play_action.setIcon(self.win._icon_play)
        self._preview_active = False
        self.win.view.set_live(False)
        self.win._on_mode_changed(self.win.view.mode)
        self.win.view.set_playhead(None)

    def go_to_start(self) -> None:
        self._seek_audio(0.0)

    def seek_seconds(self, d_seconds: float) -> None:
        self._seek_audio(self.win.audio.position() + d_seconds)

    def _seek_audio(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        self.win.audio.seek(seconds)
        self._preview_active = True
        chart_pos = self._ensure_timemap().chart_pos(seconds)
        self.win.view.set_playhead(chart_pos)
        self._follow_playhead(chart_pos)

    def _seek_to_chart(self, absolute: float) -> None:
        # Click-to-seek: put the playhead where clicked (no scroll jump); the
        # next play starts from there.
        seconds = self._ensure_timemap().audio_seconds(absolute)
        self.win.audio.seek(seconds)
        self._preview_active = True
        self.win.view.set_playhead(max(0.0, absolute))

    def _on_play_tick(self) -> None:
        if self._timemap is None:
            return
        pos = self.win.audio.position()
        chart_pos = self._timemap.chart_pos(pos)
        # Stop at the end of the timeline (or audio).
        if chart_pos >= self.win.project.measures or (
            self.win.audio.duration and pos >= self.win.audio.duration
        ):
            self.stop_play()
            return
        if self.win.rec_metronome.isChecked():
            beat = int(chart_pos * 4)   # 4 beats per measure (4/4)
            if beat > self._last_beat:
                self.win.audio.play_click(accent=(beat % 4 == 0))
                self._last_beat = beat
        self.win.view.set_playhead(chart_pos)
        self._follow_playhead(chart_pos)

    def _viewport_chart_pos(self) -> float:
        """Chart position currently at the playhead line in the viewport."""
        vbar = self.win.scroll.verticalScrollBar()
        vp_h = self.win.scroll.viewport().height()
        y_in_view = vbar.value() + vp_h * PLAYHEAD_VIEWPORT_FRACTION
        absolute = self.win.project.measures - (y_in_view - self.win.view.v_pad) / self.win.view.measure_px
        return max(0.0, absolute)

    def _follow_playhead(self, chart_pos: float) -> None:
        vbar = self.win.scroll.verticalScrollBar()
        vp_h = self.win.scroll.viewport().height()
        y = self.win.view.y_for(chart_pos)
        target = int(y - vp_h * PLAYHEAD_VIEWPORT_FRACTION)
        target = max(vbar.minimum(), min(vbar.maximum(), target))
        vbar.setValue(target)
