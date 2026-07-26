"""Push a real HEVC file at cuckoo's ingest port, the way a camera would.

This is how the media path gets verified without hardware: encode anything with
ffmpeg, feed it in here, and play the result back out of RTSP with a real client.

    ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=15 -t 5 -c:v libx265 -f hevc /tmp/t.h265
    python3 stubcam.py /tmp/t.h265 --port 7550 --name video1
    ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/video1

It converts Annex B to the container the camera actually sends: `extendedFlv`,
HEVC under codec id 8, length-prefixed NAL units, and 20 bytes between tags.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path
from typing import Iterator

from unifiwire import annexb, flv, hevc
from unifiwire.annexb import annex_b_units, build_hvcc, frames






def tag(kind: flv.TagType, timestamp: int, body: bytes) -> bytes:
    out = bytearray()
    out.append(int(kind))
    out += len(body).to_bytes(3, "big")
    out += (timestamp & 0xFFFFFF).to_bytes(3, "big")
    out.append((timestamp >> 24) & 0xFF)
    out += b"\x00\x00\x00"
    out += body
    out += (flv.TAG_HEADER_LEN + len(body)).to_bytes(flv.PREV_SIZE_LEN, "big")
    out += bytes(flv.TRAILER_LEN)  # the wall-clock trailer this container carries
    return bytes(out)


def script_tag(name: str) -> bytes:
    encoded = name.encode()
    body = (
        b"\x02\x00\x0conMetaData"
        + b"streamName"
        + b"\x02"
        + len(encoded).to_bytes(2, "big")
        + encoded
    )
    return tag(flv.TagType.SCRIPT, 0, body)


def video_tag(units: list[bytes], timestamp: int, keyframe: bool) -> bytes:
    payload = b"".join(len(u).to_bytes(4, "big") + u for u in units)
    frame_type = flv.FrameType.KEY if keyframe else flv.FrameType.INTER
    head = bytes([(int(frame_type) << 4) | flv.CODEC_H265, hevc.PACKET_NALU, 0, 0, 0])
    return tag(flv.TagType.VIDEO, timestamp, head + payload)


def config_tag(record: bytes) -> bytes:
    head = bytes(
        [(int(flv.FrameType.SEQUENCE_HEADER) << 4) | flv.CODEC_H265, hevc.PACKET_SEQUENCE_HEADER, 0, 0, 0]
    )
    return tag(flv.TagType.VIDEO, 0, head + record)




def opening(path: Path, name: str) -> bytes:
    """Header, stream name and parameter sets — sent once per connection.

    Once only: a second header part way through a connection would desynchronise
    the receiver, which is mid-stream and expecting a tag. A real camera opens a
    fresh connection instead.
    """
    units = list(annex_b_units(path.read_bytes()))
    sets = {hevc.nal_type(u): u for u in units if hevc.nal_type(u) in (32, 33, 34)}
    vps, sps, pps = sets.get(32, b""), sets.get(33, b""), sets.get(34, b"")
    if not (vps and sps and pps):
        raise SystemExit("no VPS/SPS/PPS in that file — is it Annex B HEVC?")
    header = flv.SIGNATURE + bytes([0x01, flv.FLAGS_EXTENDED, 0, 0, 0, flv.HEADER_LEN]) + bytes(4)
    return header + script_tag(name) + config_tag(build_hvcc(vps, sps, pps))


def picture_tags(path: Path, fps: int, first_timestamp: int = 0) -> Iterator[bytes]:
    """One tag per picture, timestamps carrying on from where the last pass ended."""
    units = [
        u for u in annex_b_units(path.read_bytes()) if (hevc.nal_type(u) or 0) not in (32, 33, 34)
    ]
    step = max(1, round(1000 / fps))
    timestamp = first_timestamp
    for group, keyframe in frames(units):
        yield video_tag(group, timestamp, keyframe)
        timestamp += step


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push HEVC at cuckoo as a camera would.")
    parser.add_argument("file", type=Path, help="Annex B HEVC, e.g. from ffmpeg -f hevc")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7550)
    parser.add_argument("--name", default="video1", help="stream name to announce")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--loop", action="store_true", help="start again at the end")
    parser.add_argument("--fast", action="store_true", help="do not pace to real time")
    args = parser.parse_args(argv)

    interval = 0.0 if args.fast else 1.0 / max(1, args.fps)
    step = max(1, round(1000 / max(1, args.fps)))
    with socket.create_connection((args.host, args.port), timeout=10) as sock:
        sock.sendall(opening(args.file, args.name))
        timestamp = 0
        while True:
            sent = 0
            for chunk in picture_tags(args.file, args.fps, timestamp):
                sock.sendall(chunk)
                sent += 1
                if interval:
                    time.sleep(interval)
            timestamp += sent * step  # keep time moving forward across passes
            print(f"pushed {sent} pictures", file=sys.stderr)
            if not args.loop:
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
