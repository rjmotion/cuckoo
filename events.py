"""Camera detection events, read into something the front end can publish.

Four verbs carry detections, and they do not agree on vocabulary: plain motion
starts and stops, object and audio detections enter and leave. Both directions are
normalised here to one flag, so nothing downstream has to know which verb it came
from.

Object types come from the camera's own hello (`smartDetect`), so nothing is
assumed about which ones a given model supports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

EVENT_ANALYTICS: Final = "EventAnalytics"
EVENT_SMART_DETECT: Final = "EventSmartDetect"
EVENT_SMART_MOTION: Final = "EventSmartMotion"
EVENT_SMART_AUDIO: Final = "EventSmartAudio"

DETECTION_VERBS: Final = frozenset(
    {EVENT_ANALYTICS, EVENT_SMART_DETECT, EVENT_SMART_MOTION, EVENT_SMART_AUDIO}
)

MOTION: Final = "motion"
AUDIO: Final = "audio"

STARTING: Final = frozenset({"start", "enter", "begin", "on"})
ENDING: Final = frozenset({"stop", "leave", "end", "off"})


@dataclass(frozen=True)
class Detection:
    """One thing the camera says it can see or hear, and whether it still does."""

    kind: str  # "motion", an object type such as "person", or an audio alarm class
    active: bool
    event_id: str = ""


def _edge(payload: dict[str, Any]) -> bool | None:
    """Read the start/stop flag, whichever spelling this verb uses."""
    edge = payload.get("edgeType") or payload.get("edge")
    if isinstance(edge, str):
        lowered = edge.strip().lower()
        if lowered in STARTING:
            return True
        if lowered in ENDING:
            return False
    for key, value in (("enter", True), ("leave", False)):
        if payload.get(key):
            return value
    return None


def _identifier(payload: dict[str, Any]) -> str:
    value = payload.get("eventId")
    return str(value) if value is not None else ""


def parse(function_name: str, payload: dict[str, Any]) -> list[Detection]:
    """Turn one event message into zero or more detections."""
    if function_name not in DETECTION_VERBS:
        return []
    active = _edge(payload)
    if active is None:
        return []
    identifier = _identifier(payload)

    objects = payload.get("objectTypes")
    if isinstance(objects, list) and objects:
        return [
            Detection(kind=str(kind), active=active, event_id=identifier)
            for kind in objects
            if isinstance(kind, str)
        ]

    if function_name == EVENT_SMART_AUDIO:
        classes = [
            key
            for key, value in payload.items()
            if key.startswith("alrm") and isinstance(value, (int, bool)) and value
        ]
        if classes:
            return [Detection(kind=name, active=active, event_id=identifier) for name in classes]
        return [Detection(kind=AUDIO, active=active, event_id=identifier)]

    kind = payload.get("eventType")
    return [
        Detection(kind=str(kind) if isinstance(kind, str) and kind else MOTION,
                  active=active, event_id=identifier)
    ]
