"""Adoption: the message sequence that takes a camera to adopted.

Replying to the camera's own hello is the gate. Until that reply lands the camera
retransmits hello and ignores everything else.

The settings suite is ack-gated: message N+1 is released only when N is
acknowledged. Sent as a burst, the camera stops acking and resets the channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Final

import ptz
from unifiwire.envelope import CONTROLLER, Envelope, Ids, reply_to, request
from model import Camera, Codec, VideoTrack

PROTOCOL_VERSION: Final = 67
CONTROLLER_VERSION: Final = "1.21.4"

ACK_TIMEOUT_SEC: Final = 3.0
PING_INTERVAL_SEC: Final = 2.0
HEARTBEAT_TIMEOUT_MS: Final = 60_000

CHANGE_VIDEO: Final = "ChangeVideoSettings"
CHANGE_ISP: Final = "ChangeIspSettings"
CHANGE_DEVICE: Final = "ChangeDeviceSettings"
CHANGE_OSD: Final = "ChangeOsdSettings"
CHANGE_SOUND_LED: Final = "ChangeSoundLedSettings"
CHANGE_SMART_DETECT: Final = "ChangeSmartDetectSettings"
CHANGE_AUDIO_EVENTS: Final = "ChangeAudioEventsSettings"
NETWORK_STATUS: Final = "NetworkStatus"
START_SERVICE: Final = "StartService"
STOP_SERVICE: Final = "StopService"
GET_REQUEST: Final = "GetRequest"


def hello_reply(name: str = "cuckoo", version: str = CONTROLLER_VERSION) -> dict[str, Any]:
    """What the camera needs back to unblock.

    No adoptionCode and no features block. `controllerUuid` is null and
    `overrideUuid` is true: that combination tells the camera to accept us
    whatever controller identity it currently holds, so a camera already adopted
    elsewhere does not have to be reset first.
    """
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "controllerName": name,
        "controllerUuid": None,
        "controllerVersion": version,
        "overrideUuid": True,
    }


def param_agreement() -> dict[str, Any]:
    return {
        "enableStatusCodes": True,
        "useHeartbeats": False,
        "heartbeatsTimeoutMs": HEARTBEAT_TIMEOUT_MS,
    }


def time_sync_reply(now_ms: int) -> dict[str, Any]:
    """Clock sync happens here, in band. Two timestamps, nothing else."""
    return {"t1": now_ms, "t2": now_ms}


def read_hello(payload: dict[str, Any]) -> dict[str, Any]:
    """The camera announces its own limits; take them rather than assuming."""
    features = payload.get("features")
    return features if isinstance(features, dict) else {}


def apply_hello(camera: Camera, payload: dict[str, Any]) -> None:
    features = read_hello(payload)
    pan, tilt, zoom = ptz.ranges_from_hello(features)
    camera.pan_range, camera.tilt_range, camera.zoom_range = pan, tilt, zoom
    camera.firmware = str(payload.get("fwVersion") or camera.firmware)
    camera.model = str(payload.get("lensmodel") or camera.model)
    smart = features.get("smartDetect")
    if isinstance(smart, list):
        camera.smart_detect = [str(s) for s in smart]
    codecs = features.get("audioCodecs")
    if isinstance(codecs, list):
        camera.audio_codecs = [Codec(c) for c in codecs if c in {"aac", "opus"}]


def video_settings(camera: Camera, ingest_host: str, port: int, tracks: list[str]) -> dict[str, Any]:
    """Arm tracks by giving them a destination; an empty list disarms.

    Audio rides in a two-key block. Every track is pointed at one port and told
    apart by the name we assign it, because the camera may collapse them onto a
    single connection regardless of what we ask for.
    """
    video: dict[str, Any] = {}
    for name in tracks:
        track = camera.track(name)
        if track is None:
            continue
        video[name] = {
            "avSerializer": {
                "type": "extendedFlv",
                "parameters": {
                    "streamName": name,
                    "withOpus": True,
                    "opusSampleRate": 24_000,
                },
                "destinations": [
                    f"tcp://{ingest_host}:{port}?retryInterval=1&connectTimeout=5"
                ],
            },
            "type": track.codec.value,
        }
    return {"audio": {"bitRate": 64_000, "volume": 100}, "video": video}


def disarm(tracks: list[str]) -> dict[str, Any]:
    return {"video": {name: {"avSerializer": {"destinations": []}}} for name in tracks}


def osd_settings(label: str, show_date: bool = True) -> dict[str, Any]:
    """enableOverlay must stay on for anything to render; the logo is ours to drop."""
    slot = {
        "enableDate": 1 if show_date else 0,
        "enableLogo": 0,
        "enableReportdStatsLevel": 0,
        "enableStreamerStatsLevel": 0,
        "tag": label,
    }
    return {
        "_1": dict(slot),
        "_2": dict(slot),
        "_3": dict(slot),
        "_4": dict(slot),
        "enableOverlay": 1,
        "logoScale": 50,
        "overlayColorId": 0,
        "textScale": 50,
        "useCustomLogo": 0,
    }


def service(name: str, start: bool) -> tuple[str, dict[str, Any]]:
    """Service control is one parameterised verb in each direction."""
    return (START_SERVICE if start else STOP_SERVICE), {"service": name}


def snapshot_request(upload_url: str, quality: str = "medium") -> dict[str, Any]:
    """Snapshots are pushed: hand over a one-time URL and the camera POSTs to it."""
    return {"what": "snapshot", "uri": upload_url, "timeoutMs": 60_000, "quality": quality}


@dataclass
class Step:
    name: str
    payload: dict[str, Any]
    expect_ack: bool = True


@dataclass
class Sequence:
    """The ack-gated settings suite.

    Some verbs are never acked even when they are processed, so a step may be
    marked and stepped past on timeout instead of blocking forever.
    """

    steps: list[Step]
    ids: Ids
    _index: int = 0
    _outstanding: int | None = None
    sent: list[Envelope] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self._index >= len(self.steps)

    @property
    def waiting_for(self) -> int | None:
        return self._outstanding

    def next_message(self) -> Envelope | None:
        if self.done or self._outstanding is not None:
            return None
        step = self.steps[self._index]
        message = request(step.name, step.payload, self.ids, expect_reply=step.expect_ack)
        if step.expect_ack:
            self._outstanding = message.message_id
        else:
            self._index += 1
        self.sent.append(message)
        return message

    def on_reply(self, envelope: Envelope) -> bool:
        if self._outstanding is None or envelope.in_response_to != self._outstanding:
            return False
        self._outstanding = None
        self._index += 1
        return True

    def on_timeout(self) -> None:
        """Advance past a step whose ack never comes."""
        self._outstanding = None
        self._index += 1


def suite(camera: Camera, ingest_host: str, port: int, tracks: list[str]) -> list[Step]:
    """The order the camera is happy to receive settings in."""
    return [
        Step(ptz.GET_POSITION, ptz.get_position()),
        Step(CHANGE_VIDEO, video_settings(camera, ingest_host, port, tracks)),
        Step(CHANGE_DEVICE, {"name": camera.name, "timezone": "UTC0"}),
        Step(CHANGE_OSD, osd_settings(camera.name)),
        Step(CHANGE_SOUND_LED, {"ledFaceEnabled": 1, "systemSoundsEnabled": 1}),
    ]


def build_reply(source: Envelope, payload: dict[str, Any], ids: Ids) -> Envelope:
    return reply_to(source, payload, ids)


def is_from_controller(envelope: Envelope) -> bool:
    return envelope.sender == CONTROLLER


def track_defaults() -> list[VideoTrack]:
    """Fallback track table for when the camera has not described its own.

    Real values are read from the camera's settings echo, which is authoritative
    about what it accepted.
    """
    return [
        # Default codec H.264: an ONVIF client (Home Assistant) needs an H.264
        # profile, so a channel armed without an explicit codec gets one. Override
        # per channel with `--tracks video2:h265` or the config file.
        VideoTrack("video1", 1, 0, 2688, 1512, 15, Codec.H264, 1_400_000),
        VideoTrack("video2", 2, 1, 1280, 720, 15, Codec.H264, 500_000),
        VideoTrack("video3", 4, 2, 640, 360, 15, Codec.H264, 300_000),
    ]


def set_track_codecs(camera: Camera, codecs: dict[str, Codec]) -> None:
    """Pin the codec each named track should encode.

    The model is the single source of truth: the same `codec` a track carries is
    what `video_settings` asks the camera to encode and what the ONVIF face
    advertises. Home Assistant's ONVIF integration requires an H.264 profile, so a
    track is pinned to `h264` for it while another stays `h265` — the camera
    encodes both natively (`videoCodecs: [h264, h265, mjpg]`).
    """
    if not codecs:
        return
    camera.tracks = [
        replace(track, codec=codecs.get(track.name, track.codec)) for track in camera.tracks
    ]
