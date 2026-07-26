"""RTSP server — the north-side media face.

What the camera pushes at us is republished here as ordinary RTSP/RTP, so any
client can play it: `ffplay rtsp://<us>:8554/video1`, VLC, or an ONVIF client
following the URI in a media profile.

Two things about this stream are not what a receiver would assume, and both are
handled here rather than left to the client:

* the video is **HEVC or H.264** — whichever the track carries — packetised per
  RFC 7798 / RFC 6184 (single NAL, or FU fragments). The codec is chosen per stream
  by its `VideoFormat` (see `videofmt`), so nothing here branches on it directly.
* the audio is **AAC-LC at 16 kHz mono**, so the RTP clock rate is 16000

Parameter sets go out in the SDP *and* ahead of every keyframe, so a client that
joins mid-stream can start without waiting for the next configuration record.

Transport: TCP interleaved and UDP unicast. Interleaved is the reliable choice on
a busy network and what `-rtsp_transport tcp` selects.
"""

from __future__ import annotations

import logging
import random
import socket
import socketserver
import struct
import threading
from dataclasses import dataclass, field
from typing import Final, Iterator

import media
import videofmt

RTSP_PORT: Final = 8554
VIDEO_PAYLOAD_TYPE: Final = 96
AUDIO_PAYLOAD_TYPE: Final = 97
VIDEO_CLOCK: Final = 90_000
MTU_PAYLOAD: Final = 1400

VIDEO_TRACK: Final = 0
AUDIO_TRACK: Final = 1

log = logging.getLogger("cuckoo.rtsp")


def sdp(host: str, name: str, stream: media.Stream) -> str:
    """Describe the stream, including the parameter sets a decoder needs first."""
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {host}",
        f"s={name}",
        "c=IN IP4 0.0.0.0",
        "t=0 0",
        "a=control:*",
        f"m=video 0 RTP/AVP {VIDEO_PAYLOAD_TYPE}",
        f"a=rtpmap:{VIDEO_PAYLOAD_TYPE} {stream.fmt.rtpmap}/{VIDEO_CLOCK}",
        stream.fmt.fmtp(VIDEO_PAYLOAD_TYPE, stream.parameters),
        f"a=control:trackID={VIDEO_TRACK}",
    ]
    audio = stream.audio
    if audio is not None:
        _, rate, channels = audio
        config = stream.audio_specific_config.hex()
        lines += [
            f"m=audio 0 RTP/AVP {AUDIO_PAYLOAD_TYPE}",
            f"a=rtpmap:{AUDIO_PAYLOAD_TYPE} mpeg4-generic/{rate}/{max(1, channels)}",
            f"a=fmtp:{AUDIO_PAYLOAD_TYPE} streamtype=5;profile-level-id=1;mode=AAC-hbr;"
            f"config={config};sizelength=13;indexlength=3;indexdeltalength=3",
            f"a=control:trackID={AUDIO_TRACK}",
        ]
    return "\r\n".join(lines) + "\r\n"


def rtp_header(
    payload_type: int, sequence: int, timestamp: int, ssrc: int, marker: bool
) -> bytes:
    first = 0x80  # version 2, no padding, no extension, no CSRCs
    second = (0x80 if marker else 0) | payload_type
    return struct.pack("!BBHII", first, second, sequence & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc)


@dataclass
class Packetiser:
    """Turns frames into RTP packets. One instance per track per client."""

    payload_type: int
    clock: int
    ssrc: int
    sequence: int = 0

    def _next(self, timestamp_ms: int, payload: bytes, marker: bool) -> bytes:
        self.sequence = (self.sequence + 1) & 0xFFFF
        timestamp = int(timestamp_ms * self.clock / 1000)
        return rtp_header(self.payload_type, self.sequence, timestamp, self.ssrc, marker) + payload

    def video(
        self,
        frame: media.VideoFrame,
        parameters: videofmt.VideoParameters,
        fmt: videofmt.VideoFormat,
    ) -> Iterator[bytes]:
        units = list(frame.units)
        if frame.keyframe and parameters.complete:
            # Repeat the sets in band: a client that joined mid-stream can start.
            units = list(parameters.sets) + units
        for index, unit in enumerate(units):
            last = index == len(units) - 1
            if len(unit) <= MTU_PAYLOAD:
                yield self._next(frame.timestamp, unit, marker=last)
                continue
            yield from self._fragments(frame.timestamp, unit, fmt, final_unit=last)

    def _fragments(
        self, timestamp_ms: int, unit: bytes, fmt: videofmt.VideoFormat, final_unit: bool
    ) -> Iterator[bytes]:
        """A fragmentation unit: the codec's payload header, then S/E/type per fragment.

        RFC 7798 (HEVC) uses a two-byte FU header; RFC 6184 (H.264) a one-byte one.
        Both then carry a start/end/type byte on every fragment and the NAL body
        with its header stripped.
        """
        if fmt.nal_header_len == 2:  # HEVC — RFC 7798
            kind = (unit[0] >> 1) & 0x3F
            header = bytes([(fmt.fu_type << 1) | (unit[0] & 0x81), unit[1]])
        else:  # H.264 — RFC 6184 FU-A: keep F and NRI, replace type with 28
            kind = unit[0] & 0x1F
            header = bytes([(unit[0] & 0xE0) | fmt.fu_type])
        body = unit[fmt.nal_header_len :]
        chunk = MTU_PAYLOAD - (len(header) + 1)
        offsets = range(0, len(body), chunk)
        for index, at in enumerate(offsets):
            start = index == 0
            end = at + chunk >= len(body)
            fu = bytes([(0x80 if start else 0) | (0x40 if end else 0) | kind])
            yield self._next(
                timestamp_ms, header + fu + body[at : at + chunk], marker=end and final_unit
            )

    def audio(self, frame: media.AudioFrame) -> Iterator[bytes]:
        """AAC-hbr: a two-byte AU header length, then 13 bits of size, 3 of index."""
        size = len(frame.payload) & 0x1FFF
        au_header = struct.pack("!HH", 16, size << 3)
        yield self._next(frame.timestamp, au_header + frame.payload, marker=True)


@dataclass
class Transport:
    """How one track's packets reach this client."""

    interleaved: tuple[int, int] | None = None
    udp_host: str = ""
    udp_port: int = 0

    @property
    def is_tcp(self) -> bool:
        return self.interleaved is not None

    def describe(self, track_udp_port: int = 0) -> str:
        if self.interleaved is not None:
            return f"RTP/AVP/TCP;unicast;interleaved={self.interleaved[0]}-{self.interleaved[1]}"
        return (
            f"RTP/AVP;unicast;client_port={self.udp_port}-{self.udp_port + 1};"
            f"server_port={track_udp_port}-{track_udp_port + 1}"
        )


def parse_transport(header: str) -> Transport | None:
    """Read the client's Transport header. Interleaved wins if both are offered."""
    parts = [p.strip() for p in header.split(";")]
    transport = Transport()
    for part in parts:
        if part.startswith("interleaved="):
            lo, _, hi = part[len("interleaved=") :].partition("-")
            try:
                transport.interleaved = (int(lo), int(hi or int(lo) + 1))
            except ValueError:
                return None
        elif part.startswith("client_port="):
            lo, _, _hi = part[len("client_port=") :].partition("-")
            try:
                transport.udp_port = int(lo)
            except ValueError:
                return None
    if transport.interleaved is None and transport.udp_port == 0:
        return None
    return transport


@dataclass
class Request:
    method: str
    uri: str
    headers: dict[str, str]

    @property
    def cseq(self) -> str:
        return self.headers.get("cseq", "0")

    @property
    def session(self) -> str:
        return self.headers.get("session", "").split(";", 1)[0]

    @property
    def path(self) -> str:
        after_scheme = self.uri.split("://", 1)[-1]
        path = after_scheme.partition("/")[2]
        return "/" + path

    @property
    def stream_name(self) -> str:
        return self.path.lstrip("/").split("/")[0]

    @property
    def track(self) -> int | None:
        marker = "trackID="
        at = self.path.find(marker)
        if at < 0:
            return None
        try:
            return int(self.path[at + len(marker) :].split("/")[0])
        except ValueError:
            return None


def parse_request(text: str) -> Request | None:
    lines = text.split("\r\n")
    if not lines or len(lines[0].split()) < 2:
        return None
    method, uri = lines[0].split()[0].upper(), lines[0].split()[1]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return Request(method=method, uri=uri, headers=headers)


def response(status: str, cseq: str, headers: dict[str, str] | None = None, body: str = "") -> bytes:
    fields = {"CSeq": cseq, "Server": "cuckoo"}
    fields.update(headers or {})
    if body:
        fields.setdefault("Content-Type", "application/sdp")
        fields["Content-Length"] = str(len(body.encode()))
    head = f"RTSP/1.0 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields.items())
    return (head + "\r\n" + body).encode()


@dataclass
class ClientSession:
    """One RTSP session: the transports it set up and the thread feeding it."""

    identifier: str
    stream: media.Stream
    transports: dict[int, Transport] = field(default_factory=dict)
    subscriber: media.Subscriber | None = None
    playing: bool = False
    packetisers: dict[int, Packetiser] = field(default_factory=dict)
    udp: socket.socket | None = None

    def packetiser(self, track: int) -> Packetiser:
        if track not in self.packetisers:
            ssrc = random.getrandbits(32)
            self.packetisers[track] = Packetiser(
                payload_type=VIDEO_PAYLOAD_TYPE if track == VIDEO_TRACK else AUDIO_PAYLOAD_TYPE,
                clock=VIDEO_CLOCK if track == VIDEO_TRACK else self._audio_clock(),
                ssrc=ssrc,
            )
        return self.packetisers[track]

    def _audio_clock(self) -> int:
        audio = self.stream.audio
        return audio[1] if audio is not None else 16_000


def interleaved_frame(channel: int, packet: bytes) -> bytes:
    return b"$" + bytes([channel]) + len(packet).to_bytes(2, "big") + packet


class _Handler(socketserver.BaseRequestHandler):
    """One connected client. Requests are read here; frames go out on a second thread."""

    def setup(self) -> None:
        self.session: ClientSession | None = None
        self.write_lock = threading.Lock()
        self.sender: threading.Thread | None = None
        self.stopping = threading.Event()

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, RtspServer)
        sock = self.request
        assert isinstance(sock, socket.socket)
        buffer = ""
        try:
            while not server.closing.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                while "\r\n\r\n" in buffer:
                    head, _, buffer = buffer.partition("\r\n\r\n")
                    request = parse_request(head)
                    if request is None:
                        continue
                    log.debug("<- %s %s", request.method, request.uri)
                    self._respond(server, sock, request)
        except OSError as exc:
            log.debug("rtsp client ended: %s", exc)
        finally:
            self.stopping.set()
            if self.session is not None and self.session.subscriber is not None:
                self.session.stream.unsubscribe(self.session.subscriber)
            if self.session is not None and self.session.udp is not None:
                self.session.udp.close()
            log.info("rtsp client %s gone", self.client_address[0])

    # ------------------------------------------------------------------- methods

    def _respond(self, server: RtspServer, sock: socket.socket, request: Request) -> None:
        methods = "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"
        if request.method == "OPTIONS":
            self._send(sock, response("200 OK", request.cseq, {"Public": methods}))
            return
        if request.method == "DESCRIBE":
            self._describe(server, sock, request)
            return
        if request.method == "SETUP":
            self._setup(server, sock, request)
            return
        if request.method == "PLAY":
            self._play(sock, request)
            return
        if request.method in ("TEARDOWN",):
            self._send(sock, response("200 OK", request.cseq))
            self.stopping.set()
            sock.close()
            return
        if request.method == "GET_PARAMETER":  # keepalive
            self._send(sock, response("200 OK", request.cseq))
            return
        self._send(sock, response("501 Not Implemented", request.cseq))

    def _describe(self, server: RtspServer, sock: socket.socket, request: Request) -> None:
        stream = server.hub.get(request.stream_name)
        if stream is None:
            self._send(sock, response("404 Not Found", request.cseq))
            return
        if not stream.ready:
            # Honest: the camera has not sent parameter sets, so nothing can decode yet.
            self._send(sock, response("503 Service Unavailable", request.cseq))
            return
        body = sdp(server.advertise_host, request.stream_name, stream)
        self._send(
            sock,
            response("200 OK", request.cseq, {"Content-Base": request.uri.rstrip("/") + "/"}, body),
        )

    def _setup(self, server: RtspServer, sock: socket.socket, request: Request) -> None:
        stream = server.hub.get(request.stream_name)
        header = request.headers.get("transport", "")
        transport = parse_transport(header)
        if stream is None or transport is None:
            self._send(sock, response("461 Unsupported Transport", request.cseq))
            return
        if self.session is None:
            self.session = ClientSession(
                identifier=f"{random.getrandbits(32):08X}", stream=stream
            )
        track = request.track if request.track is not None else VIDEO_TRACK
        server_port = 0
        if not transport.is_tcp:
            transport.udp_host = self.client_address[0]
            if self.session.udp is None:
                self.session.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.session.udp.bind(("0.0.0.0", 0))
            server_port = self.session.udp.getsockname()[1]
        self.session.transports[track] = transport
        self._send(
            sock,
            response(
                "200 OK",
                request.cseq,
                {
                    "Session": f"{self.session.identifier};timeout=60",
                    "Transport": transport.describe(server_port),
                },
            ),
        )

    def _play(self, sock: socket.socket, request: Request) -> None:
        session = self.session
        if session is None or not session.transports:
            self._send(sock, response("454 Session Not Found", request.cseq))
            return
        if not session.playing:
            session.playing = True
            session.subscriber = session.stream.subscribe()
            self.sender = threading.Thread(target=self._pump, args=(sock, session), daemon=True)
            self.sender.start()
        self._send(
            sock,
            response(
                "200 OK",
                request.cseq,
                {"Session": f"{session.identifier};timeout=60", "Range": "npt=now-"},
            ),
        )

    # -------------------------------------------------------------------- sending

    def _pump(self, sock: socket.socket, session: ClientSession) -> None:
        subscriber = session.subscriber
        assert subscriber is not None
        while not self.stopping.is_set():
            frame = subscriber.take(timeout=0.5)
            if frame is None:
                continue
            track = VIDEO_TRACK if isinstance(frame, media.VideoFrame) else AUDIO_TRACK
            transport = session.transports.get(track)
            if transport is None:
                continue
            packetiser = session.packetiser(track)
            if isinstance(frame, media.VideoFrame):
                packets = packetiser.video(frame, session.stream.parameters, session.stream.fmt)
            else:
                packets = packetiser.audio(frame)
            try:
                for packet in packets:
                    self._emit(sock, session, transport, packet)
            except OSError:
                self.stopping.set()
                return

    def _emit(
        self, sock: socket.socket, session: ClientSession, transport: Transport, packet: bytes
    ) -> None:
        if transport.interleaved is not None:
            with self.write_lock:
                sock.sendall(interleaved_frame(transport.interleaved[0], packet))
            return
        if session.udp is not None and transport.udp_port:
            session.udp.sendto(packet, (transport.udp_host, transport.udp_port))

    def _send(self, sock: socket.socket, payload: bytes) -> None:
        with self.write_lock:
            sock.sendall(payload)


class RtspServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, hub: media.Hub, advertise_host: str, port: int = RTSP_PORT) -> None:
        self.hub = hub
        self.advertise_host = advertise_host
        self.closing = threading.Event()
        super().__init__(("0.0.0.0", port), _Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def start(self) -> None:
        log.info("rtsp listening on :%d", self.port)
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.closing.set()
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
