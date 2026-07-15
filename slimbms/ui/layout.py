"""Shared horizontal lane layout used by both the canvas and its header.

Left to right: a single BGM lane, then the 4K / 6K lane groups, each
separated by a gap so the key modes can be compared side by side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..model import DISPLAY_LABELS, DISPLAY_MODES, lanes_for

LEFT_MARGIN = 48   # room for measure numbers
LANE_W = 30
GROUP_GAP = 24
RIGHT_PAD = 12


@dataclass(frozen=True)
class Column:
    kind: str               # "bgm" or "key"
    key_mode: Optional[int]  # None for BGM
    lane: int
    x: int                  # left edge
    w: int                  # column width (BGM may differ from the key lanes)


@dataclass(frozen=True)
class Group:
    label: str
    x0: int
    x1: int


def build_layout(lane_w: int = LANE_W, bgm_w: Optional[int] = None
                 ) -> Tuple[List[Column], List[Group], int]:
    if bgm_w is None:
        bgm_w = lane_w
    columns: List[Column] = []
    groups: List[Group] = []
    x = LEFT_MARGIN

    start = x
    columns.append(Column("bgm", None, 0, x, bgm_w))
    x += bgm_w
    groups.append(Group("BGM", start, x))
    x += GROUP_GAP

    for km in DISPLAY_MODES:
        start = x
        for lane in range(lanes_for(km)):
            columns.append(Column("key", km, lane, x, lane_w))
            x += lane_w
        groups.append(Group(DISPLAY_LABELS[km], start, x))
        x += GROUP_GAP

    total_width = x - GROUP_GAP + RIGHT_PAD
    return columns, groups, total_width


def column_at(columns: List[Column], px: float) -> Optional[Column]:
    for col in columns:
        if col.x <= px < col.x + col.w:
            return col
    return None
