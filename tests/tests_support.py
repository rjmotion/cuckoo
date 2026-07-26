"""Shared fixtures. Not a test module — helpers only, so every test builds the
same camera and a drift in one place shows up everywhere.
"""

from __future__ import annotations

import adoption
from model import AxisRange, Camera, Position

PAN_RANGE = AxisRange(500, 35500)
TILT_RANGE = AxisRange(8000, 18000)
ZOOM_RANGE = AxisRange(0, 730)


def a_camera(ptz: bool = True) -> Camera:
    """An adopted G5 PTZ, with the motor bounds the real one announces."""
    camera = Camera(
        mac="AABBCCDDEEFF",
        model="UVC G5 PTZ",
        firmware="5.3.95",
        name="front gate",
        adopted=True,
        tracks=adoption.track_defaults(),
    )
    if ptz:
        camera.pan_range, camera.tilt_range, camera.zoom_range = PAN_RANGE, TILT_RANGE, ZOOM_RANGE
    camera.motion.update(Position(pan=18000, tilt=13000, zoom=365, focus=50), activity=0)
    return camera
