"""WS-Discovery — how ONVIF clients find us without being told an address.

A client multicasts a Probe to 239.255.255.250:3702; we answer with a ProbeMatch
naming our device service. A Hello goes out at startup and a Bye at shutdown, so a
client that is already listening notices us without probing.

A Probe that asks for a device type we are not is ignored rather than answered
wrongly — answering everything makes us appear as devices we cannot serve.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import uuid
from dataclasses import dataclass
from typing import Final
from xml.etree import ElementTree

WS_DISCOVERY_PORT: Final = 3702
MULTICAST_GROUP: Final = "239.255.255.250"

DISCOVERY_NS: Final = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
# ONVIF pairs the 2005/04 WS-Discovery draft with the **2004/08** WS-Addressing
# namespace, and that is what real ONVIF clients (Home Assistant's included) send
# and expect back. The later 2005/08 addressing namespace is a different spec; a
# device that answers in it, or looks for the client's MessageID under it, is
# silently ignored — the probe's MessageID simply is not found, so no reply goes
# out and the camera never appears in discovery.
ADDRESSING_NS: Final = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
ADDRESSING_NS_2005: Final = "http://www.w3.org/2005/08/addressing"
SOAP_NS: Final = "http://www.w3.org/2003/05/soap-envelope"
NVT_TYPE: Final = "dn:NetworkVideoTransmitter"
DEVICE_TYPE: Final = "tds:Device"

log = logging.getLogger("cuckoo.discovery")


def _envelope(body: str, header: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{SOAP_NS}" xmlns:a="{ADDRESSING_NS}" xmlns:d="{DISCOVERY_NS}" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
        f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
    )


@dataclass(frozen=True)
class Identity:
    """What we say we are when asked."""

    endpoint: str  # a stable urn:uuid for this device
    xaddr: str  # where the device service actually is
    scopes: tuple[str, ...]

    @property
    def scope_text(self) -> str:
        return " ".join(self.scopes)


def identity_for(host: str, port: int, path: str, name: str, ptz: bool) -> Identity:
    scopes = [
        "onvif://www.onvif.org/Profile/Streaming",
        "onvif://www.onvif.org/type/video_encoder",
        f"onvif://www.onvif.org/name/{name.replace(' ', '_')}",
    ]
    if ptz:
        scopes.append("onvif://www.onvif.org/type/ptz")
    # Derived from the address so a restart keeps the same identity.
    endpoint = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'cuckoo://{host}:{port}')}"
    return Identity(endpoint=endpoint, xaddr=f"http://{host}:{port}{path}", scopes=tuple(scopes))


def probe_match(identity: Identity, relates_to: str) -> str:
    header = (
        f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
        f"<a:RelatesTo>{relates_to}</a:RelatesTo>"
        "<a:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:To>"
        f"<a:Action>{DISCOVERY_NS}/ProbeMatches</a:Action>"
    )
    body = (
        "<d:ProbeMatches><d:ProbeMatch>"
        f"<a:EndpointReference><a:Address>{identity.endpoint}</a:Address></a:EndpointReference>"
        f"<d:Types>{NVT_TYPE} {DEVICE_TYPE}</d:Types>"
        f"<d:Scopes>{identity.scope_text}</d:Scopes>"
        f"<d:XAddrs>{identity.xaddr}</d:XAddrs>"
        "<d:MetadataVersion>1</d:MetadataVersion>"
        "</d:ProbeMatch></d:ProbeMatches>"
    )
    return _envelope(body, header)


def announcement(identity: Identity, leaving: bool = False) -> str:
    action = "Bye" if leaving else "Hello"
    header = (
        f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
        f"<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
        f"<a:Action>{DISCOVERY_NS}/{action}</a:Action>"
    )
    body = (
        f"<d:{action}>"
        f"<a:EndpointReference><a:Address>{identity.endpoint}</a:Address></a:EndpointReference>"
        f"<d:Types>{NVT_TYPE} {DEVICE_TYPE}</d:Types>"
        f"<d:Scopes>{identity.scope_text}</d:Scopes>"
        f"<d:XAddrs>{identity.xaddr}</d:XAddrs>"
        "<d:MetadataVersion>1</d:MetadataVersion>"
        f"</d:{action}>"
    )
    return _envelope(body, header)


def probe_message_id(payload: bytes) -> str | None:
    """The MessageID of a Probe we should answer, or None if it is not for us."""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return None
    body = root.find(f"{{{SOAP_NS}}}Body")
    if body is None or body.find(f"{{{DISCOVERY_NS}}}Probe") is None:
        return None
    probe = body.find(f"{{{DISCOVERY_NS}}}Probe")
    assert probe is not None
    types = probe.find(f"{{{DISCOVERY_NS}}}Types")
    if types is not None and types.text:
        wanted = types.text.split()
        # Compare local names: the client's prefixes are its own business.
        locals_wanted = {name.rpartition(":")[2] for name in wanted}
        if not locals_wanted & {"NetworkVideoTransmitter", "Device", "NetworkVideoDisplay"}:
            return None
        if "NetworkVideoDisplay" in locals_wanted and len(locals_wanted) == 1:
            return None
    header = root.find(f"{{{SOAP_NS}}}Header")
    if header is None:
        return None
    # Accept the MessageID under either WS-Addressing namespace: ONVIF clients use
    # 2004/08, but tolerate a client that used 2005/08 rather than dropping it.
    message_id = header.find(f"{{{ADDRESSING_NS}}}MessageID")
    if message_id is None:
        message_id = header.find(f"{{{ADDRESSING_NS_2005}}}MessageID")
    if message_id is None or not message_id.text:
        return None
    return message_id.text.strip()


class DiscoveryServer:
    """Answers Probes. Multicast is optional so this can be driven over unicast."""

    def __init__(
        self, identity: Identity, port: int = WS_DISCOVERY_PORT, multicast: bool = True
    ) -> None:
        self.identity = identity
        self.multicast = multicast
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", port))
        if multicast:
            membership = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        self.sock.settimeout(0.5)
        self.answered = 0
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.sock.getsockname()[1])

    def start(self) -> None:
        log.info("ws-discovery answering on :%d", self.port)
        if self.multicast:
            self.say_hello()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def say_hello(self) -> None:
        self._announce(leaving=False)

    def _announce(self, leaving: bool) -> None:
        try:
            self.sock.sendto(
                announcement(self.identity, leaving).encode(),
                (MULTICAST_GROUP, WS_DISCOVERY_PORT),
            )
        except OSError as exc:
            log.debug("could not announce: %s", exc)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                payload, peer = self.sock.recvfrom(65536)
            except (TimeoutError, OSError):
                continue
            message_id = probe_message_id(payload)
            if message_id is None:
                continue
            try:
                self.sock.sendto(probe_match(self.identity, message_id).encode(), peer)
                self.answered += 1
                log.debug("answered probe from %s", peer[0])
            except OSError as exc:  # pragma: no cover - transient network failure
                log.debug("could not answer probe: %s", exc)

    def stop(self) -> None:
        if self.multicast:
            self._announce(leaving=True)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.sock.close()
