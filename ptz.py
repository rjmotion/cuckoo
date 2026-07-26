"""PTZ command encoding.

Motion is expressed as a preset: define it, then go to it. There is no absolute
move verb on this channel. Presets carry four axes including focus.
"""

from __future__ import annotations

from typing import Any, Final

from model import AxisRange, Camera, Position, Preset

PRESET: Final = "Preset"
ENABLE_PTZ: Final = "EnablePtzControl"
DISABLE_PTZ: Final = "DisablePtzControl"
GET_POSITION: Final = "GetCurrentPosition"
EVENT_MOTOR_STATE: Final = "EventMotorState"

PTZ_SUBPROTOCOL: Final = "ptz1"

DEFAULT_SPEED: Final = 1000
SCRATCH_INDEX: Final = 99  # reused for arbitrary moves, which have no slot of their own


def enable(controller_url: str) -> dict[str, Any]:
    """Ask the camera to open its PTZ channel back to us.

    The URL is our own management path; the camera distinguishes the second
    socket by negotiating the `ptz1` subprotocol on it.
    """
    return {"uri": controller_url}


def disable() -> dict[str, Any]:
    return {}


def get_position() -> dict[str, Any]:
    """Position is polled as well as pushed; both units are requested."""
    return {"inDegree": True, "inSteps": True}


def configure(preset: Preset) -> dict[str, Any]:
    return {
        "action": "config",
        "items": [
            {
                "index": preset.index,
                "name": preset.name,
                "pan": preset.position.pan,
                "tilt": preset.position.tilt,
                "zoom": preset.position.zoom,
                "focus": preset.position.focus,
            }
        ],
    }


def go(index: int, speed: int = DEFAULT_SPEED) -> dict[str, Any]:
    return {
        "action": "go",
        "index": index,
        "speed": speed,
        "notifyCommandStatus": {},
    }


def move_to(position: Position, speed: int = DEFAULT_SPEED) -> list[dict[str, Any]]:
    """An arbitrary move: write a scratch preset, then go to it."""
    scratch = Preset(index=SCRATCH_INDEX, name="cuckoo-move", position=position)
    return [configure(scratch), go(scratch.index, speed)]


def auto_track(track_timeout_sec: int = 20, back_to_preset: bool = True) -> dict[str, Any]:
    """Return-home behaviour after auto-tracking; a third action of the same verb."""
    return {
        "trackTimeoutSec": track_timeout_sec,
        "backToPresetPosition": back_to_preset,
    }


def parse_motor_state(payload: dict[str, Any]) -> tuple[Position, int] | None:
    """Read a pushed gimbal update.

    `activity` is a flag word, not a magnitude: zero means settled. `scale` tells
    you which coordinate system the position is in, so it must not be assumed.
    """
    state = payload.get("state")
    if not isinstance(state, dict):
        return None
    raw = state.get("position")
    if not isinstance(raw, dict):
        return None

    def axis(name: str) -> int:
        value = raw.get(name)
        return int(value) if isinstance(value, (int, float)) else 0

    position = Position(
        pan=axis("pan"), tilt=axis("tilt"), zoom=axis("zoom"), focus=axis("focus")
    )
    activity = state.get("activity")
    return position, int(activity) if isinstance(activity, (int, float)) else 0


def normalised_to_position(camera: Camera, pan: float, tilt: float, zoom: float) -> Position:
    """Map an ONVIF vector onto motor units, keeping the current focus."""
    return Position(
        pan=camera.pan_range.from_normalised(pan),
        tilt=camera.tilt_range.from_normalised(tilt),
        zoom=camera.zoom_range.from_normalised(zoom),
        focus=camera.motion.position.focus,
    )


def position_to_normalised(camera: Camera, position: Position) -> tuple[float, float, float]:
    return (
        camera.pan_range.to_normalised(position.pan),
        camera.tilt_range.to_normalised(position.tilt),
        camera.zoom_range.to_normalised(position.zoom),
    )


def ranges_from_hello(features: dict[str, Any]) -> tuple[AxisRange, AxisRange, AxisRange]:
    """Read motor bounds from the camera's own announcement.

    They differ per model, so they are never hardcoded.
    """

    def axis(name: str) -> AxisRange:
        entry = features.get(name)
        steps = entry.get("steps") if isinstance(entry, dict) else None
        if not isinstance(steps, dict):
            return AxisRange(0, 0)
        low = steps.get("min")
        high = steps.get("max")
        if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
            return AxisRange(0, 0)
        return AxisRange(int(low), int(high))

    return axis("pan"), axis("tilt"), axis("zoom")
