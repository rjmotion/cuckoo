"""Device model — the seam between the ONVIF front end and the camera controller.

Nothing here knows about SOAP or about wire message names. Both sides depend on
this and not on each other.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

PAN: Final = "pan"
TILT: Final = "tilt"
ZOOM: Final = "zoom"
FOCUS: Final = "focus"

AXES: Final[tuple[str, str, str, str]] = (PAN, TILT, ZOOM, FOCUS)


class Codec(Enum):
    H264 = "h264"
    H265 = "h265"
    MJPEG = "mjpg"
    AAC = "aac"
    OPUS = "opus"


@dataclass(frozen=True)
class AxisRange:
    """Motor travel for one axis, in raw motor units as the camera reports them."""

    minimum: int
    maximum: int
    invert: bool = False

    def clamp(self, value: int) -> int:
        return max(self.minimum, min(self.maximum, value))

    def to_normalised(self, value: int) -> float:
        """Map motor units onto ONVIF's -1.0..1.0."""
        span = self.maximum - self.minimum
        if span <= 0:
            return 0.0
        unit = (self.clamp(value) - self.minimum) / span
        out = unit * 2.0 - 1.0
        return -out if self.invert else out

    def from_normalised(self, value: float) -> int:
        v = -value if self.invert else value
        unit = (max(-1.0, min(1.0, v)) + 1.0) / 2.0
        return self.clamp(round(self.minimum + unit * (self.maximum - self.minimum)))


@dataclass(frozen=True)
class Position:
    pan: int = 0
    tilt: int = 0
    zoom: int = 0
    focus: int = 0

    def as_dict(self) -> dict[str, int]:
        return {PAN: self.pan, TILT: self.tilt, ZOOM: self.zoom, FOCUS: self.focus}


@dataclass
class Preset:
    index: int
    name: str
    position: Position


@dataclass(frozen=True)
class VideoTrack:
    """One encoder track the camera can be told to push."""

    name: str
    stream_id: int
    source_id: int
    width: int
    height: int
    fps: int
    codec: Codec
    bitrate: int

    @property
    def is_video(self) -> bool:
        return self.codec in (Codec.H264, Codec.H265, Codec.MJPEG)


@dataclass
class Motion:
    """Latest known gimbal state.

    Position arrives two ways: polled, and pushed while the head is moving.
    Both write here; `settled` reflects the most recent push.
    """

    position: Position = field(default_factory=Position)
    activity: int = 0
    updated_at: float = 0.0

    @property
    def settled(self) -> bool:
        return self.activity == 0

    def update(self, position: Position, activity: int) -> None:
        self.position = position
        self.activity = activity
        self.updated_at = time.time()


@dataclass
class Camera:
    """Everything the front end may know about the adopted camera."""

    mac: str
    model: str = ""
    firmware: str = ""
    name: str = "cuckoo camera"
    adopted: bool = False
    pan_range: AxisRange = field(default_factory=lambda: AxisRange(0, 0))
    tilt_range: AxisRange = field(default_factory=lambda: AxisRange(0, 0))
    zoom_range: AxisRange = field(default_factory=lambda: AxisRange(0, 0))
    tracks: list[VideoTrack] = field(default_factory=list)
    presets: dict[int, Preset] = field(default_factory=dict)
    motion: Motion = field(default_factory=Motion)
    smart_detect: list[str] = field(default_factory=list)
    audio_codecs: list[Codec] = field(default_factory=list)

    @property
    def is_ptz(self) -> bool:
        return self.pan_range.maximum > self.pan_range.minimum

    def range_for(self, axis: str) -> AxisRange:
        if axis == PAN:
            return self.pan_range
        if axis == TILT:
            return self.tilt_range
        if axis == ZOOM:
            return self.zoom_range
        raise KeyError(axis)

    def track(self, name: str) -> VideoTrack | None:
        return next((t for t in self.tracks if t.name == name), None)

    def next_preset_index(self) -> int:
        return max(self.presets, default=-1) + 1
