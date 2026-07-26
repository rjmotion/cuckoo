"""Layer tests. No camera, no network, no controller."""

from __future__ import annotations

import json
from typing import Any

import pytest

import adoption
from unifiwire import envelope
from unifiwire import flv
import ptz
from unifiwire import ws
from model import AxisRange, Camera, Codec, Position, Preset, VideoTrack

# --------------------------------------------------------------------------- model


def test_axis_maps_both_ways() -> None:
    axis = AxisRange(500, 35500)
    assert axis.to_normalised(500) == pytest.approx(-1.0)
    assert axis.to_normalised(35500) == pytest.approx(1.0)
    assert axis.from_normalised(-1.0) == 500
    assert axis.from_normalised(1.0) == 35500
    midpoint = axis.from_normalised(0.0)
    assert axis.to_normalised(midpoint) == pytest.approx(0.0, abs=1e-4)


def test_axis_inversion_flips_sense() -> None:
    plain = AxisRange(0, 100)
    flipped = AxisRange(0, 100, invert=True)
    assert plain.to_normalised(100) == pytest.approx(1.0)
    assert flipped.to_normalised(100) == pytest.approx(-1.0)
    assert flipped.from_normalised(-1.0) == 100


def test_axis_clamps_out_of_range() -> None:
    axis = AxisRange(10, 20)
    assert axis.clamp(-5) == 10
    assert axis.clamp(99) == 20
    assert axis.from_normalised(-99.0) == 10


def test_degenerate_axis_is_not_ptz() -> None:
    camera = Camera(mac="AA")
    assert not camera.is_ptz
    assert camera.pan_range.to_normalised(5) == 0.0


def test_motion_settles_on_zero_activity() -> None:
    camera = Camera(mac="AA")
    camera.motion.update(Position(pan=1), activity=16)
    assert not camera.motion.settled
    camera.motion.update(Position(pan=1), activity=0)
    assert camera.motion.settled


# ---------------------------------------------------------------------------- ptz


def _ptz_camera() -> Camera:
    camera = Camera(mac="AA")
    camera.pan_range = AxisRange(500, 35500)
    camera.tilt_range = AxisRange(8000, 18000)
    camera.zoom_range = AxisRange(0, 730)
    return camera


def test_move_is_configure_then_go() -> None:
    messages = ptz.move_to(Position(pan=23502, tilt=8000, zoom=0, focus=59))
    assert [m["action"] for m in messages] == ["config", "go"]
    item = messages[0]["items"][0]
    assert item["pan"] == 23502
    assert item["focus"] == 59
    assert messages[1]["index"] == item["index"]
    assert messages[1]["speed"] == ptz.DEFAULT_SPEED
    assert messages[1]["notifyCommandStatus"] == {}


def test_configure_carries_four_axes() -> None:
    payload = ptz.configure(Preset(3, "corner", Position(1, 2, 3, 4)))
    item = payload["items"][0]
    assert (item["pan"], item["tilt"], item["zoom"], item["focus"]) == (1, 2, 3, 4)


def test_get_position_requests_both_unit_systems() -> None:
    assert ptz.get_position() == {"inDegree": True, "inSteps": True}


def test_enable_carries_our_own_url() -> None:
    assert ptz.enable("wss://host/camera/1.0/ws")["uri"].endswith("/camera/1.0/ws")


def test_parse_motor_state() -> None:
    payload: dict[str, Any] = {
        "ignoreActivity": True,
        "state": {
            "activity": 16,
            "focusMode": "manual",
            "scale": "normalized",
            "position": {"focus": 58, "pan": 23502, "tilt": 8000, "zoom": 0},
            "wallClockMs": 1784989188609,
        },
    }
    parsed = ptz.parse_motor_state(payload)
    assert parsed is not None
    position, activity = parsed
    assert position.pan == 23502
    assert position.focus == 58
    assert activity == 16


@pytest.mark.parametrize("payload", [{}, {"state": 1}, {"state": {}}])
def test_parse_motor_state_rejects_malformed(payload: dict[str, Any]) -> None:
    assert ptz.parse_motor_state(payload) is None


def test_ranges_read_from_hello() -> None:
    features = {
        "pan": {"steps": {"min": 500, "max": 35500}},
        "tilt": {"steps": {"min": 8000, "max": 18000}},
        "zoom": {"steps": {"min": 0, "max": 730}},
    }
    pan, tilt, zoom = ptz.ranges_from_hello(features)
    assert (pan.minimum, pan.maximum) == (500, 35500)
    assert (tilt.minimum, tilt.maximum) == (8000, 18000)
    assert (zoom.minimum, zoom.maximum) == (0, 730)


def test_ranges_absent_are_degenerate() -> None:
    pan, _, _ = ptz.ranges_from_hello({})
    assert pan.minimum == pan.maximum == 0


def test_normalised_round_trip_preserves_focus() -> None:
    camera = _ptz_camera()
    camera.motion.update(Position(focus=42), activity=0)
    position = ptz.normalised_to_position(camera, 0.0, 1.0, -1.0)
    assert position.focus == 42
    assert position.tilt == 18000
    assert position.zoom == 0
    pan, tilt, zoom = ptz.position_to_normalised(camera, position)
    assert tilt == pytest.approx(1.0)
    assert zoom == pytest.approx(-1.0)


# ----------------------------------------------------------------------- adoption


def test_hello_reply_omits_adoption_code_and_features() -> None:
    payload = adoption.hello_reply()
    assert "adoptionCode" not in payload
    assert "features" not in payload
    assert payload["protocolVersion"] == adoption.PROTOCOL_VERSION


def test_hello_reply_overrides_any_held_identity() -> None:
    """null uuid plus overrideUuid true is what lets us take over an adopted camera."""
    payload = adoption.hello_reply()
    assert payload["controllerUuid"] is None
    assert payload["overrideUuid"] is True


def test_param_agreement_shape() -> None:
    assert adoption.param_agreement() == {
        "enableStatusCodes": True,
        "useHeartbeats": False,
        "heartbeatsTimeoutMs": adoption.HEARTBEAT_TIMEOUT_MS,
    }
    assert "authToken" not in adoption.param_agreement()


def test_time_sync_is_two_timestamps() -> None:
    assert adoption.time_sync_reply(1234) == {"t1": 1234, "t2": 1234}


def test_apply_hello_takes_camera_bounds() -> None:
    camera = Camera(mac="AA")
    adoption.apply_hello(
        camera,
        {
            "fwVersion": "5.3.95",
            "features": {
                "pan": {"steps": {"min": 500, "max": 35500}},
                "tilt": {"steps": {"min": 8000, "max": 18000}},
                "zoom": {"steps": {"min": 0, "max": 730}},
                "smartDetect": ["person", "vehicle"],
                "audioCodecs": ["aac", "opus"],
            },
        },
    )
    assert camera.is_ptz
    assert camera.firmware == "5.3.95"
    assert camera.smart_detect == ["person", "vehicle"]
    assert Codec.AAC in camera.audio_codecs


def test_video_settings_arm_and_audio_block() -> None:
    camera = Camera(mac="AA", tracks=adoption.track_defaults())
    payload = adoption.video_settings(camera, "10.0.0.1", 7550, ["video1"])
    assert payload["audio"] == {"bitRate": 64_000, "volume": 100}
    track = payload["video"]["video1"]
    assert track["type"] == "h264"  # default codec
    assert track["avSerializer"]["destinations"][0].startswith("tcp://10.0.0.1:7550")
    assert track["avSerializer"]["parameters"]["withOpus"] is True


def test_disarm_clears_destinations() -> None:
    payload = adoption.disarm(["video1"])
    assert payload["video"]["video1"]["avSerializer"]["destinations"] == []


def test_osd_drops_the_logo_but_keeps_the_overlay() -> None:
    payload = adoption.osd_settings("front door")
    assert payload["enableOverlay"] == 1
    assert payload["_1"]["enableLogo"] == 0
    assert payload["_1"]["tag"] == "front door"


def test_service_verb_is_parameterised() -> None:
    assert adoption.service("ssh", start=True) == ("StartService", {"service": "ssh"})
    assert adoption.service("ssh", start=False) == ("StopService", {"service": "ssh"})


def test_snapshot_request_carries_upload_url() -> None:
    payload = adoption.snapshot_request("https://host:7444/internal/camera-upload/tok")
    assert payload["what"] == "snapshot"
    assert payload["uri"].endswith("/tok")


def test_sequence_is_ack_gated() -> None:
    ids = envelope.Ids()
    seq = adoption.Sequence(
        steps=[adoption.Step("A", {}), adoption.Step("B", {})], ids=ids
    )
    first = seq.next_message()
    assert first is not None and first.function_name == "A"
    # Nothing more is released while an ack is outstanding.
    assert seq.next_message() is None
    ack = envelope.Envelope(
        function_name="A", payload={}, message_id=1, in_response_to=first.message_id,
        sender=envelope.CAMERA,
    )
    assert seq.on_reply(ack)
    second = seq.next_message()
    assert second is not None and second.function_name == "B"


def test_sequence_ignores_unrelated_reply() -> None:
    ids = envelope.Ids()
    seq = adoption.Sequence(steps=[adoption.Step("A", {})], ids=ids)
    seq.next_message()
    stray = envelope.Envelope(
        function_name="A", payload={}, message_id=5, in_response_to=999,
        sender=envelope.CAMERA,
    )
    assert not seq.on_reply(stray)
    assert seq.waiting_for is not None


def test_sequence_steps_past_unacked_verb() -> None:
    ids = envelope.Ids()
    seq = adoption.Sequence(steps=[adoption.Step("A", {}), adoption.Step("B", {})], ids=ids)
    seq.next_message()
    seq.on_timeout()
    nxt = seq.next_message()
    assert nxt is not None and nxt.function_name == "B"


def test_sequence_completes() -> None:
    ids = envelope.Ids()
    seq = adoption.Sequence(steps=[adoption.Step("A", {}, expect_ack=False)], ids=ids)
    seq.next_message()
    assert seq.done


def test_suite_starts_by_asking_position() -> None:
    camera = Camera(mac="AA", tracks=adoption.track_defaults())
    steps = adoption.suite(camera, "10.0.0.1", 7550, ["video1"])
    assert steps[0].name == ptz.GET_POSITION
    assert any(s.name == adoption.CHANGE_VIDEO for s in steps)
