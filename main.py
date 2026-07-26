"""Run cuckoo.

    python3 main.py --host 192.168.1.10

`--host` is the address the camera should reach us on; it is written into the
stream destinations, the snapshot upload URL and the PTZ callback, and it is what
ONVIF clients are told to come back to. It has to be routable from the camera and
from the client, so not a loopback.

Assembly lives in `build()` rather than inline, so the whole stack can be started
on ephemeral ports by a test.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Final

import config
from unifiwire import certs
import discovery
import events
import media
import onvif
import rtsp
import snapshots
from controller import CONTROL_PORT, Controller
from unifiwire.envelope import Envelope
from model import Camera, Codec, Position

DEFAULT_CERT: Final = "cuckoo.pem"
SNAPSHOT_WAIT_SEC: Final = 10.0

log = logging.getLogger("cuckoo")


@dataclass
class Options:
    """Everything the stack needs to know. Ports are settable so tests can bind 0."""

    host: str
    cert: Path = Path(DEFAULT_CERT)
    name: str = "cuckoo"
    tracks: tuple[str, ...] = ("video1",)
    track_codecs: dict[str, Codec] = field(default_factory=dict)
    control_port: int = CONTROL_PORT
    ingest_port: int = media.INGEST_PORT
    snapshot_port: int = snapshots.SNAPSHOT_PORT
    rtsp_port: int = rtsp.RTSP_PORT
    onvif_port: int = onvif.ONVIF_PORT
    discovery_port: int = discovery.WS_DISCOVERY_PORT
    announce: bool = True
    dump: Path | None = None


@dataclass
class Stack:
    """Every server, and the controller that drives the camera."""

    options: Options
    controller: Controller
    hub: media.Hub
    ingest: media.IngestServer
    images: snapshots.Store
    uploads: snapshots.SnapshotServer
    stream: rtsp.RtspServer
    services: onvif.Services
    north: onvif.OnvifServer
    finder: discovery.DiscoveryServer

    def start_servers(self) -> None:
        self.ingest.start()
        self.uploads.start()
        self.stream.start()
        self.north.start()
        self.finder.start()

    def stop(self) -> None:
        self.controller.stop()
        for server in (self.finder, self.north, self.stream, self.uploads, self.ingest):
            try:
                server.stop()
            except OSError as exc:  # pragma: no cover - shutdown races
                log.debug("stopping %s: %s", type(server).__name__, exc)

    def run(self) -> None:
        """Serve until stopped. The control channel owns this thread."""
        self.start_servers()
        self.controller.serve_forever()


def spec_to_tracks(spec: str) -> dict[str, str]:
    """Parse a `--tracks` value into `{channel: codec}`.

    Each entry is `name` or `name:codec`, e.g. `video1:h264,video2:h265`. A bare
    name defaults to h264 — the codec an ONVIF client needs — so `--tracks video1`
    arms one H.264 channel. This is the same shape as the config file's `tracks`
    object, so the two are interchangeable and the CLI simply replaces it.
    """
    out: dict[str, str] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if entry:
            name, _, codec = entry.partition(":")
            out[name] = codec or "h264"
    return out


def tracks_to_model(tracks: dict[str, str]) -> tuple[tuple[str, ...], dict[str, Codec]]:
    """Turn a `{channel: codec}` map into the arm order and typed codecs."""
    names: list[str] = []
    codecs: dict[str, Codec] = {}
    for name, codec in tracks.items():
        names.append(name)
        try:
            codecs[name] = Codec(codec)
        except ValueError:
            log.warning("unknown codec %r for track %s; using h264", codec, name)
            codecs[name] = Codec.H264
    return tuple(names), codecs


def resolve_options(args: argparse.Namespace) -> Options:
    """Merge config file, then CLI, into the Options the stack runs on.

    Precedence is defaults < config file < CLI flag. A flag left unset (None) does
    not override the file; a flag given always wins. A missing default config file
    is fine, but a `--config PATH` that does not exist is an error.
    """
    path = args.config or config.DEFAULT_CONFIG_PATH
    if args.config and not os.path.exists(path):
        raise SystemExit(f"config file not found: {path}")
    effective = config.merged(config.load(path))

    if args.host is not None:
        effective["host"] = args.host
    if args.name is not None:
        effective["name"] = args.name
    if args.cert is not None:
        effective["cert"] = str(args.cert)
    if args.no_announce:
        effective["announce"] = False
    if args.tracks is not None:
        effective["tracks"] = spec_to_tracks(args.tracks)
    ports = effective["ports"]
    for flag, key in (
        (args.control_port, "control"), (args.ingest_port, "ingest"),
        (args.snapshot_port, "snapshot"), (args.rtsp_port, "rtsp"),
        (args.onvif_port, "onvif"), (args.discovery_port, "discovery"),
    ):
        if flag is not None:
            ports[key] = flag

    if not effective["host"]:
        raise SystemExit('no host set — pass --host or set "host" in the config file')
    names, codecs = tracks_to_model(effective["tracks"])
    return Options(
        host=effective["host"],
        cert=Path(effective["cert"]),
        name=effective["name"],
        tracks=names,
        track_codecs=codecs,
        control_port=ports["control"],
        ingest_port=ports["ingest"],
        snapshot_port=ports["snapshot"],
        rtsp_port=ports["rtsp"],
        onvif_port=ports["onvif"],
        discovery_port=ports["discovery"],
        announce=effective["announce"],
        dump=args.dump,
    )


def build(options: Options) -> Stack:
    certs.ensure(options.cert, common_name=options.name)

    hub = media.Hub()

    # Listeners first: every URL we hand out has to name the port actually bound,
    # not the one asked for. They differ whenever a port is left to the system.
    ingest = media.IngestServer(
        hub, port=options.ingest_port, fallback_name=options.tracks[0] if options.tracks else "video1"
    )
    ingest_port = int(ingest.server_address[1])
    images = snapshots.Store("https://placeholder")  # rewritten below, once bound
    uploads = snapshots.SnapshotServer(images, cert=options.cert, port=options.snapshot_port)
    snapshot_port = int(uploads.server_address[1])
    images.base_url = f"https://{options.host}:{snapshot_port}"
    stream = rtsp.RtspServer(hub, advertise_host=options.host, port=options.rtsp_port)

    control = Controller(
        cert=options.cert,
        ingest_host=options.host,
        control_port=options.control_port,
        ingest_port=ingest_port,
        tracks=list(options.tracks),
        track_codecs=options.track_codecs,
        name=options.name,
        hub=hub,
        images=images,
    )

    def current() -> Camera | None:
        adopted = [c for c in control.cameras.values() if c.adopted]
        if adopted:
            return adopted[0]
        return next(iter(control.cameras.values()), None)

    def mac_of() -> str | None:
        camera = current()
        return camera.mac if camera is not None else None

    def move(position: Position) -> bool:
        mac = mac_of()
        return control.move(mac, position) if mac else False

    def goto_preset(index: int, speed: int) -> bool:
        mac = mac_of()
        return control.goto_preset(mac, index, speed) if mac else False

    def set_preset(name: str, index: int | None) -> int | None:
        mac = mac_of()
        return control.set_preset(mac, name, index) if mac else None

    def remove_preset(index: int) -> bool:
        mac = mac_of()
        return control.remove_preset(mac, index) if mac else False

    def refresh_position() -> bool:
        mac = mac_of()
        return control.poll_position(mac) if mac else False

    def set_encoder(token: str, codec: str) -> bool:
        mac = mac_of()
        if mac is None:
            return False
        try:
            return control.set_codec(mac, token, Codec(codec))
        except ValueError:
            return False

    def snapshot() -> bytes | None:
        """Ask for a fresh image; fall back to the last one if the camera is slow."""
        mac = mac_of()
        if mac is None:
            return None
        fresh = control.snapshot(mac, timeout=SNAPSHOT_WAIT_SEC)
        return fresh if fresh is not None else images.latest.get(mac)

    backend = onvif.Backend(
        camera=current,
        stream_uri=lambda token: f"rtsp://{options.host}:{stream.port}/{token}",
        snapshot_uri=lambda token: (
            f"http://{options.host}:{north.port}{onvif.SNAPSHOT_PATH}{token}"
        ),
        snapshot=snapshot,
        move_absolute=move,
        move_relative=move,
        goto_preset=goto_preset,
        set_preset=set_preset,
        remove_preset=remove_preset,
        refresh_position=refresh_position,
        set_encoder=set_encoder,
    )
    services = onvif.Services(backend, host=options.host, port=options.onvif_port)
    north = onvif.OnvifServer(services, port=options.onvif_port)
    # The service addresses it advertises must match where it is really listening.
    services.port = north.port

    identity = discovery.identity_for(
        options.host, north.port, onvif.DEVICE_PATH, options.name, ptz=False
    )
    finder = discovery.DiscoveryServer(
        identity, port=options.discovery_port, multicast=options.announce
    )

    def adopted(camera: Camera) -> None:
        log.info(
            "camera ready: %s model=%s ptz=%s pan=%s..%s tilt=%s..%s",
            camera.mac, camera.model or "?", camera.is_ptz,
            camera.pan_range.minimum, camera.pan_range.maximum,
            camera.tilt_range.minimum, camera.tilt_range.maximum,
        )
        # Re-announce now that we know whether to claim PTZ.
        finder.identity = discovery.identity_for(
            options.host, north.port, onvif.DEVICE_PATH,
            camera.name or options.name, ptz=camera.is_ptz,
        )
        if options.announce:
            finder.say_hello()

    def detected(camera: Camera, detection: events.Detection) -> None:
        services.subscriptions.publish(
            onvif.detection_event(camera, detection.kind, detection.active)
        )

    def record(direction: str, message: Envelope) -> None:
        """Write every message to a file. The cheapest way to answer "what did we
        actually send it?" — which is most of the work with this protocol."""
        if options.dump is None:
            return
        with options.dump.open("a") as handle:
            handle.write(json.dumps({
                "direction": direction,
                "functionName": message.function_name,
                "messageId": message.message_id,
                "inResponseTo": message.in_response_to,
                "payload": message.payload,
            }) + "\n")

    control.on_adopted = adopted
    control.on_detection = detected
    control.on_traffic = record

    return Stack(
        options=options,
        controller=control,
        hub=hub,
        ingest=ingest,
        images=images,
        uploads=uploads,
        stream=stream,
        services=services,
        north=north,
        finder=finder,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Present a UniFi camera as an ONVIF device.")
    # Overridable settings default to None so the config file shows through when a
    # flag is omitted; a flag that is given always wins. See resolve_options().
    parser.add_argument("--config", default=None,
                        help=f"JSON config file (default {config.DEFAULT_CONFIG_PATH} if present)")
    parser.add_argument("--host", default=None, help="address the camera and clients reach us on")
    parser.add_argument("--control-port", type=int, default=None)
    parser.add_argument("--ingest-port", type=int, default=None)
    parser.add_argument("--snapshot-port", type=int, default=None)
    parser.add_argument("--rtsp-port", type=int, default=None)
    parser.add_argument("--onvif-port", type=int, default=None)
    parser.add_argument("--discovery-port", type=int, default=None)
    parser.add_argument("--cert", type=Path, default=None)
    parser.add_argument(
        "--tracks", default=None,
        help="comma-separated tracks to arm, each name[:codec] (bare name = h264). "
        "Overrides the config file's tracks. Default: h264 on video1/video2/video3.",
    )
    parser.add_argument("--name", default=None, help="controller name shown to the camera")
    parser.add_argument(
        "--no-announce", action="store_true", help="answer discovery probes but do not multicast"
    )
    parser.add_argument("--dump", type=Path, default=None,
                        help="write every message, in and out, as one JSON object per line")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )

    options = resolve_options(args)
    stack = build(options)

    stopping = threading.Event()

    def shutdown(signum: int, frame: FrameType | None) -> None:
        if stopping.is_set():
            return
        stopping.set()
        log.info("stopping")
        stack.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info(
        "cuckoo up: control :%d, ingest :%d, snapshots :%d, rtsp :%d, onvif :%d%s",
        options.control_port, options.ingest_port, options.snapshot_port,
        options.rtsp_port, options.onvif_port, onvif.DEVICE_PATH,
    )
    stack.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
