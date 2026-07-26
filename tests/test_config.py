"""The JSON config loader, the defaults, and how CLI flags override a file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import config
import controller
import discovery
import main
import media
import onvif
import rtsp
import snapshots
from model import Codec

# ------------------------------------------------------------------ the loader


def test_defaults_answer_everything() -> None:
    merged = config.merged({})
    assert merged["name"] == "cuckoo"
    assert merged["announce"] is True
    assert merged["ports"]["onvif"] == 8000


def test_the_default_codec_is_h264_on_every_channel() -> None:
    assert config.DEFAULTS["tracks"] == {"video1": "h264", "video2": "h264", "video3": "h264"}


def test_default_ports_match_the_module_constants() -> None:
    """So the config baseline never drifts from what the servers actually bind."""
    ports = config.DEFAULTS["ports"]
    assert ports["control"] == controller.CONTROL_PORT
    assert ports["ingest"] == media.INGEST_PORT
    assert ports["snapshot"] == snapshots.SNAPSHOT_PORT
    assert ports["rtsp"] == rtsp.RTSP_PORT
    assert ports["onvif"] == onvif.ONVIF_PORT
    assert ports["discovery"] == discovery.WS_DISCOVERY_PORT


def test_a_file_overrides_one_port_and_leaves_the_rest() -> None:
    merged = config.merged({"ports": {"onvif": 9000}})
    assert merged["ports"]["onvif"] == 9000
    assert merged["ports"]["rtsp"] == 8554


def test_tracks_merge_per_channel() -> None:
    """A file naming one channel re-codecs only it — the others keep the default."""
    merged = config.merged({"tracks": {"video2": "h265"}})
    assert merged["tracks"] == {"video1": "h264", "video2": "h265", "video3": "h264"}


def test_deep_merge_replaces_scalars_and_lists_but_merges_dicts() -> None:
    out = config.deep_merge({"a": [1, 2], "b": {"x": 1}}, {"a": [3], "b": {"y": 2}})
    assert out == {"a": [3], "b": {"x": 1, "y": 2}}


def test_load_dict_rejects_a_non_object() -> None:
    with pytest.raises(ValueError):
        config.load_dict(b"[1, 2, 3]")


def test_a_missing_file_is_an_empty_config() -> None:
    assert config.load("/no/such/path/cuckoo.json") == {}


# ------------------------------------------------------ CLI over file precedence


def _args(**over: object) -> argparse.Namespace:
    base: dict[str, object] = dict(
        config=None, host=None, control_port=None, ingest_port=None, snapshot_port=None,
        rtsp_port=None, onvif_port=None, discovery_port=None, cert=None, tracks=None,
        name=None, no_announce=False, dump=None, verbose=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_cli_flags_beat_the_file(tmp_path: Path) -> None:
    cfg = tmp_path / "cuckoo.json"
    cfg.write_text(json.dumps({"host": "1.1.1.1", "name": "fromfile", "tracks": {"video1": "h265"}}))
    opts = main.resolve_options(
        _args(config=str(cfg), name="fromcli", tracks="video1:h264,video2:h264")
    )
    assert opts.host == "1.1.1.1"  # only in the file — shows through
    assert opts.name == "fromcli"  # flag wins over the file
    assert opts.tracks == ("video1", "video2")  # --tracks replaces the file's set
    assert opts.track_codecs == {"video1": Codec.H264, "video2": Codec.H264}


def test_the_file_supplies_host_and_per_channel_codec(tmp_path: Path) -> None:
    cfg = tmp_path / "cuckoo.json"
    cfg.write_text(json.dumps({"host": "2.2.2.2", "tracks": {"video2": "h265"}}))
    opts = main.resolve_options(_args(config=str(cfg)))
    assert opts.host == "2.2.2.2"
    assert opts.track_codecs == {
        "video1": Codec.H264, "video2": Codec.H265, "video3": Codec.H264
    }


def test_no_host_anywhere_is_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "cuckoo.json"
    empty.write_text("{}")
    with pytest.raises(SystemExit):
        main.resolve_options(_args(config=str(empty)))


def test_a_named_config_that_is_missing_is_an_error() -> None:
    with pytest.raises(SystemExit):
        main.resolve_options(_args(config="/no/such/file.json", host="1.2.3.4"))


def test_a_bare_track_name_defaults_to_h264() -> None:
    assert main.spec_to_tracks("video1,video2:h265") == {"video1": "h264", "video2": "h265"}
