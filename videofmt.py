"""One handle over the two video codecs the camera can push.

The camera encodes HEVC or H.264, and cuckoo re-serves whichever it is as RTP. The
bitstream details live in `pyunifiwire` (`hevc`, `avc`); what differs for *RTP and
SDP* is small and lives here, behind one `VideoFormat` so `media` and `rtsp` never
branch on codec themselves:

* the SDP media name (`H265` / `H264`) and the `a=fmtp` line;
* the NAL header length (2 / 1) and RFC fragmentation-unit type (49 / 28).

A `Stream` picks its `VideoFormat` from the FLV codec id of the first video tag, so
nothing upstream has to be told which codec a track carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final, Protocol

from unifiwire import avc, flv, hevc


class VideoParameters(Protocol):
    """The parameter-set surface both `hevc` and `avc` expose."""

    @property
    def length_size(self) -> int: ...

    @property
    def complete(self) -> bool: ...

    @property
    def sets(self) -> tuple[bytes, ...]: ...


def _hevc_fmtp(payload_type: int, params: VideoParameters) -> str:
    import base64

    assert isinstance(params, hevc.ParameterSets)
    return (
        f"a=fmtp:{payload_type} "
        f"sprop-vps={base64.b64encode(params.vps).decode()};"
        f"sprop-sps={base64.b64encode(params.sps).decode()};"
        f"sprop-pps={base64.b64encode(params.pps).decode()}"
    )


def _avc_fmtp(payload_type: int, params: VideoParameters) -> str:
    assert isinstance(params, avc.ParameterSets)
    line = f"a=fmtp:{payload_type} packetization-mode=1"
    if params.profile_level_id:
        line += f";profile-level-id={params.profile_level_id}"
    return f"{line};sprop-parameter-sets={params.sprop()}"


@dataclass(frozen=True)
class VideoFormat:
    """Everything the RTP/SDP layer needs that depends on the codec."""

    rtpmap: str  # the SDP encoding name
    codec_id: int  # the FLV codec id that selects this format
    nal_header_len: int  # bytes of NAL header (HEVC 2, H.264 1)
    fu_type: int  # RFC 7798 / 6184 fragmentation-unit NAL type
    parse: Callable[[bytes], VideoParameters]  # sequence-header body -> params
    collect: Callable[[list[bytes], int], VideoParameters]  # in-band NAL units -> params
    is_keyframe: Callable[[bytes], bool]  # is this NAL unit a random-access point
    empty: Callable[[], VideoParameters]  # empty params before the header arrives
    fmtp: Callable[[int, VideoParameters], str]  # the SDP a=fmtp line


H265: Final = VideoFormat(
    rtpmap="H265",
    codec_id=flv.CODEC_H265,
    nal_header_len=2,
    fu_type=49,
    parse=hevc.parameter_sets,
    collect=hevc.parameter_sets_from_units,
    is_keyframe=hevc.is_irap,
    empty=hevc.ParameterSets,
    fmtp=_hevc_fmtp,
)

H264: Final = VideoFormat(
    rtpmap="H264",
    codec_id=flv.CODEC_H264,
    nal_header_len=1,
    fu_type=28,
    parse=avc.parameter_sets,
    collect=avc.parameter_sets_from_units,
    is_keyframe=avc.is_keyframe,
    empty=avc.ParameterSets,
    fmtp=_avc_fmtp,
)

_BY_CODEC_ID: Final = {H265.codec_id: H265, H264.codec_id: H264}


def for_codec_id(codec_id: int | None) -> VideoFormat:
    """The format for an FLV codec id, defaulting to HEVC for an unknown one."""
    if codec_id is None:
        return H265
    return _BY_CODEC_ID.get(codec_id, H265)
