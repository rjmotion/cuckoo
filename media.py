"""Media ingest — the camera connects to us and pushes.

Once a track is armed with a destination, the camera dials that address and writes
`extendedFlv` at it, reconnecting on its own if the socket drops. So this is a
plain TCP listener, one thread per connection.

Tracks are told apart by the `streamName` in the metadata tag and never by port,
because the camera may collapse several tracks onto one connection.

What arrives is republished to subscribers (RTSP, snapshots, anything else) as
decodable frames rather than raw tags, with the parameter sets held aside so a
subscriber that joins mid-stream can still start.
"""

from __future__ import annotations

import logging
import queue
import socket
import socketserver
import threading
from dataclasses import dataclass, field
from typing import Final, Iterator

from unifiwire import flv
from unifiwire import hevc

INGEST_PORT: Final = 7550
MJPEG_PORT: Final = 7551
READ_SIZE: Final = 65536
QUEUE_DEPTH: Final = 120  # a slow subscriber loses frames rather than stalling ingest

log = logging.getLogger("cuckoo.media")


@dataclass(frozen=True)
class VideoFrame:
    timestamp: int
    units: list[bytes]
    keyframe: bool


@dataclass(frozen=True)
class AudioFrame:
    timestamp: int
    payload: bytes


Frame = VideoFrame | AudioFrame


class Subscriber:
    """A bounded view of one stream. Falls behind by dropping, never by blocking."""

    def __init__(self, depth: int = QUEUE_DEPTH) -> None:
        self._queue: queue.Queue[Frame] = queue.Queue(maxsize=depth)
        self.dropped = 0

    def offer(self, frame: Frame) -> None:
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - raced with a reader
                pass
            self.dropped += 1
            try:
                self._queue.put_nowait(frame)
            except queue.Full:  # pragma: no cover - raced with another writer
                pass

    def take(self, timeout: float = 1.0) -> Frame | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


@dataclass
class Stream:
    """Live state for one track: what a late subscriber needs, plus counters."""

    name: str
    codec_id: int | None = None
    parameters: hevc.ParameterSets = field(default_factory=hevc.ParameterSets)
    audio_specific_config: bytes = b""
    frames: int = 0
    keyframes: int = 0
    bytes_in: int = 0
    last_timestamp: int = 0
    _subscribers: list[Subscriber] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def ready(self) -> bool:
        """True once a subscriber joining now could start decoding."""
        return self.parameters.complete

    @property
    def audio(self) -> tuple[int, int, int] | None:
        if not self.audio_specific_config:
            return None
        return hevc.audio_config(self.audio_specific_config)

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber()
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def _fan_out(self, frame: Frame) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for target in targets:
            target.offer(frame)

    def accept(self, tag: flv.Tag) -> Frame | None:
        """Turn one tag into a frame and republish it. Config tags return None."""
        self.bytes_in += len(tag.body)
        self.last_timestamp = tag.timestamp
        if tag.kind is flv.TagType.VIDEO:
            return self._accept_video(tag)
        if tag.kind is flv.TagType.AUDIO:
            return self._accept_audio(tag)
        return None

    def _accept_video(self, tag: flv.Tag) -> Frame | None:
        self.codec_id = tag.codec_id
        # The sequence header is marked by the FLV frame type, not the packet-type
        # byte: a real camera sets that byte to 1 even on config, so keying on it
        # (as the AVC/FLV convention says) misses the parameter sets entirely.
        if tag.is_sequence_header:
            parameters = hevc.parameter_sets(tag.body)
            if parameters.complete:
                self.parameters = parameters
                log.info("%s: parameter sets received, stream is playable", self.name)
            return None
        packet = hevc.video_packet(tag.body)
        if packet is None:
            return None
        units = list(hevc.split_nalus(packet.payload, self.parameters.length_size))
        if not units:
            return None
        frame = VideoFrame(
            timestamp=tag.timestamp,
            units=units,
            keyframe=tag.is_keyframe or any(hevc.is_irap(u) for u in units),
        )
        self.frames += 1
        if frame.keyframe:
            self.keyframes += 1
        self._fan_out(frame)
        return frame

    def _accept_audio(self, tag: flv.Tag) -> Frame | None:
        packet = hevc.audio_packet(tag.body)
        if packet is None or not packet.is_aac:
            return None
        if packet.is_config:
            self.audio_specific_config = packet.payload
            log.info("%s: audio config %s", self.name, self.audio)
            return None
        if not packet.payload:
            return None
        frame = AudioFrame(timestamp=tag.timestamp, payload=packet.payload)
        self._fan_out(frame)
        return frame


class Hub:
    """Every live stream, keyed by the name we assigned the track."""

    def __init__(self) -> None:
        self.streams: dict[str, Stream] = {}
        self._lock = threading.Lock()

    def stream(self, name: str) -> Stream:
        with self._lock:
            return self.streams.setdefault(name, Stream(name=name))

    def get(self, name: str) -> Stream | None:
        with self._lock:
            return self.streams.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self.streams)

    def publish(self, name: str, tag: flv.Tag) -> Frame | None:
        return self.stream(name).accept(tag)


class Connection:
    """One camera connection: deframe, name the stream, hand tags to the hub.

    Split out from the socket so it can be driven byte-for-byte by a test.
    """

    def __init__(self, hub: Hub, fallback_name: str) -> None:
        self.hub = hub
        self.name = fallback_name
        self.named = False
        self._deframer = flv.Deframer()

    def feed(self, chunk: bytes) -> Iterator[Frame]:
        for tag in self._deframer.feed(chunk):
            if tag.kind is flv.TagType.SCRIPT:
                found = flv.stream_name(tag.body)
                if found:
                    self.name = found
                    self.named = True
                    log.info("stream named %s", found)
                continue
            frame = self.hub.publish(self.name, tag)
            if frame is not None:
                yield frame


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, IngestServer)
        peer = self.client_address[0]
        connection = Connection(server.hub, fallback_name=f"{server.fallback_name}")
        log.info("ingest connection from %s", peer)
        sock = self.request
        assert isinstance(sock, socket.socket)
        try:
            while not server.closing.is_set():
                chunk = sock.recv(READ_SIZE)
                if not chunk:
                    break
                for _ in connection.feed(chunk):
                    pass
        except OSError as exc:
            log.debug("ingest connection from %s ended: %s", peer, exc)
        finally:
            stream = server.hub.get(connection.name)
            log.info(
                "ingest connection from %s closed (%s, %d frames)",
                peer, connection.name, stream.frames if stream else 0,
            )


class IngestServer(socketserver.ThreadingTCPServer):
    """Threaded listener for pushed media."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, hub: Hub, port: int = INGEST_PORT, fallback_name: str = "video1") -> None:
        self.hub = hub
        self.fallback_name = fallback_name
        self.closing = threading.Event()
        super().__init__(("0.0.0.0", port), _Handler)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        log.info("media ingest listening on :%d", self.server_address[1])
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.closing.set()
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
