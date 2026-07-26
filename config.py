"""cuckoo runtime configuration — one JSON file, merged under the CLI.

A single JSON file (``cuckoo.json`` by default, or ``--config PATH``) supplies the
baseline; command-line flags are per-run overrides and **always win**. A missing
file is not an error — cuckoo then runs on defaults + CLI exactly as before this
module existed.

Pure and testable: parsing is separate from the filesystem (`load_dict` takes
bytes), and nothing here touches the network or global state.

What became of the old cuckoo's config fields
----------------------------------------------
The old daemon read a richer ``cuckoo.json`` because it drove the camera over SSH
and toggled many device features. This cuckoo is narrower — it is *only* the
controller-and-ONVIF seam — so the surface is smaller and some fields are gone by
design:

* ``camera.ssh_user`` / ``ssh_password`` / ``ip`` — **removed.** This cuckoo never
  SSHes into the camera; the camera dials *us*. Custody hand-off credentials live
  outside the repo (see ``handoff.sh`` and ``CUCKOO_SECRETS``), not here.
* ``rtsp.profiles`` (``main``/``medium``/``low``) — became **``tracks``**, the three
  encoder channels ``video1``/``video2``/``video3``, each now naming its **codec**.
* ``ports`` — kept, same idea (``control``/``ingest``/``snapshot``/``rtsp``/
  ``onvif``/``discovery``).
* ``controller.uuid`` — no longer needed: this cuckoo adopts with a null
  ``controllerUuid`` and ``overrideUuid: true`` rather than persisting one.
* ``ptz`` / ``events`` / ``audio`` / ``imaging`` toggles — not yet surfaced here;
  PTZ and events are derived from what the camera announces. They are candidates to
  add back as fields when there is a reason to turn them off.

The one genuinely new idea is **per-channel codec**, because that is what a real
ONVIF client cares about (Home Assistant needs an H.264 profile). The default is
H.264 on every channel; set any to ``h265`` for an efficient stream.
"""

from __future__ import annotations

import json
from typing import Any, Final

DEFAULT_CONFIG_PATH: Final = "cuckoo.json"

# Every value cuckoo reads has a default here, so a config built from {} answers
# everything. Ports mirror the module constants (asserted by the tests).
DEFAULTS: Final[dict[str, Any]] = {
    "host": None,  # the address the camera and clients reach us on; required
    "name": "cuckoo",  # controller identity shown to the camera and in discovery
    "cert": "cuckoo.pem",
    "announce": True,  # multicast WS-Discovery Hello, or only answer probes
    # channel -> codec. Default H.264 everywhere so an ONVIF client (Home
    # Assistant) always finds a profile it can decode; set any to "h265".
    "tracks": {"video1": "h264", "video2": "h264", "video3": "h264"},
    "ports": {
        "control": 7442,
        "ingest": 7550,
        "snapshot": 7444,
        "rtsp": 8554,
        "onvif": 8000,
        "discovery": 3702,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay ``override`` onto a copy of ``base``.

    Dicts merge key-by-key; every other value (including lists) replaces wholesale.
    So a file that sets ``ports.onvif`` leaves the other ports alone, and a file
    that sets ``tracks.video2`` re-codecs just that channel.
    """
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_dict(raw: bytes) -> dict[str, Any]:
    """Parse config bytes into a dict. Raises on non-object JSON."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def load(path: str) -> dict[str, Any]:
    """Read and parse a config file; a missing file is an empty config."""
    try:
        with open(path, "rb") as handle:
            return load_dict(handle.read())
    except FileNotFoundError:
        return {}


def merged(file_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The defaults with a parsed config file overlaid."""
    return deep_merge(DEFAULTS, file_config or {})
