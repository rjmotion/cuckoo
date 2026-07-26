# cuckoo — architecture

cuckoo is a UniFi controller on one side and a standard ONVIF camera on the other.
The whole design is organised around keeping those two faces from knowing about
each other.

```
  ┌─ north: ONVIF ────────────────────────────────────────────┐
  │  Device · Media · PTZ · Events · Imaging   (SOAP/HTTP)     │
  │  WS-Discovery (UDP 3702)   ·   RTSP out (TCP 8554)         │
  └───────────────────────────┬───────────────────────────────┘
                              │  device model  (the only shared vocabulary)
  ┌───────────────────────────┴───────────────────────────────┐
  │  core: camera state, profiles, presets, event bus         │
  └───────────────────────────┬───────────────────────────────┘
                              │  controller API
  ┌───────────────────────────┴───────────────────────────────┐
  │  south: the controller                                    │
  │   · adoption + management  (TLS WS :7442)                 │
  │   · ptz1 channel           (second WS, same path)         │
  │   · media ingest           (TCP :7550, H.265 + AAC)       │
  │   · snapshot upload        (TCP :7444, camera POSTs to us)│
  │   · discovery / custody    (UDP :10001, HTTPS :443)       │
  └───────────────────────────────────────────────────────────┘
```

**The device model (`model.py`) is the seam.** North knows nothing of the camera's
`functionName`s; south knows nothing of SOAP. Everything that crosses between them
goes through a typed model — an adopted `Camera` with its axis ranges, tracks,
presets and motion state. That is what makes each layer testable without the one
below it: the ONVIF face can be driven against a hand-built model, and the
controller can be driven against a fake socket, with no camera and no client in
sight.

Every protocol fact below was measured against a real UniFi Protect controller and
a real UVC G5 PTZ. The full write-up, with captures, is in the
[flock guides](https://github.com/rjmotion/unifi-guides); this file states only
what shapes the code.

## South — being the controller

**Adoption.** The camera dials `:7442` over TLS (it does not validate the
controller's certificate) and upgrades to a WebSocket with subprotocol
`secure_transfer`. It then pings `timeSync` — and keeps pinging, each message
referencing the controller's last, until its clock converges; **only then** does
it send its `hello`. The controller must answer *every* timeSync (a reply flag is
not a reason to ignore one) and must **not** speak first. Once the camera's hello
arrives, replying to it is the gate; the reply carries a null `controllerUuid`
with `overrideUuid: true`. The settings suite that follows is **ack-gated** —
message N+1 is released only when N is acknowledged.

**PTZ** rides a *second* WebSocket. The controller sends `EnablePtzControl` with
its own URL; the camera dials back on the same path negotiating subprotocol
`ptz1`. There is no absolute-move verb — a move is *configure a preset, then go to
it* (`Preset` with `action:config` then `action:go`), and presets carry focus, so
the model is four axes. Position is both polled (`GetCurrentPosition`) and pushed
(`EventMotorState`).

**Media** is pushed, not pulled. Arming a track with a `destinations` address makes
the camera dial it and write `extendedFlv` — FLV with 20 bytes between tags instead
of 4, HEVC under codec id 8 (or H.264 under id 7), AAC-LC 16 kHz. The sequence
header is not a standard `hvcC`/`avcC`; the parameter sets are length-prefixed and
config is signalled by the FLV frame type. Each track carries the codec it was
armed for — the camera encodes `h264`/`h265`/`mjpg` natively — so cuckoo arms one
H.265 track and one H.264 track, because Home Assistant's ONVIF integration requires
an H.264 profile. `flv`/`hevc`/`avc`/`annexb` in
[`pyunifiwire`](https://github.com/rjmotion/pyunifiwire) handle all of this.

**Snapshots** are pushed too: the controller sends `GetRequest` with a one-time
`:7444` upload URL and the camera POSTs the JPEG back.

**Custody.** A camera that already belongs to a controller reconnects to whoever
holds `:7442` but refuses commands — taking the socket is not taking the camera.
It has to be *told* to adopt a new controller: `custody.py` asks the resident
controller to release it, and `manage.py` POSTs `/api/1.2/manage` to point it here.

## North — the ONVIF camera

The adopted `Camera` model is rendered as ONVIF: Device, Media, PTZ, Events and
Imaging over SOAP; WS-Discovery so clients find it; and an RTSP server that
re-packetises the pushed H.265 or H.264 (RFC 7798 / RFC 6184) and AAC as standard
RTP — one media profile per armed track, so a client picks the codec it wants. PTZ
maps the ONVIF move and
preset operations onto the `ptz1` channel — normalised −1..1 coordinates in, motor
units out, clamped to the camera's own announced ranges.

Two things a schema-validating client (Home Assistant uses `python-onvif-zeep`)
demands that hand-formed SOAP does not: XML element order matters (the ONVIF
`Capabilities` and profile structures are strict sequences, and out-of-order
elements are silently dropped), and a media profile must embed a
`PTZConfiguration` for PTZ to be offered at all.

## Testing

Each layer is tested without the one below it, and the assembled whole is tested
against a fake peer:

- **layer** — model arithmetic, the settings suite, PTZ encoding, RTP
  fragmentation, SOAP parsing, discovery filtering. No I/O.
- **adoption** — the real `Controller` against a fake socket, so the sequencing
  rules (the timeSync ping-pong, ack-gating, the PTZ-channel request) are executed
  rather than described.
- **stack** — every server on ephemeral ports, a fake camera adopting over real
  TLS, then the ONVIF face checked against it.
- **acceptance** ([`harness/`](harness)) — a real ONVIF client (`python-onvif-zeep`,
  the Home Assistant stack) walking the onboarding and the full PTZ surface against
  a live cuckoo, verified against both finch and a real G5 PTZ.

## Not built

Talkback (AAC out over UDP `:7004`), MJPEG `:7551`, applying Imaging settings
(they are reported, not yet written), ONVIF authentication, and a wire-level
preset delete (`RemovePreset` currently forgets the slot locally only).
