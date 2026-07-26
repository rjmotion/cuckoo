"""The snapshot upload endpoint, including a real TLS POST. No camera."""

from __future__ import annotations

import http.client
import ssl
import threading
from pathlib import Path

from unifiwire import certs
import snapshots

JPEG = b"\xff\xd8\xff\xe0" + b"body" * 32 + b"\xff\xd9"


def test_minted_url_is_the_one_the_camera_is_told_to_post_to() -> None:
    store = snapshots.Store("https://10.0.0.1:7444")
    token, url = store.mint("AABBCCDDEEFF")
    assert url == f"https://10.0.0.1:7444{snapshots.UPLOAD_PREFIX}{token}"


def test_upload_resolves_the_waiter() -> None:
    store = snapshots.Store("https://10.0.0.1:7444")
    token, _ = store.mint("AABBCCDDEEFF")
    got: list[bytes | None] = []
    waiter = threading.Thread(target=lambda: got.append(store.wait(token, timeout=5)))
    waiter.start()
    assert store.claim(token, JPEG)
    waiter.join(timeout=5)
    assert got == [JPEG]
    assert store.latest["AABBCCDDEEFF"] == JPEG


def test_tokens_are_single_use() -> None:
    store = snapshots.Store("https://10.0.0.1:7444")
    token, _ = store.mint("AABBCCDDEEFF")
    assert store.claim(token, JPEG)
    assert not store.claim(token, JPEG), "a replayed token must not be accepted"
    assert store.outstanding == 0


def test_unknown_token_is_refused() -> None:
    store = snapshots.Store("https://10.0.0.1:7444")
    assert not store.claim("not-a-token", JPEG)


def test_expired_token_is_refused_and_forgotten() -> None:
    store = snapshots.Store("https://10.0.0.1:7444")
    token, _ = store.mint("AABBCCDDEEFF")
    pending = store.awaiting(token)
    assert pending is not None
    pending.created_at -= snapshots.TOKEN_TTL_SEC * 2
    assert not store.claim(token, JPEG)


def _client(port: int) -> http.client.HTTPSConnection:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)


def test_real_https_upload_end_to_end(tmp_path: Path) -> None:
    cert = certs.ensure(tmp_path / "test.pem")
    store = snapshots.Store("https://127.0.0.1:7444")
    server = snapshots.SnapshotServer(store, cert=cert, port=0)
    server.start()
    try:
        token, url = store.mint("AABBCCDDEEFF")
        path = url.split("7444", 1)[1]
        connection = _client(server.server_address[1])
        connection.request("POST", path, body=JPEG, headers={"Content-Length": str(len(JPEG))})
        assert connection.getresponse().status == 200
        connection.close()
        assert store.latest["AABBCCDDEEFF"] == JPEG
    finally:
        server.stop()


def test_real_https_upload_accepts_chunked(tmp_path: Path) -> None:
    """The camera picks the framing; a chunked body must not be rejected."""
    cert = certs.ensure(tmp_path / "test.pem")
    store = snapshots.Store("https://127.0.0.1:7444")
    server = snapshots.SnapshotServer(store, cert=cert, port=0)
    server.start()
    try:
        token, _ = store.mint("AABBCCDDEEFF")
        connection = _client(server.server_address[1])
        body = b"%x\r\n%s\r\n0\r\n\r\n" % (len(JPEG), JPEG)
        connection.request(
            "POST",
            f"{snapshots.UPLOAD_PREFIX}{token}",
            body=body,
            headers={"Transfer-Encoding": "chunked"},
        )
        assert connection.getresponse().status == 200
        connection.close()
        assert store.latest["AABBCCDDEEFF"] == JPEG
    finally:
        server.stop()


def test_wrong_path_is_a_404(tmp_path: Path) -> None:
    cert = certs.ensure(tmp_path / "test.pem")
    store = snapshots.Store("https://127.0.0.1:7444")
    server = snapshots.SnapshotServer(store, cert=cert, port=0)
    server.start()
    try:
        connection = _client(server.server_address[1])
        connection.request("POST", "/elsewhere", body=b"", headers={"Content-Length": "0"})
        assert connection.getresponse().status == 404
        connection.close()
    finally:
        server.stop()
