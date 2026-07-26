# cuckoo — a UniFi controller that isn't there

**cuckoo presents a UniFi G5 PTZ as a standard ONVIF camera, by being its
controller.** The camera speaks a private protocol to a UniFi console and nothing
else. cuckoo takes the console's place: it accepts the camera's connection, drives
the adoption and settings the camera expects, receives the video it pushes, and
moves the head the way the real controller does — then re-serves all of it as
ordinary **RTSP, ONVIF and WS-Discovery**. Point Home Assistant, VLC or `ffplay`
at it.

Everything camera-facing was measured off a real controller and a real camera, not
invented. No SSH, no shelling into the camera, no patched firmware.

> It has a mirror image: [`finch`](https://github.com/rjmotion/finch), a camera
> that isn't there. Run the two against each other and the whole protocol is
> exercised with no UniFi hardware at all.

## Verified against real hardware

cuckoo has driven a physical UVC G5 PTZ end to end: adoption, the settings suite,
PTZ, and **all three encoder channels re-served as RTSP at once** — `ffprobe` reads
**`h264 / Main`** at `2688×1512`, `1280×720` and `640×360`, all decoding cleanly
with AAC, from the one camera. (The camera runs the 4MP H.264 at ~8 Mbps of its own
accord, and mixed codecs work too — pin a channel to `h265` and it streams
alongside the H.264 ones.) Its ONVIF face was checked by **Home Assistant's own
client**
(`python-onvif-zeep`), which walked the full onboarding — profiles, stream URI,
snapshot, the complete PTZ move-and-preset set — against both the real camera and
finch. See [`harness/`](harness).

## What it does

```
   camera                          cuckoo                        clients
  ────────                        ────────                      ─────────
  dials :7442  ──── control ────▶  adoption, settings, PTZ
  pushes video ──── :7550   ────▶  deframe, republish  ────────▶ RTSP :8554
  (H.264+H.265)                    (two tracks)         ────────▶ (both ONVIF profiles)
  POSTs JPEG   ──── :7444   ────▶  snapshot store      ────────▶ HTTP snapshot
  Event*       ──── control ────▶  detections          ────────▶ ONVIF events
                                   device model        ────────▶ ONVIF :8000
                                                       ────────▶ WS-Discovery :3702
```

| Concern | Module | State |
|---|---|---|
| Device model — the seam both faces meet at | `model.py` | done |
| Adoption + settings suite | `adoption.py` | done |
| PTZ encoding | `ptz.py` | done |
| Detection events | `events.py` | done |
| Control-channel event loop | `controller.py` | done |
| Media ingest `:7550` | `media.py` | done |
| Snapshot upload server `:7444` | `snapshots.py` | done |
| RTSP out `:8554` | `rtsp.py` | done |
| ONVIF services `:8000` | `onvif.py` | done |
| WS-Discovery `:3702` | `discovery.py` | done |
| Taking custody of a camera (`/api/1.2/manage`, resident-controller API) | `manage.py`, `custody.py` | done |
| Assembly | `main.py` | done |
| Talkback, MJPEG `:7551`, Imaging writes, ONVIF auth | — | to do |

The wire itself — the message envelope, WebSocket framing, the `extendedFlv`
container, the HEVC/AAC bitstream — lives in
[`pyunifiwire`](https://github.com/rjmotion/pyunifiwire), shared with finch.

## Requirements

- Python **3.13**. cuckoo itself is standard-library only.
- [`pyunifiwire`](https://github.com/rjmotion/pyunifiwire) — the shared wire.
  Not yet on PyPI:

  ```sh
  pip install git+https://github.com/rjmotion/pyunifiwire
  # or, working on both at once, put it on the path instead:
  git clone https://github.com/rjmotion/pyunifiwire ../pyunifiwire   # run.sh/test.sh look there
  ```
- `ffmpeg`/`ffprobe` for the media checks.

## Run it

```sh
./test.sh                            # mypy --strict, then pytest. No camera, no network beyond loopback.
./run.sh --host 192.168.1.10         # --host must be routable from the camera and clients — not loopback
```

`--host` is written into the stream destinations, the snapshot upload URL, the PTZ
callback and every ONVIF address, so it cannot be a loopback. A self-signed
certificate is generated on first run (`cuckoo.pem`, mode 600, gitignored). Every
port can be moved (`--ingest-port`, `--snapshot-port`, `--rtsp-port`,
`--onvif-port`).

## Configuration — file or flags

Everything can come from a JSON file, a flag, or both. cuckoo reads `cuckoo.json`
(or `--config PATH`) as the baseline; **a flag always overrides the file**, and a
missing file just means defaults + flags. So the same run is expressible either way:

```jsonc
// cuckoo.json
{
  "host": "192.168.1.10",
  "tracks": { "video1": "h264", "video2": "h265", "video3": "h264" },
  "ports": { "onvif": 8000, "rtsp": 8554 }
}
```

```sh
./run.sh --config cuckoo.json                       # all from the file
./run.sh --config cuckoo.json --tracks video1:h265  # …but this run's main is H.265
```

`tracks` is `{channel: codec}` across the camera's three encoder channels
(2688×1512, 1280×720, 640×360). **The default codec is H.264** — the one an ONVIF
client (Home Assistant) needs — so a bare `--tracks video1,video2` or an empty
config still yields H.264; set any channel to `h265` for an efficient stream. A file
that names one channel re-codecs only it. The old cuckoo's SSH/credential fields are
gone (this cuckoo never touches the camera over SSH); see `config.py` for the full
field map.

### Changing a codec at runtime

A codec can also be flipped live over ONVIF `SetVideoEncoderConfiguration` — set a
profile's `Encoding` to `H264` or `H265` and cuckoo re-arms that channel on the
camera through the same settings path, no restart. (Home Assistant never calls this
— it consumes the profiles you advertise — but a VMS or ONVIF Device Manager can.)

## Testing without a camera

`./test.sh` runs the lot — nothing needs hardware.

- **Per layer** — axis arithmetic, the settings suite, PTZ encoding, RTP
  fragmentation, SOAP parsing, discovery probe filtering.
- **Adoption** (`test_adoption_flow.py`) drives the real `Controller` against a
  fake camera exposing only `sendall`, so the sequencing rules are tested rather
  than described: the timeSync ping-pong that must be answered every time, the
  settings released one at a time, a PTZ channel requested only for a camera that
  announced motor bounds.
- **Whole stack** (`test_stack.py`) builds every server on ephemeral ports, has a
  fake camera connect over real TLS and adopt, then checks the ONVIF face reflects
  it: profiles, stream URI, a PTZ move arriving as `Preset` config+go clamped to
  the camera's limits, a detection reaching a pull-point subscriber, a snapshot
  round-tripping through the upload endpoint.

### Playing real video through it

`stubcam.py` pushes a real HEVC **or H.264** file at the ingest port the way a
camera would:

```sh
ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=15 -t 4 -c:v libx265 \
       -x265-params keyint=15 -f hevc /tmp/t.h265
./run.sh --host 127.0.0.1 &
python3 stubcam.py /tmp/t.h265 --port 7550 --name video1 --loop
ffprobe -rtsp_transport tcp -i rtsp://127.0.0.1:8554/video1     # -> hevc 1280x720

# The H.264 profile HA needs, exercised the same way with no camera:
ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=15 -t 4 -c:v libx264 \
       -x264-params keyint=15 -bsf:v h264_mp4toannexb -f h264 /tmp/t.h264
python3 stubcam.py /tmp/t.h264 --codec h264 --port 7550 --name video2 --loop
ffprobe -rtsp_transport tcp -i rtsp://127.0.0.1:8554/video2     # -> h264  1280x720
```

### A real ONVIF client — the acceptance test

[`harness/`](harness) drives a running cuckoo with `python-onvif-zeep`, the same
`ONVIFCamera` stack Home Assistant uses, validated against the ONVIF WSDL. That
catches things hand-formed SOAP cannot. Run it against a live pair or a real
camera:

```sh
python3 harness/ha_walk.py  <host> <onvif-port> [--camera]   # HA's onboarding walk
python3 harness/ptz_walk.py <host> <onvif-port>              # every move kind + presets
```

## Testing with the real camera

An adopted camera reconnects to whoever holds `:7442`, but a *stolen* socket alone
is refused — the camera has to be *told* to adopt a new controller, and only its
current controller can do that. `handoff.sh` orchestrates the whole dance:

```sh
./handoff.sh status        # who holds each port right now
./handoff.sh take          # release from the resident controller, bind :7442, point the camera at us
./handoff.sh give-back     # stop cuckoo, hand the camera back
```

`take` asks the resident controller to release the camera, frees `:7442`, and then
POSTs `/api/1.2/manage` to the camera pointing it here — at which point it dials
in, says hello, and adoption proceeds. Credentials come from a directory named by
`CUCKOO_SECRETS` (defaults to `~/.cuckoo/secrets`), which is never committed. See
the header of `handoff.sh` for the full set of `CUCKOO_*` overrides.

## Things worth knowing (all measured)

- **The video is HEVC under FLV codec id 8**, which no stock demuxer maps, in a
  container with 20 bytes between tags where FLV has 4. The real camera's sequence
  header is not a standard `hvcC` either — it length-prefixes the parameter sets
  and signals config by the FLV frame type, not the packet-type byte.
- **Home Assistant's ONVIF integration requires an H.264 profile** and rejects an
  H.265-only device with *"no H.264 streams available"* — and it does **not**
  reconfigure the encoder to get one (it consumes the profiles you advertise). The
  camera has **three hardware encoder channels** (2688×1512, 1280×720, 640×360) and
  encodes `h264`/`h265`/`mjpg` natively. `--tracks` sets the codec per channel; the
  default `video1:h264,video2:h265,video3:h264` offers **both codecs** — full-res
  H.264 and a low H.264 so an ONVIF client always finds one, plus an H.265 substream
  for players that prefer it. Nothing is fixed to one codec; set any channel to
  either (`--tracks video1:h265,video2:h264,…`). The codec is armed once at
  adoption, not per client request. No transcode:
  the H.264 is re-packetised straight through as RFC 6184 RTP the way the HEVC is as
  RFC 7798. The codec of each stream is read from the wire, not assumed — including
  the trap that the two codecs signal their config *differently*: HEVC by FLV frame
  type 6 (packet-type byte 1 even on config), H.264 the standard way (frame type 1,
  `AVCPacketType` 0, a normal `avcC`). Both are handled; assuming they match loses
  one codec's parameter sets silently. Verified against the real camera streaming
  all three channels as H.264 at once (2688×1512, 1280×720, 640×360), and mixed
  codecs (an H.265 channel alongside H.264 ones) too.
- **The audio is AAC-LC at 16 kHz mono**, so the SDP states a 16000 clock rate; a
  receiver assuming 44.1 kHz plays it at the wrong speed.
- **timeSync is a ping-pong.** The camera pings it repeatedly, each message
  referencing the controller's last, until its clock converges — only then does it
  send hello. Answer every timeSync, reply flag or not, and never speak first.
- **There is no absolute-move verb.** A move is *configure a preset, then go to
  it*, and presets carry focus, so the model is four axes.
- **ONVIF `ContinuousMove` becomes one relative step**, because the camera has no
  continuous mode; a client holding an arrow key sends these repeatedly.
- **`RemovePreset` forgets the slot locally.** No wire delete has been observed, so
  a preset cuckoo wrote persists on the camera even though the client sees it gone.
- **ONVIF `Capabilities` order is load-bearing.** It is a strict schema sequence
  and a real client drops anything out of order — get it wrong and Home Assistant
  silently adds the camera with no event sensors.
- **WS-Discovery must use WS-Addressing 2004/08, not 2005/08** — a must-have for
  autodiscovery. ONVIF pairs the 2005/04 discovery draft with the *2004/08*
  addressing namespace; a device that uses 2005/08 finds no `MessageID` in the
  probe, replies to nothing, and never appears in Home Assistant's discovery. The
  failure is silent and total, and invisible to a self-written probe (which
  round-trips against its own wrong namespace) — only a real client catches it.
  See the `discovery.py` docstring and `tests/test_discovery.py`.
- **No ONVIF authentication yet.** Requests are served unauthenticated. Don't
  expose the ONVIF port to an untrusted network.

The full protocol write-up, with captures, is in the
[flock guides](https://github.com/rjmotion/unifi-guides).

## Licence

MIT — see [`LICENSE`](LICENSE).

Not affiliated with or endorsed by Ubiquiti Inc. UniFi and UniFi Protect are their
trademarks. No Ubiquiti firmware or binaries are included here.
