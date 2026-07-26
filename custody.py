"""Moving a camera between controllers, deliberately.

A camera belongs to one controller at a time and knows which. Taking the socket
away from the resident controller is not enough: a camera that already believes it
is adopted says nothing but `timeSync`, ignores a hello it did not ask for, and
answers settings with `{"desc": "Unauthorized"}`. It only introduces itself — and
only then accepts orders — when it thinks nobody owns it.

So a clean handover is two steps, and this is the first: ask the resident
controller to let go, and afterwards ask it to take the camera back. Both are
ordinary operations that controller already supports; nothing here touches the
camera itself.

    python3 custody.py release --host <controller> --user <u> --password <p>
    python3 custody.py restore --host <controller> --user <u> --password <p>
    python3 custody.py status  --host <controller> --user <u> --password <p>

Without `--mac` it acts on every adopted camera, which on a lab controller is
usually the one you mean. Pass `--mac` when it is not.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any, Final

DEFAULT_PORT: Final = 443
SETTLE_SEC: Final = 3.0

log = logging.getLogger("cuckoo.custody")


def _client(host: str, user: str, password: str, port: int) -> Any:
    """Imported late so the rest of cuckoo never needs this dependency."""
    try:
        from uiprotect import ProtectApiClient
    except ImportError:  # pragma: no cover - only hit without the client installed
        raise SystemExit(
            "custody needs the uiprotect client: pip install uiprotect"
        ) from None
    return ProtectApiClient(host, port, user, password, verify_ssl=False)


def _camera_model() -> Any:
    """The client wants its own enum here, not the string "camera"."""
    from uiprotect.data import ModelType

    return ModelType.CAMERA


def _matches_raw(camera: dict[str, Any], mac: str) -> bool:
    return not mac or str(camera.get("mac", "")).upper() == mac.replace(":", "").upper()


async def _act(action: str, host: str, user: str, password: str, mac: str, port: int) -> int:
    client = _client(host, user, password, port)
    await client.update()
    try:
        # Read the raw bootstrap rather than the typed collection: a released
        # camera is still listed there, with isAdopted false, but drops out of the
        # client's `cameras` mapping — which is exactly the camera we care about.
        bootstrap = await client.api_request_obj("bootstrap")
        cameras = [c for c in bootstrap.get("cameras", []) if _matches_raw(c, mac)]
        if not cameras:
            log.error("no camera on %s matches %s", host, mac or "(any)")
            return 1

        for camera in cameras:
            adopted = bool(camera.get("isAdopted"))
            identifier = str(camera.get("id"))
            name = camera.get("name")
            if action == "status":
                print(f"  {camera.get('mac')}  {name!r}  adopted={adopted}  "
                      f"state={camera.get('state')}")
                continue

            if action == "release":
                if not adopted:
                    log.info("%s is already released", camera.get("mac"))
                    continue
                log.info("asking the controller to release %s (%s)", camera.get("mac"), name)
                await client.unadopt_device(_camera_model(), identifier)
                log.info("released — it now waits to be told where to go")

            elif action == "restore":
                if adopted:
                    log.info("%s is already adopted here", camera.get("mac"))
                    continue
                log.info("asking the controller to take %s back", camera.get("mac"))
                await client.adopt_device(_camera_model(), identifier)
                log.info("adopted — the camera belongs to the resident controller again")
        await asyncio.sleep(SETTLE_SEC)
        return 0
    finally:
        await client.close_session()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move a camera between controllers.")
    parser.add_argument("action", choices=["release", "restore", "status"])
    parser.add_argument("--host", required=True, help="the resident controller")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--mac", default="", help="limit to one camera")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="  %(message)s", stream=sys.stderr)
    return asyncio.run(_act(args.action, args.host, args.user, args.password, args.mac, args.port))


if __name__ == "__main__":
    raise SystemExit(main())
