"""Coordinate grounding for screenshot->page mapping.

All coordinates crossing a model/act boundary are CSS pixels (page space).
Screenshots captured via chrome.debugger are device pixels; the only place
device pixels exist here is .captureScreenshot scale — normalized at ingest.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GroundedPoint:
    x: float  # CSS px, page space
    y: float


def ground_coords(
    *, device_x: float, device_y: float, dpr: float, scroll_y: float
) -> GroundedPoint:
    """Convert a device-pixel screenshot point + DPR + scroll offset into
    CSS-pixel page coordinates. dpr<=0 is a contract breach — ValueError."""
    if dpr <= 0:
        raise ValueError("dpr must be > 0")
    return GroundedPoint(x=device_x / dpr, y=device_y / dpr + scroll_y)
