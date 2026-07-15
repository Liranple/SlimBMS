# SlimBMS

A slim, **keysound-less BMS chart editor** for **4K / 5K / 6K**.

SlimBMS is a deliberately small alternative to full editors like uBMSC. It does
one thing: let you place notes for the 4-key, 5-key and 6-key versions of a song
**side by side** so you can compare them at a glance, over a single background
music track — no per-note keysounds to manage.

![editor layout](docs/layout.png)

## Concept

- **Three charts, one screen.** The 4K, 5K and 6K lanes are shown next to each
  other. Notes flow bottom-to-top (measure 0 at the bottom).
- **No keysounds.** The song is a single audio file (`.wav` / `.ogg` / …) laid
  down on the **BGM lane**; you only place its start timing plus the notes.
- **Save per key mode.** Pick `4K` / `5K` / `6K` in the toolbar and export that
  chart as a standard `.bms` file. Metadata and BGM are shared across all three.
- **Never lose work.** The native `.slbms` project file keeps all three charts
  together so an editing session round-trips losslessly.

## Files

| Format    | Contents                                             | Use                     |
|-----------|------------------------------------------------------|-------------------------|
| `.slbms`  | All three charts + shared metadata (JSON)            | Save / continue editing |
| `.bms`    | One selected key mode, standard BMS                  | Deliverable for a game  |

## Editing

Two editing modes (toolbar buttons or **F2** / **F3**):

- **추가 모드 (F3)** — **left click** adds a note in the cell you point at (a
  ghost note previews it on hover); **right click** removes the nearest note.
- **편집 모드 (F2)** — click or drag a rubber-band to **select** notes, then:
  **arrow keys** move them (←/→ lane, ↑/↓ time), **Ctrl+C / X / V** copy / cut /
  paste (at the last clicked spot), **`** flips the selection left↔right,
  **Delete** removes them.

- **Grid / snap** (right sidebar): two number boxes — a main snap grid and a
  reference grid — each is *cells per measure* (16 = snap to 1/16). The
  **격자 스냅** button toggles snapping; hold **Shift** to place freely off-grid.
- **Zoom**: 확대 / 축소 buttons, or **Ctrl + mouse wheel**.
- The timeline **auto-extends** as you place notes / set a BGM — no measure
  count to manage.
- **Sidebar**: title, artist, genre, BPM, level, grids, and BGM registration.

### Playback / preview

- **Space** — play / pause. The chart scrolls in sync and a red playhead sweeps.
- **`+`** — seek 1 second forward, **`-`** — 1 second back (arrow keys are
  reserved for moving selected notes).
- **Home** (⏮) — jump to the start.

### Updating

The app checks GitHub for a newer release on launch (and via **도움말 → 업데이트
확인**). If one exists it offers to download and restart into it automatically —
no manual re-download needed.
- **곡 → BGM 오디오 선택**: pick the background audio file. Keep that file next
  to the exported `.bms` so the game can find it.

### Lane → BMS channel mapping

Keysound-less, so the channel numbers only matter when a chart is opened in
another BMS player. Defined in `slimbms/model.py` (`KEY_CHANNELS`):

| Key mode | Channels               |
|----------|------------------------|
| 4K       | 11 12 13 14            |
| 5K       | 11 12 13 14 15         |
| 6K       | 11 12 13 14 15 18      |

BGM objects use channel `01`.

### Importing a .bms

Loading a `.bms` puts every key object into a dedicated **불러오기 (import)**
lane group (A1~A8) to the right of the 6K group, so charts with more channels
than 6K (scratch, keys 6/7, …) load without losing notes. That group is a
reference/staging area — you copy from it into the 4K/5K/6K charts as needed.

## Running from source

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
.venv/bin/python main.py                     # Windows: .venv\Scripts\python main.py
```

## Building the Windows `.exe`

Every push to `main` (and every `v*` tag) builds a **folder (onedir)** app via
GitHub Actions and zips it to `SlimBMS-windows.zip`. Onedir loads its DLLs
directly (fast start, and no temp-extraction race when a self-update swaps the
files), so distribution is a zip you unzip once — then run `SlimBMS/SlimBMS.exe`.

- **Latest build:** repo → **Actions** → newest run → **Artifacts** →
  `SlimBMS-windows`.
- **Tagged release:** push a tag like `v0.8.0`; the zip is attached to the
  Release automatically. In-app **update** downloads that zip and swaps the
  folder in place.

To build locally on Windows:

```bat
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --windowed --name SlimBMS main.py
```

The app appears in `dist/SlimBMS/`.

## Tests

```bash
.venv/bin/python tests/test_bms_io.py     # data model + BMS/project I/O
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_gui_smoke.py
```
