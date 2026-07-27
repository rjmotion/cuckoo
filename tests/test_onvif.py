"""The ONVIF front end: SOAP in, SOAP out, and a real HTTP client. No camera.

Requests here are written the way clients actually send them, so the tests fail if
a namespace, token or coordinate convention drifts.
"""

from __future__ import annotations

import http.client
from xml.etree import ElementTree

import onvif
from model import Camera, Position, Preset
from tests_support import PAN_RANGE, TILT_RANGE, ZOOM_RANGE, a_camera


class Recorder:
    """A backend that records what the front end asked the controller to do."""

    def __init__(self, camera: Camera | None) -> None:
        self.camera = camera
        self.absolute: list[Position] = []
        self.presets_gone: list[int] = []
        self.preset_gotos: list[tuple[int, int]] = []
        self.image: bytes | None = b"\xff\xd8jpeg"

    def backend(self) -> onvif.Backend:
        return onvif.Backend(
            camera=lambda: self.camera,
            stream_uri=lambda token: f"rtsp://10.0.0.1:8554/{token}",
            snapshot_uri=lambda token: f"http://10.0.0.1:8000/snapshot/{token}",
            snapshot=lambda: self.image,
            move_absolute=self._move,
            goto_preset=self._goto,
            set_preset=self._set_preset,
            remove_preset=self._remove,
            telemetry=self._telemetry,
        )

    def _telemetry(self) -> dict[str, dict[str, object]]:
        return {
            "video1": {
                "rate_bps": 1_000_000.0,  # 8 Mbps
                "series": [1.0, 4.0, 2.0, 8.0],
                "bytes_in": 5_000_000,
                "frames": 300,
                "keyframes": 6,
                "subscribers": 1,
                "playable": True,
            }
        }

    def _move(self, position: Position) -> bool:
        self.absolute.append(position)
        return True

    def _goto(self, index: int, speed: int) -> bool:
        self.preset_gotos.append((index, speed))
        return True

    def _set_preset(self, name: str, index: int | None) -> int | None:
        assigned = index if index is not None else 7
        if self.camera is not None:
            self.camera.presets[assigned] = Preset(assigned, name, Position())
        return assigned

    def _remove(self, index: int) -> bool:
        self.presets_gone.append(index)
        return True


def services(camera: Camera | None = None) -> tuple[onvif.Services, Recorder]:
    recorder = Recorder(camera if camera is not None else a_camera())
    return onvif.Services(recorder.backend(), host="10.0.0.1", port=8000), recorder


def services_before_adoption() -> tuple[onvif.Services, Recorder]:
    """No camera has connected yet — the front end still has to answer."""
    recorder = Recorder(None)
    return onvif.Services(recorder.backend(), host="10.0.0.1", port=8000), recorder


def request(action: str, body: str = "", namespace: str = "tds") -> onvif.Call:
    payload = onvif.envelope(f'<{namespace}:{action} xmlns:{namespace}="urn:x">{body}</{namespace}:{action}>')
    call = onvif.parse_call(payload.encode())
    assert call is not None
    return call


def ask(service: onvif.Services, action: str, body: str = "") -> str:
    return service.handle(request(action, body))


def text_of(xml: str, local: str) -> str | None:
    root = ElementTree.fromstring(xml)
    for element in root.iter():
        if onvif.local_name(element.tag) == local:
            return (element.text or "").strip()
    return None


def attributes_of(xml: str, local: str) -> dict[str, str]:
    root = ElementTree.fromstring(xml)
    for element in root.iter():
        if onvif.local_name(element.tag) == local:
            return dict(element.attrib)
    return {}


# ------------------------------------------------------------------------ device


def test_device_information_comes_from_the_camera() -> None:
    service, _ = services()
    body = ask(service, "GetDeviceInformation")
    assert text_of(body, "Model") == "UVC G5 PTZ"
    assert text_of(body, "FirmwareVersion") == "5.3.95"
    assert text_of(body, "SerialNumber") == "AABBCCDDEEFF"


def test_capabilities_point_at_our_own_services() -> None:
    service, _ = services()
    body = ask(service, "GetCapabilities")
    for path in (onvif.MEDIA_PATH, onvif.PTZ_PATH, onvif.EVENTS_PATH, onvif.IMAGING_PATH):
        assert f"http://10.0.0.1:8000{path}" in body
    assert "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>" in body


def test_services_list_names_all_five() -> None:
    service, _ = services()
    body = ask(service, "GetServices")
    assert body.count("<tds:Service>") == 5


def test_scopes_declare_ptz_only_for_a_ptz_camera() -> None:
    service, _ = services()
    assert "onvif://www.onvif.org/type/ptz" in ask(service, "GetScopes")
    fixed, _ = services(a_camera(ptz=False))
    assert "onvif://www.onvif.org/type/ptz" not in ask(fixed, "GetScopes")


def test_an_unknown_action_is_a_fault_not_a_crash() -> None:
    service, _ = services()
    assert "s:Fault" in ask(service, "GetDot11Status")


def test_unparseable_body_is_rejected() -> None:
    assert onvif.parse_call(b"<not xml") is None
    assert onvif.parse_call(onvif.envelope("").encode()) is None


# ------------------------------------------------------------------------- media


def test_profiles_describe_the_encoding_and_carry_the_ptz_configuration() -> None:
    service, _ = services()
    body = ask(service, "GetProfiles")
    assert body.count("<trt:Profiles") == 3, "one profile per armed track"
    assert "<tt:Encoding>H264</tt:Encoding>" in body, "default codec"
    assert f'token="{onvif.PTZ_CONFIG}"' in body


def test_profiles_omit_ptz_for_a_fixed_camera() -> None:
    service, _ = services(a_camera(ptz=False))
    assert onvif.PTZ_CONFIG not in ask(service, "GetProfiles")


def _encoder_config(token: str, encoding: str) -> str:
    return (
        f'<trt:Configuration xmlns:trt="urn:x" token="{token}">'
        f'<tt:Encoding xmlns:tt="urn:y">{encoding}</tt:Encoding></trt:Configuration>'
    )


def test_set_video_encoder_configuration_asks_the_controller_to_rearm() -> None:
    recorded: list[tuple[str, str]] = []

    def rearm(token: str, codec: str) -> bool:
        recorded.append((token, codec))
        return True

    camera = a_camera()
    backend = onvif.Backend(
        camera=lambda: camera,
        stream_uri=lambda token: f"rtsp://10.0.0.1/{token}",
        snapshot_uri=lambda token: f"http://10.0.0.1/{token}",
        set_encoder=rearm,
    )
    service = onvif.Services(backend, host="10.0.0.1", port=8000)
    body = ask(service, "SetVideoEncoderConfiguration", _encoder_config("video2", "H265"))
    assert "SetVideoEncoderConfigurationResponse" in body
    assert recorded == [("video2", "h265")]


def test_set_video_encoder_rejects_an_unknown_channel() -> None:
    service, _ = services()
    body = ask(service, "SetVideoEncoderConfiguration", _encoder_config("nope", "H264"))
    assert "Fault" in body


def test_set_video_encoder_rejects_an_unsupported_encoding() -> None:
    service, _ = services()
    body = ask(service, "SetVideoEncoderConfiguration", _encoder_config("video1", "VP9"))
    assert "Fault" in body


def test_stream_uri_is_the_rtsp_url_for_that_profile() -> None:
    service, _ = services()
    body = ask(service, "GetStreamUri", "<ProfileToken>video2</ProfileToken>")
    assert text_of(body, "Uri") == "rtsp://10.0.0.1:8554/video2"


def test_snapshot_uri_is_served_by_us() -> None:
    service, _ = services()
    body = ask(service, "GetSnapshotUri", "<ProfileToken>video1</ProfileToken>")
    assert text_of(body, "Uri") == "http://10.0.0.1:8000/snapshot/video1"


def test_unknown_profile_is_a_fault() -> None:
    service, _ = services()
    assert "s:Fault" in ask(service, "GetProfile", "<ProfileToken>nope</ProfileToken>")


def test_encoder_configurations_match_the_tracks() -> None:
    service, _ = services()
    body = ask(service, "GetVideoEncoderConfigurations")
    assert body.count("<trt:Configurations") == 3


# --------------------------------------------------------------------------- PTZ


def test_status_reports_the_position_in_onvif_units() -> None:
    service, _ = services()
    body = ask(service, "GetStatus")
    position = attributes_of(body, "PanTilt")
    assert abs(float(position["x"]) - PAN_RANGE.to_normalised(18000)) < 1e-4
    assert abs(float(position["y"]) - TILT_RANGE.to_normalised(13000)) < 1e-4
    zoom = attributes_of(body, "Zoom")
    assert abs(float(zoom["x"]) - 0.5) < 0.01, "ONVIF zoom is 0..1, not -1..1"
    assert text_of(body, "PanTilt") is not None


def test_status_says_moving_while_the_head_is_moving() -> None:
    camera = a_camera()
    camera.motion.update(camera.motion.position, activity=1)
    service, _ = services(camera)
    assert "MOVING" in ask(service, "GetStatus")


def test_absolute_move_converts_to_motor_units() -> None:
    service, recorder = services()
    ask(
        service,
        "AbsoluteMove",
        '<Position><PanTilt x="-1.0" y="1.0"/><Zoom x="1.0"/></Position>',
    )
    # ONVIF +Y = up, and this camera's tilt axis is inverted, so y=1.0 (fully up)
    # maps to the tilt *minimum*. The extremes still land on the camera's limits.
    assert recorder.absolute == [
        Position(pan=PAN_RANGE.minimum, tilt=TILT_RANGE.minimum, zoom=ZOOM_RANGE.maximum, focus=50)
    ], "the extremes must land on the camera's own limits, and focus is preserved"


def test_relative_move_is_applied_to_the_current_position() -> None:
    service, recorder = services()
    ask(service, "RelativeMove", '<Translation><PanTilt x="0.1" y="0"/></Translation>')
    moved = recorder.absolute[0]
    span = PAN_RANGE.maximum - PAN_RANGE.minimum
    assert moved.pan == round(18000 + 0.1 * span / 2)
    assert moved.tilt == 13000, "an axis the client left out must not move"


def test_continuous_move_becomes_one_step_rather_than_being_refused() -> None:
    service, recorder = services()
    body = ask(service, "ContinuousMove", '<Velocity><PanTilt x="-0.5" y="0"/></Velocity>')
    assert "ContinuousMoveResponse" in body
    assert recorder.absolute and recorder.absolute[0].pan < 18000


def test_move_is_clamped_to_the_cameras_limits() -> None:
    service, recorder = services()
    ask(service, "AbsoluteMove", '<Position><PanTilt x="-5.0" y="5.0"/></Position>')
    assert recorder.absolute[0].pan == PAN_RANGE.minimum
    # y=+5 (up, past the limit) clamps to the tilt minimum under the inverted axis.
    assert recorder.absolute[0].tilt == TILT_RANGE.minimum


def test_move_without_a_camera_is_a_fault() -> None:
    service, _ = services_before_adoption()
    assert "s:Fault" in ask(service, "AbsoluteMove", '<Position><PanTilt x="0" y="0"/></Position>')


def test_nodes_advertise_the_generic_spaces_clients_look_for() -> None:
    service, _ = services()
    body = ask(service, "GetNodes")
    assert "PanTiltSpaces/PositionGenericSpace" in body
    assert "ZoomSpaces/PositionGenericSpace" in body
    assert "PanTiltSpaces/TranslationGenericSpace" in body


def test_profile_ptz_configuration_declares_relative_and_continuous_defaults() -> None:
    # Home Assistant reads move-mode support ONLY from the PTZConfiguration's
    # Default*Space elements in GetProfiles. With just the absolute defaults it
    # marks the camera PTZ-capable but logs "RelativeMove not supported" and
    # no-ops every relative/continuous move (card buttons and onvif.ptz alike).
    service, _ = services()
    body = ask(service, "GetProfiles")
    assert "DefaultRelativePanTiltTranslationSpace" in body
    assert "DefaultContinuousPanTiltVelocitySpace" in body
    assert "DefaultAbsolutePantTiltPositionSpace" in body


def test_configuration_options_advertise_every_move_space() -> None:
    # Home Assistant decides RelativeMove/ContinuousMove/AbsoluteMove support from
    # this response, not from GetNodes. Missing the Spaces block here is what made
    # HA log "RelativeMove not supported" and no-op every PTZ call.
    service, _ = services()
    body = ask(service, "GetConfigurationOptions", "<ConfigurationToken>PTZConfig</ConfigurationToken>")
    assert "PTZConfigurationOptions" in body
    assert "s:Fault" not in body
    assert "RelativePanTiltTranslationSpace" in body
    assert "AbsolutePanTiltPositionSpace" in body
    assert "ContinuousPanTiltVelocitySpace" in body


def test_presets_are_listed_with_normalised_positions() -> None:
    camera = a_camera()
    camera.presets[3] = Preset(3, "gate", Position(pan=500, tilt=8000, zoom=0, focus=0))
    service, _ = services(camera)
    body = ask(service, "GetPresets")
    assert 'token="3"' in body and "<tt:Name>gate</tt:Name>" in body
    assert attributes_of(body, "PanTilt")["x"] == "-1.0000"
    # tilt at its motor minimum reports as fully up (+1) under the inverted axis.
    assert attributes_of(body, "PanTilt")["y"] == "1.0000"


def test_status_page_renders_live_telemetry() -> None:
    service, _ = services()
    page = service.status_page()
    assert page.startswith("<!doctype html>")
    assert "adopted" in page and "bandwidth" in page
    assert "rtsp://10.0.0.1:8554/" in page  # a real track URI from the backend
    assert "<svg" in page and "Mbps" in page  # the bandwidth sparkline + rate
    assert "prefers-color-scheme" in page  # light default, dark follows the OS


def test_status_page_before_adoption_says_waiting() -> None:
    service, _ = services_before_adoption()
    page = service.status_page()
    assert "waiting" in page and "No camera adopted" in page


def test_tilt_axis_is_inverted_so_onvif_up_raises_the_head() -> None:
    # The user saw up/down reversed: ONVIF +Y means up, but the camera's tilt
    # value grows as the head drops, so "up" must lower the motor value.
    service, recorder = services()
    ask(service, "RelativeMove", '<Translation><PanTilt x="0" y="0.2"/></Translation>')
    assert recorder.absolute[0].tilt < 13000, "ONVIF up must move toward the tilt minimum"


def test_goto_preset_reaches_the_controller() -> None:
    service, recorder = services()
    ask(service, "GotoPreset", "<PresetToken>4</PresetToken>")
    assert recorder.preset_gotos == [(4, 1000)]


def test_non_numeric_preset_token_is_a_fault() -> None:
    service, _ = services()
    assert "s:Fault" in ask(service, "GotoPreset", "<PresetToken>front-door</PresetToken>")


def test_set_preset_returns_the_token_it_assigned() -> None:
    service, _ = services()
    body = ask(service, "SetPreset", "<PresetName>gate</PresetName>")
    assert text_of(body, "PresetToken") == "7"


def test_remove_preset_reaches_the_controller() -> None:
    service, recorder = services()
    ask(service, "RemovePreset", "<PresetToken>2</PresetToken>")
    assert recorder.presets_gone == [2]


# ------------------------------------------------------------------------ events


def test_subscription_reference_carries_its_own_identifier() -> None:
    service, _ = services()
    body = ask(service, "CreatePullPointSubscription")
    address = text_of(body, "Address")
    assert address is not None and "?sub=sub1" in address
    assert service.subscriptions.count == 1


def test_motion_events_reach_a_subscriber_and_are_drained_once() -> None:
    service, _ = services()
    ask(service, "CreatePullPointSubscription")
    camera = a_camera()
    service.subscriptions.publish(onvif.motion_event(camera, active=True))
    body = service.pull_messages("sub1")
    assert onvif.MOTION_TOPIC in body
    assert 'Value="true"' in body
    assert "NotificationMessage" not in service.pull_messages("sub1"), "events are pulled once"


def test_unsubscribe_forgets_the_subscription() -> None:
    service, _ = services()
    ask(service, "CreatePullPointSubscription")
    ask(service, "Unsubscribe")
    assert service.subscriptions.count == 0


def test_event_properties_advertise_the_motion_topic() -> None:
    service, _ = services()
    assert "CellMotionDetector" in ask(service, "GetEventProperties")


def test_a_slow_subscriber_backlog_is_bounded() -> None:
    subscriptions = onvif.Subscriptions()
    identifier = subscriptions.create()
    camera = a_camera()
    for _ in range(onvif.Subscriptions.DEPTH * 2):
        subscriptions.publish(onvif.motion_event(camera, active=True))
    assert len(subscriptions.pull(identifier, limit=1000)) == onvif.Subscriptions.DEPTH


# -------------------------------------------------------------------- over HTTP


def test_a_real_client_can_talk_soap_to_us() -> None:
    service, recorder = services()
    server = onvif.OnvifServer(service, port=0)
    server.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        payload = onvif.envelope(
            '<tds:GetDeviceInformation xmlns:tds="http://www.onvif.org/ver10/device/wsdl"/>'
        )
        connection.request("POST", onvif.DEVICE_PATH, body=payload.encode(),
                           headers={"Content-Type": "application/soap+xml"})
        response = connection.getresponse()
        body = response.read().decode()
        assert response.status == 200
        assert "UVC G5 PTZ" in body

        # The snapshot URI we advertise must actually serve an image.
        connection.request("GET", f"{onvif.SNAPSHOT_PATH}video1")
        image = connection.getresponse()
        assert image.status == 200 and image.read() == b"\xff\xd8jpeg"

        recorder.image = None
        connection.request("GET", f"{onvif.SNAPSHOT_PATH}video1")
        unavailable = connection.getresponse()
        unavailable.read()
        assert unavailable.status == 503, "no image is a 503, not an empty JPEG"
        connection.close()
    finally:
        server.stop()


def test_pull_messages_uses_the_subscription_on_the_url() -> None:
    service, _ = services()
    server = onvif.OnvifServer(service, port=0)
    server.start()
    try:
        ask(service, "CreatePullPointSubscription")
        service.subscriptions.publish(onvif.motion_event(a_camera(), active=True))
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        payload = onvif.envelope(
            '<tev:PullMessages xmlns:tev="http://www.onvif.org/ver10/events/wsdl">'
            "<tev:Timeout>PT1S</tev:Timeout><tev:MessageLimit>10</tev:MessageLimit>"
            "</tev:PullMessages>"
        )
        connection.request("POST", f"{onvif.EVENTS_PATH}?sub=sub1", body=payload.encode())
        body = connection.getresponse().read().decode()
        assert onvif.MOTION_TOPIC in body
        connection.close()
    finally:
        server.stop()



def test_capabilities_are_in_the_onvif_schema_order() -> None:
    """ONVIF Capabilities is a strict sequence (Device, Events, Imaging, Media,
    PTZ). A real client validating against the WSDL drops anything out of order —
    which is how Home Assistant silently lost the Events service and its motion
    sensors. Hand-formed SOAP tests miss this; this pins the order."""
    service, _ = services()
    body = ask(service, "GetCapabilities")
    order = [body.index(f"<tt:{name}>") for name in ("Device", "Events", "Imaging", "Media", "PTZ")]
    assert order == sorted(order), "capabilities must follow the ONVIF schema sequence"
