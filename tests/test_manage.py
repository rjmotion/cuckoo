"""The /api/1.2/manage takeover, against a fake camera HTTP server. No hardware.

The fake stands in for the camera's management endpoint: it demands a login, sets
a cookie, and accepts a manage POST only when that cookie comes back — the same
shape the real camera enforces.
"""

from __future__ import annotations

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from unifiwire import certs
import manage


class FakeCamera(BaseHTTPRequestHandler):
    username = "admin"
    password = "secret"
    cookie = "deadbeef"
    received: dict[str, object] = {}

    def log_message(self, *a: object) -> None:
        pass

    def _json(self, code: int, body: dict[str, object], headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == manage.LOGIN_PATH:
            if body.get("username") == self.username and body.get("password") == self.password:
                self._json(200, {"status": "ok"}, {"Set-Cookie": f"TOKEN={self.cookie}; Path=/"})
            else:
                self._json(401, {})
            return
        if self.path == manage.MANAGE_PATH:
            if f"TOKEN={self.cookie}" not in self.headers.get("Cookie", ""):
                self._json(401, {})
                return
            FakeCamera.received = body
            self._json(200, {"status": "ok"})
            return
        self._json(404, {})


def a_camera(tmp_path: Path) -> tuple[ThreadingHTTPServer, int]:
    cert = certs.ensure(tmp_path / "cam.pem")
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCamera)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# ------------------------------------------------------------------- the payload


def test_manage_payload_points_the_camera_at_us() -> None:
    payload = manage.manage_payload("10.0.0.1", token="anything", port=7442)
    assert payload["mgmt"]["hosts"] == ["10.0.0.1:7442"]
    assert payload["mgmt"]["token"] == "anything"
    assert payload["mgmt"]["protocol"] == "wss"


def test_the_token_is_ours_to_choose() -> None:
    """It becomes the adoptionCode the camera quotes back; cuckoo accepts its own."""
    assert manage.manage_payload("h", "cuckoo-42")["mgmt"]["token"] == "cuckoo-42"


# ------------------------------------------------------------- against a fake camera


def test_login_then_manage_hands_over_the_address(tmp_path: Path) -> None:
    server, port = a_camera(tmp_path)
    FakeCamera.received = {}
    try:
        result = manage.take_custody(
            camera_host="127.0.0.1", username="admin", password="secret",
            controller_host="10.0.0.9", token="tok-123",
            control_port=7442, camera_port=port,
        )
        assert result == {"status": "ok"}
        assert FakeCamera.received["mgmt"]["hosts"] == ["10.0.0.9:7442"]  # type: ignore[index]
        assert FakeCamera.received["mgmt"]["token"] == "tok-123"  # type: ignore[index]
    finally:
        server.shutdown()


def test_a_wrong_credential_is_a_clear_error(tmp_path: Path) -> None:
    server, port = a_camera(tmp_path)
    try:
        try:
            manage.take_custody(
                camera_host="127.0.0.1", username="admin", password="wrong",
                controller_host="10.0.0.9", token="t", camera_port=port,
            )
            raise AssertionError("a bad password should not be accepted")
        except manage.ManageError as exc:
            assert "401" in str(exc) and "factory-fresh" in str(exc)
    finally:
        server.shutdown()


def test_manage_without_a_login_cookie_is_refused(tmp_path: Path) -> None:
    """Proves the fake enforces the cookie, so the success test means something."""
    server, port = a_camera(tmp_path)
    try:
        camera = manage.Camera("127.0.0.1", "admin", "secret", port=port)
        opener = manage._opener()  # no login call
        try:
            camera.manage(opener, "10.0.0.9", "t")
            raise AssertionError("manage without a session should be refused")
        except manage.ManageError as exc:
            assert "401" in str(exc)
    finally:
        server.shutdown()


def test_an_unreachable_camera_is_reported(tmp_path: Path) -> None:
    camera = manage.Camera("127.0.0.1", "admin", "secret", port=1)
    try:
        camera.login(manage._opener())
        raise AssertionError("a closed port should not log in")
    except manage.ManageError as exc:
        assert "could not reach" in str(exc)
