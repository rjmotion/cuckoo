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
PTZ, and **H.265 video re-served as RTSP** (`ffprobe` reads `hevc / Main /
2688×1512 / 30 fps` + AAC, and a full frame decodes). Its ONVIF face was checked
by **Home Assistant's own client** (`python-onvif-zeep`), which walked the full
onboarding — profiles, stream URI, snapshot, the complete PTZ move-and-preset set —
against both the real camera and finch. See [`harness/`](harness).

## What it does

```
   camera                          cuckoo                        clients
  ────────                        ────────                      ─────────
  dials :7442  ──── control ────▶  adoption, settings, PTZ
  pushes H.265 ──── :7550   ────▶  deframe, republish  ────────▶ RTSP :8554
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

`stubcam.py` pushes a real HEVC file at the ingest port the way a camera would:

```sh
ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=15 -t 4 -c:v libx265 \
       -x265-params keyint=15 -f hevc /tmp/t.h265
./run.sh --host 127.0.0.1 &
python3 stubcam.py /tmp/t.h265 --port 7550 --name video1 --loop
ffprobe -rtsp_transport tcp -i rtsp://127.0.0.1:8554/video1     # -> hevc 1280x720
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
- **No ONVIF authentication yet.** Requests are served unauthenticated. Don't
  expose the ONVIF port to an untrusted network.

The full protocol write-up, with captures, is in the
[flock guides](https://github.com/rjmotion/unifi-guides).

## Licence

MIT — see [`LICENSE`](LICENSE).

Not affiliated with or endorsed by Ubiquiti Inc. UniFi and UniFi Protect are their
trademarks. No Ubiquiti firmware or binaries are included here.
