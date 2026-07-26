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
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Final

from unifiwire import certs
import discovery
import events
import media
import onvif
import rtsp
import snapshots
from controller import CONTROL_PORT, Controller
from unifiwire.envelope import Envelope
from model import Camera, Position

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
    parser.add_argument("--host", required=True, help="address the camera and clients reach us on")
    parser.add_argument("--control-port", type=int, default=CONTROL_PORT)
    parser.add_argument("--ingest-port", type=int, default=media.INGEST_PORT)
    parser.add_argument("--snapshot-port", type=int, default=snapshots.SNAPSHOT_PORT)
    parser.add_argument("--rtsp-port", type=int, default=rtsp.RTSP_PORT)
    parser.add_argument("--onvif-port", type=int, default=onvif.ONVIF_PORT)
    parser.add_argument("--cert", type=Path, default=Path(DEFAULT_CERT))
    parser.add_argument("--tracks", default="video1", help="comma-separated track names to arm")
    parser.add_argument("--name", default="cuckoo", help="controller name shown to the camera")
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

    options = Options(
        host=args.host,
        cert=args.cert,
        name=args.name,
        tracks=tuple(t for t in args.tracks.split(",") if t),
        control_port=args.control_port,
        ingest_port=args.ingest_port,
        snapshot_port=args.snapshot_port,
        rtsp_port=args.rtsp_port,
        onvif_port=args.onvif_port,
        announce=not args.no_announce,
        dump=args.dump,
    )
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
