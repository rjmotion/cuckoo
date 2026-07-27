"""The whole daemon, assembled and running, with a fake camera. No hardware.

This is the test that catches wiring mistakes the per-layer tests cannot: a camera
connects over real TLS, is adopted, pushes media, and an ONVIF client then sees the
camera it should — same process, all sockets real, no camera on the network.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import adoption
from unifiwire import envelope
import events
from unifiwire import flv
from unifiwire import hevc
import main
import onvif
import ptz
from unifiwire import ws
from test_media import IDR, hvcc, length_prefixed, stream_bytes, tag_bytes, video_body

HELLO_PAYLOAD: dict[str, Any] = {
    "fwVersion": "5.3.95",
    "features": {
        "pan": {"steps": {"min": 500, "max": 35500}},
        "tilt": {"steps": {"min": 8000, "max": 18000}},
        "zoom": {"steps": {"min": 0, "max": 730}},
        "mic": 1,
        "smartDetect": ["person", "vehicle"],
    },
}


class FakeCamera:
    """Dials the control channel over TLS and behaves like the real thing."""

    def __init__(self, port: int, mac: str = "AABBCCDDEEFF") -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock = context.wrap_socket(raw)
        self.sock.sendall(
            b"GET /camera/1.0/ws HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Protocol: secure_transfer\r\n"
            + f"camera-mac: {mac}\r\n".encode()
            + b"camera-model: UVC G5 PTZ\r\nadopted: false\r\n\r\n"
        )
        assert b"101" in self.sock.recv(1024)
        self.mac = mac
        self.reader = ws.FrameReader()
        self.received: list[envelope.Envelope] = []
        self.next_id = 1000

    def send(self, name: str, payload: dict[str, Any], reply_to: int = 0) -> None:
        self.next_id += 1
        message = envelope.Envelope(
            function_name=name,
            payload=payload,
            message_id=self.next_id,
            in_response_to=reply_to,
            sender=envelope.CAMERA,
            recipient=envelope.CONTROLLER,
        )
        ws.send(self.sock, message.to_json())

    def pump(self, seconds: float = 1.0) -> list[envelope.Envelope]:
        """Read whatever the controller sent, acking every settings message."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.sock.settimeout(max(0.05, deadline - time.time()))
            try:
                chunk = self.sock.recv(65536)
            except (TimeoutError, OSError):
                continue
            if not chunk:
                break
            for frame in self.reader.feed(chunk):
                if frame.opcode is not ws.Opcode.BINARY and frame.opcode is not ws.Opcode.TEXT:
                    continue
                try:
                    message = envelope.decode(frame.payload)
                except envelope.DecodeError:
                    continue
                self.received.append(message)
                if message.function_name != envelope.HELLO:
                    self.send(message.function_name, {}, reply_to=message.message_id)
        return self.received

    def names(self) -> list[str]:
        return [m.function_name for m in self.received]

    def close(self) -> None:
        self.sock.close()


def soap(port: int, path: str, action: str, namespace: str, body: str = "") -> str:
    payload = onvif.envelope(
        f'<x:{action} xmlns:x="{namespace}">{body}</x:{action}>'
    )
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("POST", path, body=payload.encode(),
                       headers={"Content-Type": "application/soap+xml"})
    response = connection.getresponse().read().decode()
    connection.close()
    return response


def text_of(xml: str, local: str) -> str | None:
    for element in ElementTree.fromstring(xml).iter():
        if onvif.local_name(element.tag) == local:
            return (element.text or "").strip()
    return None


def a_stack(tmp_path: Path) -> main.Stack:
    options = main.Options(
        host="127.0.0.1",
        cert=tmp_path / "stack.pem",
        name="cuckoo test",
        tracks=("video1",),
        control_port=0,
        ingest_port=0,
        snapshot_port=0,
        rtsp_port=0,
        onvif_port=0,
        discovery_port=0,
        announce=False,  # no multicast from a test
    )
    stack = main.build(options)
    stack.start_servers()
    return stack


def control_port(stack: main.Stack) -> int:
    """The controller binds when it starts serving, so run it and wait for the port."""
    thread = threading.Thread(target=stack.controller.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        listener = stack.controller._listener  # noqa: SLF001 - test needs the bound port
        if listener is not None:
            return int(listener.getsockname()[1])
        time.sleep(0.02)
    raise AssertionError("the control channel never bound")


def test_a_camera_is_adopted_and_the_onvif_face_reflects_it(tmp_path: Path) -> None:
    stack = a_stack(tmp_path)
    camera: FakeCamera | None = None
    try:
        port = control_port(stack)
        camera = FakeCamera(port)
        camera.send(envelope.HELLO, HELLO_PAYLOAD)
        camera.pump(3.0)

        model = stack.controller.cameras.get("AABBCCDDEEFF")
        assert model is not None and model.adopted, "the fake camera should be adopted"
        assert ptz.ENABLE_PTZ in camera.names(), "a PTZ camera should be asked for its channel"
        assert adoption.CHANGE_VIDEO in camera.names()

        # The settings the camera was sent must point back at this process.
        armed = next(m for m in camera.received if m.function_name == adoption.CHANGE_VIDEO)
        destination = armed.payload["video"]["video1"]["avSerializer"]["destinations"][0]
        assert f":{stack.ingest.server_address[1]}" in destination, (
            "the camera must be pointed at the port we actually bound"
        )

        device = soap(
            stack.north.port, onvif.DEVICE_PATH, "GetDeviceInformation",
            "http://www.onvif.org/ver10/device/wsdl",
        )
        assert text_of(device, "SerialNumber") == "AABBCCDDEEFF"
        assert text_of(device, "FirmwareVersion") == "5.3.95"

        profiles = soap(
            stack.north.port, onvif.MEDIA_PATH, "GetProfiles",
            "http://www.onvif.org/ver10/media/wsdl",
        )
        assert "H264" in profiles and onvif.PTZ_CONFIG in profiles

        uri = soap(
            stack.north.port, onvif.MEDIA_PATH, "GetStreamUri",
            "http://www.onvif.org/ver10/media/wsdl", "<ProfileToken>video1</ProfileToken>",
        )
        assert text_of(uri, "Uri") == f"rtsp://127.0.0.1:{stack.stream.port}/video1"
    finally:
        if camera is not None:
            camera.close()
        stack.stop()


def test_ptz_from_the_onvif_side_reaches_the_camera(tmp_path: Path) -> None:
    stack = a_stack(tmp_path)
    camera: FakeCamera | None = None
    try:
        port = control_port(stack)
        camera = FakeCamera(port)
        camera.send(envelope.HELLO, HELLO_PAYLOAD)
        camera.pump(3.0)
        before = len([m for m in camera.received if m.function_name == ptz.PRESET])

        soap(
            stack.north.port, onvif.PTZ_PATH, "AbsoluteMove",
            "http://www.onvif.org/ver20/ptz/wsdl",
            '<Position><PanTilt x="-1.0" y="1.0"/></Position>',
        )
        camera.pump(1.0)
        presets = [m for m in camera.received if m.function_name == ptz.PRESET]
        assert len(presets) >= before + 2, "a move is a config then a go"
        configured = next(m for m in presets if m.payload.get("action") == "config")
        item = configured.payload["items"][0]
        # ONVIF +Y = up and the camera's tilt axis is inverted, so y=1.0 (fully up)
        # lands on the tilt minimum. Both axes clamp to the camera's own limits.
        assert item["pan"] == 500 and item["tilt"] == 8000, "clamped to the camera's own limits"

        status = soap(
            stack.north.port, onvif.PTZ_PATH, "GetStatus", "http://www.onvif.org/ver20/ptz/wsdl"
        )
        assert "PTZStatus" in status
    finally:
        if camera is not None:
            camera.close()
        stack.stop()


def test_a_detection_reaches_an_onvif_subscriber(tmp_path: Path) -> None:
    stack = a_stack(tmp_path)
    camera: FakeCamera | None = None
    try:
        port = control_port(stack)
        camera = FakeCamera(port)
        camera.send(envelope.HELLO, HELLO_PAYLOAD)
        camera.pump(2.0)

        created = soap(
            stack.north.port, onvif.EVENTS_PATH, "CreatePullPointSubscription",
            "http://www.onvif.org/ver10/events/wsdl",
        )
        address = text_of(created, "Address")
        assert address is not None
        subscription = address.rpartition("sub=")[2]

        camera.send(
            events.EVENT_SMART_DETECT,
            {"eventType": "smartDetectZone", "edgeType": "enter", "objectTypes": ["person"]},
        )
        deadline = time.time() + 5
        body = ""
        while time.time() < deadline:
            body = soap(
                stack.north.port, f"{onvif.EVENTS_PATH}?sub={subscription}", "PullMessages",
                "http://www.onvif.org/ver10/events/wsdl",
                "<Timeout>PT1S</Timeout><MessageLimit>10</MessageLimit>",
            )
            if "PeopleDetect" in body:
                break
            time.sleep(0.1)
        assert "PeopleDetect" in body, "the camera's person detection should reach the subscriber"
        assert 'Value="true"' in body
    finally:
        if camera is not None:
            camera.close()
        stack.stop()


def test_pushed_media_becomes_a_playable_profile(tmp_path: Path) -> None:
    """DESCRIBE only succeeds once real frames have arrived through ingest."""
    stack = a_stack(tmp_path)
    pusher: socket.socket | None = None
    try:
        before = soap(
            stack.north.port, onvif.MEDIA_PATH, "GetSnapshotUri",
            "http://www.onvif.org/ver10/media/wsdl", "<ProfileToken>video1</ProfileToken>",
        )
        assert f":{stack.north.port}" in (text_of(before, "Uri") or "")

        pusher = socket.create_connection(("127.0.0.1", stack.ingest.server_address[1]), timeout=5)
        pusher.sendall(stream_bytes("video1"))
        pusher.sendall(
            tag_bytes(
                flv.TagType.VIDEO, 300,
                video_body(flv.FrameType.KEY, hevc.PACKET_NALU, length_prefixed(IDR)),
            )
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            stream = stack.hub.get("video1")
            if stream is not None and stream.ready and stream.frames >= 3:
                break
            time.sleep(0.02)
        stream = stack.hub.get("video1")
        assert stream is not None and stream.ready and stream.audio == (2, 16000, 1)

        rtsp_sock = socket.create_connection(("127.0.0.1", stack.stream.port), timeout=5)
        rtsp_sock.sendall(
            f"DESCRIBE rtsp://127.0.0.1:{stack.stream.port}/video1 RTSP/1.0\r\n"
            "CSeq: 1\r\nAccept: application/sdp\r\n\r\n".encode()
        )
        described = rtsp_sock.recv(4096).decode()
        rtsp_sock.close()
        assert "200 OK" in described and "H265/90000" in described
    finally:
        if pusher is not None:
            pusher.close()
        stack.stop()


def test_snapshot_request_and_upload_round_trip(tmp_path: Path) -> None:
    """The camera is asked for a snapshot and uploads it to the URL we minted."""
    stack = a_stack(tmp_path)
    camera: FakeCamera | None = None
    try:
        port = control_port(stack)
        camera = FakeCamera(port)
        camera.send(envelope.HELLO, HELLO_PAYLOAD)
        camera.pump(2.0)

        image = b"\xff\xd8" + b"jpeg" * 16 + b"\xff\xd9"
        got: list[bytes | None] = []

        def fetch() -> None:
            got.append(stack.controller.snapshot("AABBCCDDEEFF", timeout=8.0))

        asking = threading.Thread(target=fetch)
        asking.start()

        # Play the camera: find the GetRequest, then POST to the URL it carries.
        deadline = time.time() + 5
        url = ""
        while time.time() < deadline and not url:
            camera.pump(0.3)
            for message in camera.received:
                if message.function_name == adoption.GET_REQUEST:
                    url = str(message.payload["uri"])
            time.sleep(0.05)
        assert url, "the camera was never asked for a snapshot"
        assert url.startswith(f"https://127.0.0.1:{stack.uploads.server_address[1]}")

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        upload = http.client.HTTPSConnection(
            "127.0.0.1", stack.uploads.server_address[1], context=context, timeout=5
        )
        upload.request("POST", url.split(str(stack.uploads.server_address[1]), 1)[1], body=image,
                       headers={"Content-Length": str(len(image))})
        assert upload.getresponse().status == 200
        upload.close()

        asking.join(timeout=10)
        assert got == [image]

        # And the ONVIF snapshot URI now serves it.
        connection = http.client.HTTPConnection("127.0.0.1", stack.north.port, timeout=15)
        connection.request("GET", f"{onvif.SNAPSHOT_PATH}video1")
        served = connection.getresponse()
        body = served.read()
        connection.close()
        assert served.status == 200 and body == image
    finally:
        if camera is not None:
            camera.close()
        stack.stop()


def test_discovery_answers_a_probe_for_the_running_stack(tmp_path: Path) -> None:
    import discovery as ws_discovery
    from test_discovery import probe

    stack = a_stack(tmp_path)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(5)
    try:
        client.sendto(probe(), ("127.0.0.1", stack.finder.port))
        payload, _ = client.recvfrom(65536)
        body = payload.decode()
        assert "ProbeMatches" in body
        assert f"http://127.0.0.1:{stack.north.port}{onvif.DEVICE_PATH}" in body
        assert ws_discovery.NVT_TYPE in body
    finally:
        client.close()
        stack.stop()
