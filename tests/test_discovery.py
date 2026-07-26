"""WS-Discovery: probe handling and a real UDP round trip. No camera, no multicast."""

from __future__ import annotations

import socket
import time
from xml.etree import ElementTree

import discovery
import onvif

IDENTITY = discovery.identity_for(
    host="10.0.0.1", port=8000, path=onvif.DEVICE_PATH, name="front gate", ptz=True
)


def probe(types: str | None = discovery.NVT_TYPE, message_id: str = "urn:uuid:abc") -> bytes:
    type_element = f"<d:Types>{types}</d:Types>" if types is not None else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        # The 2004/08 WS-Addressing namespace, as real ONVIF clients actually send.
        'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f"<s:Header><a:MessageID>{message_id}</a:MessageID>"
        "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
        "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>"
        f"</s:Header><s:Body><d:Probe>{type_element}</d:Probe></s:Body></s:Envelope>"
    ).encode()


def test_identity_is_stable_across_restarts() -> None:
    """A client that remembers us by endpoint must still recognise us."""
    again = discovery.identity_for("10.0.0.1", 8000, onvif.DEVICE_PATH, "front gate", ptz=True)
    assert again.endpoint == IDENTITY.endpoint
    assert IDENTITY.endpoint.startswith("urn:uuid:")


def test_identity_advertises_ptz_only_when_the_camera_has_it() -> None:
    fixed = discovery.identity_for("10.0.0.1", 8000, onvif.DEVICE_PATH, "hall", ptz=False)
    assert "onvif://www.onvif.org/type/ptz" in IDENTITY.scope_text
    assert "onvif://www.onvif.org/type/ptz" not in fixed.scope_text


def test_probe_for_a_video_transmitter_is_answered() -> None:
    assert discovery.probe_message_id(probe()) == "urn:uuid:abc"


def test_probe_with_no_type_filter_is_answered() -> None:
    assert discovery.probe_message_id(probe(types=None)) == "urn:uuid:abc"


def test_probe_for_something_else_is_ignored() -> None:
    assert discovery.probe_message_id(probe(types="dn:NetworkVideoDisplay")) is None
    assert discovery.probe_message_id(probe(types="tds:PrintService")) is None


def test_a_clients_own_prefix_does_not_matter() -> None:
    assert discovery.probe_message_id(probe(types="wibble:NetworkVideoTransmitter")) is not None


def test_rubbish_is_not_a_probe() -> None:
    assert discovery.probe_message_id(b"<not xml") is None
    assert discovery.probe_message_id(b"<s:Envelope/>") is None


def test_probe_match_relates_to_the_probe_and_names_our_address() -> None:
    xml = discovery.probe_match(IDENTITY, "urn:uuid:abc")
    root = ElementTree.fromstring(xml)
    texts = {
        element.tag.rpartition("}")[2]: (element.text or "").strip() for element in root.iter()
    }
    assert texts["RelatesTo"] == "urn:uuid:abc"
    assert texts["XAddrs"] == f"http://10.0.0.1:8000{onvif.DEVICE_PATH}"
    assert texts["Address"] == IDENTITY.endpoint
    assert "NetworkVideoTransmitter" in texts["Types"]


def test_hello_and_bye_carry_the_same_identity() -> None:
    hello = discovery.announcement(IDENTITY)
    bye = discovery.announcement(IDENTITY, leaving=True)
    assert "<d:Hello>" in hello and "<d:Bye>" in bye
    assert IDENTITY.endpoint in hello and IDENTITY.endpoint in bye


def test_a_real_probe_over_udp_gets_a_real_answer() -> None:
    server = discovery.DiscoveryServer(IDENTITY, port=0, multicast=False)
    server.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(5)
    try:
        client.sendto(probe(), ("127.0.0.1", server.port))
        payload, _ = client.recvfrom(65536)
        body = payload.decode()
        assert "ProbeMatches" in body
        assert f"http://10.0.0.1:8000{onvif.DEVICE_PATH}" in body

        client.sendto(probe(types="dn:NetworkVideoDisplay", message_id="urn:uuid:zzz"), ("127.0.0.1", server.port))
        client.settimeout(0.5)
        try:
            client.recvfrom(65536)
            raise AssertionError("a probe for another device type must not be answered")
        except TimeoutError:
            pass
        deadline = time.time() + 2
        while time.time() < deadline and server.answered < 1:
            time.sleep(0.02)
        assert server.answered == 1
    finally:
        client.close()
        server.stop()



def test_a_real_onvif_client_probe_is_answered() -> None:
    """The exact shape python-ws-discovery (Home Assistant's client) sends: the
    ONVIF type under a random namespace prefix, and WS-Addressing 2004/08. cuckoo
    once looked for the MessageID under 2005/08, found nothing, and silently
    dropped every real probe — so nothing appeared in HA discovery."""
    real = (
        '<?xml version="1.0" ?>'
        '<s:Envelope xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:Zx="http://www.onvif.org/ver10/network/wsdl">'
        "<s:Header>"
        "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>"
        "<a:MessageID>urn:uuid:5bb7eebe-18b6-4d63-9aea-5c63ecbff49c</a:MessageID>"
        "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
        "</s:Header>"
        "<s:Body><d:Probe><d:Types>Zx:NetworkVideoTransmitter</d:Types></d:Probe></s:Body>"
        "</s:Envelope>"
    ).encode()
    assert discovery.probe_message_id(real) == "urn:uuid:5bb7eebe-18b6-4d63-9aea-5c63ecbff49c"
