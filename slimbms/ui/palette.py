"""Canonical colour tokens — the single source of truth for the design colours
that are shared between the Qt theme (``theme.py``) and the chart canvas
(``chart_view.py``).

Previously these were spelled out as duplicate hex literals in several files
(the theme's ``CANVAS`` and the canvas's ``C_BG`` both hard-coded ``#1e1e24``,
the accent ``#6fd0ff`` appeared in three files, …), so changing one silently
drifted from the others. Keeping them here — as plain strings with no Qt
dependency — means every surface derives from one place.
"""

from __future__ import annotations

# Window / widget surfaces (consumed by the Qt palette + stylesheet).
APP_BG = "#17171c"        # window backdrop
PANEL = "#212129"         # toolbar / sidebar surfaces
CANVAS = "#1e1e24"        # chart area — shared with the canvas background
FIELD = "#2a2a33"         # text inputs
BORDER = "#33333d"        # hairlines — shared with the canvas lane separators
BORDER_STRONG = "#44444f"
TEXT = "#d6d6de"          # primary text
TEXT_DIM = "#9a9aa6"      # secondary / hints
ACCENT = "#6fd0ff"        # selection / focus — shared across theme, canvas, gauges
ACCENT_INK = "#0c1116"    # text on an accent fill
DANGER = "#ff6b81"
