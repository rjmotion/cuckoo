"""RTSP: packetisation, request handling, and a real client playing a fed stream.

The end-to-end test pushes a synthetic stream into the ingest side and plays it
out of the RTSP side over TCP interleaved, so the whole media path is exercised
with no camera and no external tools.
"""

from __future__ import annotations

import socket
import time

from unifiwire import flv
from unifiwire import hevc
import media
import rtsp
import videofmt
from test_media import (
    AAC_ASC,
    AAC_FRAME,
    IDR,
    PPS,
    SPS,
    VPS,
    audio_body,
    hvcc,
    length_prefixed,
    stream_bytes,
    tag_bytes,
    video_body,
)


def ready_stream(with_audio: bool = True) -> media.Stream:
    stream = media.Stream(name="video1")
    stream.accept(
        flv.Tag(flv.TagType.VIDEO, 0, video_body(6, hevc.PACKET_SEQUENCE_HEADER, hvcc()))
    )
    if with_audio:
        stream.accept(flv.Tag(flv.TagType.AUDIO, 0, audio_body(hevc.AAC_SEQUENCE_HEADER, AAC_ASC)))
    return stream


# ----------------------------------------------------------------------- the SDP


def test_sdp_advertises_h265_and_the_parameter_sets() -> None:
    body = rtsp.sdp("10.0.0.1", "video1", ready_stream())
    assert f"a=rtpmap:{rtsp.VIDEO_PAYLOAD_TYPE} H265/90000" in body
    assert "sprop-vps=" in body and "sprop-sps=" in body and "sprop-pps=" in body
    assert f"a=control:trackID={rtsp.VIDEO_TRACK}" in body


def test_sdp_states_sixteen_kilohertz_for_audio() -> None:
    """A receiver assuming 44.1 kHz would play the audio at the wrong speed."""
    body = rtsp.sdp("10.0.0.1", "video1", ready_stream())
    assert f"a=rtpmap:{rtsp.AUDIO_PAYLOAD_TYPE} mpeg4-generic/16000/1" in body
    assert "mode=AAC-hbr" in body and "config=1408" in body


def test_sdp_omits_audio_until_the_camera_describes_it() -> None:
    body = rtsp.sdp("10.0.0.1", "video1", ready_stream(with_audio=False))
    assert "m=audio" not in body


# ---------------------------------------------------------------- packetisation


def test_small_nal_goes_out_as_a_single_packet() -> None:
    packetiser = rtsp.Packetiser(rtsp.VIDEO_PAYLOAD_TYPE, rtsp.VIDEO_CLOCK, ssrc=1)
    frame = media.VideoFrame(timestamp=1000, units=[IDR], keyframe=False)
    packets = list(packetiser.video(frame, hevc.ParameterSets(), videofmt.H265))
    assert len(packets) == 1
    assert packets[0][12:] == IDR


def test_timestamp_is_scaled_to_the_ninety_kilohertz_clock() -> None:
    packetiser = rtsp.Packetiser(rtsp.VIDEO_PAYLOAD_TYPE, rtsp.VIDEO_CLOCK, ssrc=1)
    frame = media.VideoFrame(timestamp=1000, units=[IDR], keyframe=False)
    packet = next(iter(packetiser.video(frame, hevc.ParameterSets(), videofmt.H265)))
    assert int.from_bytes(packet[4:8], "big") == 90_000


def test_keyframes_carry_the_parameter_sets_in_band() -> None:
    """So a client joining mid-stream can decode without a fresh config record."""
    parameters = hevc.parse_hvcc(hvcc())
    packetiser = rtsp.Packetiser(rtsp.VIDEO_PAYLOAD_TYPE, rtsp.VIDEO_CLOCK, ssrc=1)
    frame = media.VideoFrame(timestamp=0, units=[IDR], keyframe=True)
    payloads = [p[12:] for p in packetiser.video(frame, parameters, videofmt.H265)]
    assert payloads == [VPS, SPS, PPS, IDR]


def test_large_nal_is_fragmented_with_start_and_end_flags() -> None:
    big = IDR[:2] + bytes(rtsp.MTU_PAYLOAD * 2)
    packetiser = rtsp.Packetiser(rtsp.VIDEO_PAYLOAD_TYPE, rtsp.VIDEO_CLOCK, ssrc=1)
    frame = media.VideoFrame(timestamp=0, units=[big], keyframe=False)
    packets = [p[12:] for p in packetiser.video(frame, hevc.ParameterSets(), videofmt.H265)]
    assert len(packets) > 1
    assert all((p[0] >> 1) & 0x3F == videofmt.H265.fu_type for p in packets)
    assert packets[0][2] & 0x80, "first fragment sets S"
    assert packets[-1][2] & 0x40, "last fragment sets E"
    assert all(p[2] & 0x3F == 19 for p in packets), "the original NAL type is carried"
    rebuilt = big[:2] + b"".join(p[3:] for p in packets)
    assert rebuilt == big


def test_only_the_last_packet_of_a_frame_marks_it() -> None:
    packetiser = rtsp.Packetiser(rtsp.VIDEO_PAYLOAD_TYPE, rtsp.VIDEO_CLOCK, ssrc=1)
    frame = media.VideoFrame(timestamp=0, units=[IDR, IDR], keyframe=False)
    markers = [bool(p[1] & 0x80) for p in packetiser.video(frame, hevc.ParameterSets(), videofmt.H265)]
    assert markers == [False, True]


def test_sequence_numbers_advance() -> None:
    packetiser = rtsp.Packetiser(rtsp.VIDEO_PAYLOAD_TYPE, rtsp.VIDEO_CLOCK, ssrc=1)
    frame = media.VideoFrame(timestamp=0, units=[IDR, IDR, IDR], keyframe=False)
    numbers = [int.from_bytes(p[2:4], "big") for p in packetiser.video(frame, hevc.ParameterSets(), videofmt.H265)]
    assert numbers == [1, 2, 3]


def test_aac_packet_carries_an_au_header() -> None:
    packetiser = rtsp.Packetiser(rtsp.AUDIO_PAYLOAD_TYPE, 16_000, ssrc=1)
    packet = next(iter(packetiser.audio(media.AudioFrame(timestamp=0, payload=AAC_FRAME))))
    assert int.from_bytes(packet[12:14], "big") == 16, "AU header length in bits"
    assert int.from_bytes(packet[14:16], "big") >> 3 == len(AAC_FRAME)
    assert packet[16:] == AAC_FRAME


# ------------------------------------------------------------------- the parsing


def test_transport_prefers_interleaved() -> None:
    transport = rtsp.parse_transport("RTP/AVP/TCP;unicast;interleaved=0-1")
    assert transport is not None and transport.interleaved == (0, 1) and transport.is_tcp


def test_udp_transport_is_understood() -> None:
    transport = rtsp.parse_transport("RTP/AVP;unicast;client_port=50000-50001")
    assert transport is not None and not transport.is_tcp and transport.udp_port == 50000


def test_unusable_transport_is_rejected() -> None:
    assert rtsp.parse_transport("RTP/AVP;multicast") is None


def test_request_paths_and_track_ids() -> None:
    request = rtsp.parse_request("SETUP rtsp://10.0.0.1:8554/video1/trackID=1 RTSP/1.0\r\nCSeq: 3\r\n")
    assert request is not None
    assert request.stream_name == "video1" and request.track == 1 and request.cseq == "3"


def test_request_without_a_track_defaults_to_none() -> None:
    request = rtsp.parse_request("DESCRIBE rtsp://10.0.0.1:8554/video1 RTSP/1.0\r\nCSeq: 1\r\n")
    assert request is not None and request.track is None


def test_rubbish_request_is_not_a_crash() -> None:
    assert rtsp.parse_request("nonsense") is None


def test_interleaved_framing_is_dollar_channel_length() -> None:
    framed = rtsp.interleaved_frame(0, b"abcd")
    assert framed[:1] == b"$" and framed[1] == 0
    assert int.from_bytes(framed[2:4], "big") == 4 and framed[4:] == b"abcd"


# ----------------------------------------------------------------- a real client


class Client:
    """The smallest RTSP client that can prove the server works.

    Once PLAY is in flight, replies and interleaved RTP share the one socket, so
    the reader has to demultiplex them — a real client has the same job.
    """

    def __init__(self, port: int) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.cseq = 0
        self.buffer = b""
        self.packets: list[bytes] = []

    def send(self, method: str, path: str, headers: dict[str, str] | None = None) -> str:
        self.cseq += 1
        lines = [f"{method} rtsp://127.0.0.1{path} RTSP/1.0", f"CSeq: {self.cseq}"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return self.read_response()

    def _take(self) -> tuple[str, bytes] | None:
        """Peel one complete item off the head of the buffer."""
        if not self.buffer:
            return None
        if self.buffer[:1] == b"$":
            if len(self.buffer) < 4:
                return None
            size = int.from_bytes(self.buffer[2:4], "big")
            if len(self.buffer) < 4 + size:
                return None
            packet = self.buffer[4 : 4 + size]
            self.buffer = self.buffer[4 + size :]
            return "rtp", packet
        if b"\r\n\r\n" not in self.buffer:
            return None
        head, _, rest = self.buffer.partition(b"\r\n\r\n")
        length = 0
        for line in head.decode().split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1])
        if len(rest) < length:
            return None
        self.buffer = rest[length:]
        return "rtsp", head + b"\r\n\r\n" + rest[:length]

    def _fill(self, deadline: float) -> bool:
        self.sock.settimeout(max(0.05, deadline - time.time()))
        try:
            chunk = self.sock.recv(65536)
        except (TimeoutError, OSError):
            return False
        if not chunk:
            return False
        self.buffer += chunk
        return True

    def read_response(self, timeout: float = 5.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            item = self._take()
            if item is None:
                if not self._fill(deadline):
                    continue
                continue
            kind, payload = item
            if kind == "rtp":
                self.packets.append(payload)  # stash media, keep looking for the reply
                continue
            return payload.decode("utf-8", errors="replace")
        raise AssertionError("no RTSP response arrived")

    def read_interleaved(self, timeout: float = 5.0) -> bytes | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.packets:
                return self.packets.pop(0)
            item = self._take()
            if item is None:
                self._fill(deadline)
                continue
            kind, payload = item
            if kind == "rtp":
                return payload
        return None

    def close(self) -> None:
        self.sock.close()


def test_a_client_can_describe_setup_and_play_a_fed_stream() -> None:
    hub = media.Hub()
    ingest = media.IngestServer(hub, port=0, fallback_name="video1")
    ingest.start()
    server = rtsp.RtspServer(hub, advertise_host="127.0.0.1", port=0)
    server.start()
    camera: socket.socket | None = None
    client: Client | None = None
    try:
        host, port = ingest.server_address[0], ingest.server_address[1]
        assert isinstance(host, str) and isinstance(port, int)
        camera = socket.create_connection((host, port), timeout=5)
        camera.sendall(stream_bytes("video1"))

        deadline = time.time() + 5
        while time.time() < deadline:
            stream = hub.get("video1")
            if stream is not None and stream.ready:
                break
            time.sleep(0.02)

        client = Client(server.port)
        assert "200 OK" in client.send("OPTIONS", "/video1")

        described = client.send("DESCRIBE", "/video1", {"Accept": "application/sdp"})
        assert "200 OK" in described and "H265/90000" in described

        setup = client.send(
            "SETUP", "/video1/trackID=0", {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"}
        )
        assert "200 OK" in setup and "interleaved=0-1" in setup
        session = next(
            line.split(":", 1)[1].strip().split(";")[0]
            for line in setup.split("\r\n")
            if line.lower().startswith("session:")
        )

        assert "200 OK" in client.send("PLAY", "/video1", {"Session": session})

        # Push a keyframe now that the client is playing, and expect it back as RTP.
        camera.sendall(
            tag_bytes(
                flv.TagType.VIDEO,
                200,
                video_body(flv.FrameType.KEY, hevc.PACKET_NALU, length_prefixed(IDR)),
            )
        )
        packet = client.read_interleaved()
        assert packet is not None, "no RTP arrived on the interleaved channel"
        assert packet[0] >> 6 == 2, "RTP version 2"
        assert packet[1] & 0x7F == rtsp.VIDEO_PAYLOAD_TYPE
        assert "200 OK" in client.send("TEARDOWN", "/video1", {"Session": session})
    finally:
        if client is not None:
            client.close()
        if camera is not None:
            camera.close()
        server.stop()
        ingest.stop()


def test_describe_before_the_camera_sends_parameter_sets_is_honest() -> None:
    hub = media.Hub()
    hub.stream("video1")  # exists, but nothing decodable yet
    server = rtsp.RtspServer(hub, advertise_host="127.0.0.1", port=0)
    server.start()
    client: Client | None = None
    try:
        client = Client(server.port)
        assert "503" in client.send("DESCRIBE", "/video1")
        assert "404" in client.send("DESCRIBE", "/nosuchtrack")
    finally:
        if client is not None:
            client.close()
        server.stop()
