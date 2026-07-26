"""Media ingest, from bitstream parsing up to a real socket. No camera.

The fixtures build `extendedFlv` by hand — 20 bytes between tags, flags 0x07,
HEVC under codec id 8 — so the tests fail if any of those assumptions drift.
"""

from __future__ import annotations

import socket
import time

from unifiwire import flv
from unifiwire import hevc
import media

VPS = bytes([0x40, 0x01, 0x0C, 0x01])
SPS = bytes([0x42, 0x01, 0x01, 0x60])
PPS = bytes([0x44, 0x01, 0xC0])
IDR = bytes([0x26, 0x01, 0xAA, 0xBB])  # nal_type 19, an IRAP
SLICE = bytes([0x02, 0x01, 0xCC])  # nal_type 1, a trailing picture

AAC_ASC = bytes([0x14, 0x08])  # AAC-LC, 16 kHz, mono
AAC_FRAME = bytes([0x21, 0x00, 0x03])


def hvcc(length_size: int = 4) -> bytes:
    record = bytearray(hevc.HVCC_FIXED_LEN)
    record[0] = 1
    record[21] = 0xFC | (length_size - 1)
    record.append(3)  # three arrays
    for nal_type, unit in ((hevc.NAL_VPS, VPS), (hevc.NAL_SPS, SPS), (hevc.NAL_PPS, PPS)):
        record.append(nal_type)
        record += (1).to_bytes(2, "big")
        record += len(unit).to_bytes(2, "big")
        record += unit
    return bytes(record)


def video_body(frame_type: int, packet_type: int, payload: bytes) -> bytes:
    head = bytes([(frame_type << 4) | flv.CODEC_H265, packet_type, 0, 0, 0])
    return head + payload


def length_prefixed(*units: bytes, length_size: int = 4) -> bytes:
    return b"".join(len(u).to_bytes(length_size, "big") + u for u in units)


def audio_body(packet_type: int, payload: bytes) -> bytes:
    return bytes([(hevc.SOUND_FORMAT_AAC << 4) | 0x0C, packet_type]) + payload


def script_body(name: str) -> bytes:
    encoded = name.encode()
    return b"\x02\x00\x0conMetaData" + b"streamName" + b"\x02" + len(encoded).to_bytes(2, "big") + encoded


def tag_bytes(kind: flv.TagType, timestamp: int, body: bytes) -> bytes:
    out = bytearray()
    out.append(int(kind))
    out += len(body).to_bytes(3, "big")
    out += (timestamp & 0xFFFFFF).to_bytes(3, "big")
    out.append(timestamp >> 24)
    out += b"\x00\x00\x00"  # stream id
    out += body
    out += (flv.TAG_HEADER_LEN + len(body)).to_bytes(flv.PREV_SIZE_LEN, "big")
    out += bytes(flv.TRAILER_LEN)  # the wall-clock trailer this container adds
    return bytes(out)


def stream_bytes(name: str = "video1") -> bytes:
    header = flv.SIGNATURE + bytes([0x01, flv.FLAGS_EXTENDED, 0, 0, 0, flv.HEADER_LEN]) + bytes(4)
    return b"".join([
        header,
        tag_bytes(flv.TagType.SCRIPT, 0, script_body(name)),
        tag_bytes(flv.TagType.VIDEO, 0, video_body(flv.FrameType.SEQUENCE_HEADER, hevc.PACKET_SEQUENCE_HEADER, hvcc())),
        tag_bytes(flv.TagType.AUDIO, 0, audio_body(hevc.AAC_SEQUENCE_HEADER, AAC_ASC)),
        tag_bytes(flv.TagType.VIDEO, 40, video_body(flv.FrameType.KEY, hevc.PACKET_NALU, length_prefixed(IDR))),
        tag_bytes(flv.TagType.AUDIO, 45, audio_body(hevc.AAC_RAW, AAC_FRAME)),
        tag_bytes(flv.TagType.VIDEO, 80, video_body(flv.FrameType.INTER, hevc.PACKET_NALU, length_prefixed(SLICE))),
    ])


# --------------------------------------------------------------------- bitstream


def test_parameter_sets_come_out_of_the_hvcc_record() -> None:
    sets = hevc.parse_hvcc(hvcc())
    assert sets.complete
    assert (sets.vps, sets.sps, sets.pps) == (VPS, SPS, PPS)


def test_length_size_is_read_from_the_record() -> None:
    assert hevc.parse_hvcc(hvcc(length_size=2)).length_size == 2
    assert hevc.parse_hvcc(hvcc(length_size=4)).length_size == 4


def test_truncated_record_is_incomplete_rather_than_an_error() -> None:
    sets = hevc.parse_hvcc(hvcc()[:25])
    assert not sets.complete


def test_annex_b_prefixes_every_set() -> None:
    body = hevc.parse_hvcc(hvcc()).as_annex_b()
    assert body.count(b"\x00\x00\x00\x01") == 3
    assert body.endswith(PPS)


def test_nalus_split_and_a_truncated_tail_is_dropped() -> None:
    payload = length_prefixed(IDR, SLICE) + (99).to_bytes(4, "big") + b"\x01\x02"
    assert list(hevc.split_nalus(payload)) == [IDR, SLICE]


def test_nal_type_and_irap_detection() -> None:
    assert hevc.nal_type(IDR) == 19
    assert hevc.is_irap(IDR)
    assert not hevc.is_irap(SLICE)


def test_negative_composition_time_is_signed() -> None:
    body = bytes([0x18, hevc.PACKET_NALU]) + (-1).to_bytes(3, "big", signed=True)
    packet = hevc.video_packet(body)
    assert packet is not None and packet.composition_time == -1


def test_audio_config_reads_sixteen_kilohertz_mono() -> None:
    assert hevc.audio_config(AAC_ASC) == (2, 16000, 1)


def test_non_aac_audio_is_flagged_rather_than_parsed() -> None:
    packet = hevc.audio_packet(bytes([0x22, 0x00]))
    assert packet is not None and not packet.is_aac


# ------------------------------------------------------------------------ stream


def test_stream_is_not_playable_until_the_parameter_sets_arrive() -> None:
    stream = media.Stream(name="video1")
    assert not stream.ready
    body = video_body(flv.FrameType.SEQUENCE_HEADER, hevc.PACKET_SEQUENCE_HEADER, hvcc())
    stream.accept(flv.Tag(flv.TagType.VIDEO, 0, body))
    assert stream.ready


def test_config_tags_produce_no_frame() -> None:
    stream = media.Stream(name="video1")
    body = video_body(flv.FrameType.SEQUENCE_HEADER, hevc.PACKET_SEQUENCE_HEADER, hvcc())
    assert stream.accept(flv.Tag(flv.TagType.VIDEO, 0, body)) is None
    assert stream.frames == 0


def test_video_frames_carry_their_nal_units() -> None:
    stream = media.Stream(name="video1")
    stream.accept(flv.Tag(flv.TagType.VIDEO, 0, video_body(6, hevc.PACKET_SEQUENCE_HEADER, hvcc())))
    body = video_body(flv.FrameType.KEY, hevc.PACKET_NALU, length_prefixed(IDR))
    frame = stream.accept(flv.Tag(flv.TagType.VIDEO, 40, body))
    assert isinstance(frame, media.VideoFrame)
    assert frame.units == [IDR]
    assert frame.keyframe
    assert frame.timestamp == 40


def test_two_byte_lengths_are_honoured_once_the_record_says_so() -> None:
    """A wrong length size reads garbage sizes, so this has to come from the hvcC."""
    stream = media.Stream(name="video1")
    stream.accept(
        flv.Tag(flv.TagType.VIDEO, 0, video_body(6, hevc.PACKET_SEQUENCE_HEADER, hvcc(length_size=2)))
    )
    body = video_body(flv.FrameType.KEY, hevc.PACKET_NALU, length_prefixed(IDR, length_size=2))
    frame = stream.accept(flv.Tag(flv.TagType.VIDEO, 40, body))
    assert isinstance(frame, media.VideoFrame) and frame.units == [IDR]


def test_audio_config_is_held_and_frames_flow() -> None:
    stream = media.Stream(name="video1")
    stream.accept(flv.Tag(flv.TagType.AUDIO, 0, audio_body(hevc.AAC_SEQUENCE_HEADER, AAC_ASC)))
    assert stream.audio == (2, 16000, 1)
    frame = stream.accept(flv.Tag(flv.TagType.AUDIO, 45, audio_body(hevc.AAC_RAW, AAC_FRAME)))
    assert isinstance(frame, media.AudioFrame) and frame.payload == AAC_FRAME


def test_subscribers_receive_frames() -> None:
    stream = media.Stream(name="video1")
    watcher = stream.subscribe()
    stream.accept(flv.Tag(flv.TagType.VIDEO, 0, video_body(6, hevc.PACKET_SEQUENCE_HEADER, hvcc())))
    body = video_body(flv.FrameType.KEY, hevc.PACKET_NALU, length_prefixed(IDR))
    stream.accept(flv.Tag(flv.TagType.VIDEO, 40, body))
    frame = watcher.take(timeout=0.1)
    assert isinstance(frame, media.VideoFrame)
    stream.unsubscribe(watcher)
    assert stream.subscriber_count == 0


def test_a_slow_subscriber_drops_rather_than_blocking() -> None:
    subscriber = media.Subscriber(depth=2)
    for n in range(5):
        subscriber.offer(media.AudioFrame(timestamp=n, payload=b"x"))
    assert subscriber.dropped == 3
    first = subscriber.take(timeout=0.1)
    assert isinstance(first, media.AudioFrame) and first.timestamp == 3, "oldest is dropped"


# -------------------------------------------------------------------- connection


def test_connection_names_the_stream_from_the_metadata_tag() -> None:
    hub = media.Hub()
    connection = media.Connection(hub, fallback_name="fallback")
    frames = list(connection.feed(stream_bytes("video2")))
    assert connection.named and connection.name == "video2"
    assert hub.names() == ["video2"], "the name from the wire wins over the fallback"
    assert len([f for f in frames if isinstance(f, media.VideoFrame)]) == 2


def test_stream_survives_being_fed_one_byte_at_a_time() -> None:
    hub = media.Hub()
    connection = media.Connection(hub, fallback_name="video1")
    payload = stream_bytes()
    frames: list[media.Frame] = []
    for index in range(len(payload)):
        frames.extend(connection.feed(payload[index : index + 1]))
    stream = hub.get("video1")
    assert stream is not None and stream.ready
    assert stream.frames == 2 and stream.keyframes == 1
    assert len(frames) == 3, "two video frames and one audio frame"


def test_unnamed_stream_falls_back_to_the_track_we_armed() -> None:
    hub = media.Hub()
    connection = media.Connection(hub, fallback_name="video1")
    header = flv.SIGNATURE + bytes([0x01, flv.FLAGS_EXTENDED, 0, 0, 0, flv.HEADER_LEN]) + bytes(4)
    body = video_body(flv.FrameType.SEQUENCE_HEADER, hevc.PACKET_SEQUENCE_HEADER, hvcc())
    list(connection.feed(header + tag_bytes(flv.TagType.VIDEO, 0, body)))
    assert hub.names() == ["video1"]


def test_hub_keeps_concurrent_tracks_apart() -> None:
    hub = media.Hub()
    for name in ("video1", "video3"):
        list(media.Connection(hub, fallback_name="unused").feed(stream_bytes(name)))
    assert hub.names() == ["video1", "video3"]
    one, three = hub.get("video1"), hub.get("video3")
    assert one is not None and three is not None
    assert one.frames == three.frames == 2


# ------------------------------------------------------------------ real socket


def test_ingest_server_accepts_a_real_connection() -> None:
    hub = media.Hub()
    server = media.IngestServer(hub, port=0, fallback_name="video1")
    server.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        assert isinstance(host, str) and isinstance(port, int)
        with socket.create_connection((host, port), timeout=5) as client:
            client.sendall(stream_bytes("video1"))
            deadline = time.time() + 5
            while time.time() < deadline:
                stream = hub.get("video1")
                if stream is not None and stream.frames == 2:
                    break
                time.sleep(0.02)
        stream = hub.get("video1")
        assert stream is not None
        assert stream.ready and stream.frames == 2 and stream.keyframes == 1
        assert stream.audio == (2, 16000, 1)
    finally:
        server.stop()



def real_camera_config_body() -> bytes:
    """A video sequence-header body shaped like a real UVC camera's: FLV byte 0x68,
    packet byte 0x01, then 2-byte-length-prefixed VPS/SPS/PPS — not an hvcC."""
    vps = bytes([0x40, 0x01]) + b"\x0c" * 22
    sps = bytes([0x42, 0x01]) + b"\x60" * 40
    pps = bytes([0x44, 0x01]) + b"\xe0" * 5
    payload = b"".join(len(n).to_bytes(2, "big") + n for n in (vps, sps, pps))
    return bytes([0x68, 0x01]) + payload


def test_a_real_camera_shaped_config_makes_the_stream_ready() -> None:
    """Regression for the RTSP-503: the real camera signals config by frame type
    and packs its sets differently, and cuckoo must still become playable."""
    stream = media.Stream(name="video1")
    result = stream.accept(flv.Tag(flv.TagType.VIDEO, 0, real_camera_config_body()))
    assert result is None, "a config tag yields no frame"
    assert stream.ready, "the real camera's parameter sets were not recognised"
    assert stream.parameters.length_size == 4
