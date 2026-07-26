"""The H.264 media path: ingest, RTSP, and the second ONVIF profile.

Home Assistant's ONVIF integration requires an H.264 profile and rejects an
H.265-only device, so cuckoo arms one track as H.264 and serves it beside the
H.265 one. These build an H.264 `extendedFlv` stream by hand — codec id 7, an
`avcC` sequence header, one-byte NAL headers — and drive it through the same media
and RTSP code the HEVC tests use, then check the ONVIF face advertises both.
"""

from __future__ import annotations

from unifiwire import avc, flv
from unifiwire.annexb import build_avcc
import adoption
import media
import rtsp
import videofmt
from model import Codec
from test_media import length_prefixed
from test_onvif import ask, services
from tests_support import a_camera

# H.264 NAL units: one-byte headers, type in the low five bits (SPS 7, PPS 8,
# IDR 5, non-IDR slice 1). The three bytes after the SPS header are the profile.
SPS = bytes([0x67, 0x64, 0x00, 0x1F, 0xAC, 0xD9])
PPS = bytes([0x68, 0xEE, 0x3C, 0x80])
IDR = bytes([0x65, 0x88, 0x84, 0x21])
SLICE = bytes([0x41, 0x9A, 0x02, 0x03])


def h264_body(frame_type: int, packet_type: int, payload: bytes) -> bytes:
    return bytes([(frame_type << 4) | flv.CODEC_H264, packet_type, 0, 0, 0]) + payload


def config_tag() -> flv.Tag:
    # The real camera signals H.264 config the standard FLV way: frame type 1 (a
    # keyframe) with the AVCPacketType byte 0 and an `avcC` record — not frame type
    # 6 as it does for HEVC.
    return flv.Tag(flv.TagType.VIDEO, 0, h264_body(1, 0, build_avcc(SPS, PPS)))


def ready_h264_stream() -> media.Stream:
    stream = media.Stream(name="video2")
    stream.accept(config_tag())
    return stream


# ------------------------------------------------------------------------- ingest


def test_stream_recognises_h264_and_reads_the_avcc() -> None:
    stream = ready_h264_stream()
    assert stream.fmt is videofmt.H264
    assert stream.parameters.complete and stream.ready
    assert stream.parameters.sets == (SPS, PPS)


def test_an_idr_frame_comes_out_as_a_keyframe() -> None:
    stream = ready_h264_stream()
    frame = stream.accept(flv.Tag(flv.TagType.VIDEO, 40, h264_body(1, 1, length_prefixed(IDR))))
    assert isinstance(frame, media.VideoFrame)
    assert frame.units == [IDR] and frame.keyframe


def test_a_plain_slice_is_not_a_keyframe() -> None:
    stream = ready_h264_stream()
    frame = stream.accept(flv.Tag(flv.TagType.VIDEO, 80, h264_body(2, 1, length_prefixed(SLICE))))
    assert isinstance(frame, media.VideoFrame)
    assert not frame.keyframe


def test_parameter_sets_are_recovered_in_band_without_a_sequence_header() -> None:
    """The real UVC camera sends no H.264 sequence header — it prepends SPS/PPS to
    the keyframe as NAL units. cuckoo must recover them from the frame itself."""
    stream = media.Stream(name="video2")
    # First and only video tag is a keyframe carrying SPS, PPS, then the IDR slice.
    frame = stream.accept(
        flv.Tag(flv.TagType.VIDEO, 0, h264_body(1, 1, length_prefixed(SPS, PPS, IDR)))
    )
    assert stream.fmt is videofmt.H264
    assert stream.ready, "the stream is describable once the in-band sets are seen"
    assert stream.parameters.sets == (SPS, PPS)
    assert isinstance(frame, media.VideoFrame) and frame.keyframe


def test_the_codec_is_taken_from_the_wire_not_assumed() -> None:
    """A fresh stream defaults to HEVC; the first H.264 tag switches it."""
    stream = media.Stream(name="video2")
    assert stream.fmt is videofmt.H265
    stream.accept(config_tag())
    assert stream.fmt is videofmt.H264


# --------------------------------------------------------------------------- SDP


def test_sdp_advertises_h264_with_sprop_and_profile() -> None:
    body = rtsp.sdp("10.0.0.1", "video2", ready_h264_stream())
    assert f"a=rtpmap:{rtsp.VIDEO_PAYLOAD_TYPE} H264/90000" in body
    assert "packetization-mode=1" in body
    assert f"profile-level-id={SPS[1:4].hex()}" in body
    assert "sprop-parameter-sets=" in body
    assert "sprop-vps=" not in body, "H.264 has no VPS"


# ----------------------------------------------------------------- packetisation


def test_keyframe_carries_sps_and_pps_in_band() -> None:
    parameters = avc.parameter_sets(h264_body(6, 1, build_avcc(SPS, PPS)))
    packetiser = rtsp.Packetiser(rtsp.VIDEO_PAYLOAD_TYPE, rtsp.VIDEO_CLOCK, ssrc=1)
    frame = media.VideoFrame(timestamp=0, units=[IDR], keyframe=True)
    payloads = [p[12:] for p in packetiser.video(frame, parameters, videofmt.H264)]
    assert payloads == [SPS, PPS, IDR]


def test_large_nal_uses_a_one_byte_fu_a_header() -> None:
    """RFC 6184 FU-A: a single indicator byte (keeping F/NRI, type 28), then the
    S/E/type byte, then the NAL body with its one-byte header stripped."""
    big = IDR[:1] + bytes(rtsp.MTU_PAYLOAD * 2)
    packetiser = rtsp.Packetiser(rtsp.VIDEO_PAYLOAD_TYPE, rtsp.VIDEO_CLOCK, ssrc=1)
    frame = media.VideoFrame(timestamp=0, units=[big], keyframe=False)
    packets = [p[12:] for p in packetiser.video(frame, avc.ParameterSets(), videofmt.H264)]
    assert len(packets) > 1
    assert all((p[0] & 0x1F) == videofmt.H264.fu_type for p in packets)
    assert all((p[1] & 0x1F) == avc.NAL_IDR for p in packets), "the original type is carried"
    assert packets[0][1] & 0x80, "first fragment sets S"
    assert packets[-1][1] & 0x40, "last fragment sets E"
    nal_byte = (packets[0][0] & 0xE0) | (packets[0][1] & 0x1F)
    rebuilt = bytes([nal_byte]) + b"".join(p[2:] for p in packets)
    assert rebuilt == big


# ----------------------------------------------------------------- both profiles


def test_onvif_advertises_an_h264_profile_beside_the_h265_one() -> None:
    camera = a_camera()  # tracks default to H.264
    adoption.set_track_codecs(camera, {"video2": Codec.H265})
    service, _ = services(camera)
    body = ask(service, "GetProfiles")
    assert "<tt:Encoding>H265</tt:Encoding>" in body
    assert "<tt:Encoding>H264</tt:Encoding>" in body
