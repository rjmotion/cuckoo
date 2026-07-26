# harness — black-box tests through a real ONVIF client

The suites under `../tests/` drive cuckoo with hand-formed SOAP: they prove cuckoo
answers what cuckoo expects. This directory is the other kind of test — a **real
third-party ONVIF client** (`python-onvif-zeep`, the same `ONVIFCamera` stack Home
Assistant uses) talking to a running cuckoo as a black box, validating every
response against the official ONVIF WSDL.

That validation catches things hand-formed SOAP cannot. It found, for instance,
that cuckoo emitted the `GetCapabilities` elements out of the schema's required
order — which zeep silently drops, so Home Assistant would never have created
motion sensors. The unit tests were all green; the real client was not.

## `ha_walk.py` — Home Assistant's onboarding walk

Every call HA's onvif integration makes, in order: clock, capabilities, services,
identity, profiles, stream URI (then it *plays* the exact URI returned), snapshot
URI, PTZ enumerate + move, events pull-point.

```sh
pip install --user onvif-zeep          # needs the ONVIF WSDLs on its wsdl path
python3 harness/ha_walk.py <host> <onvif-port> [--camera]
```

`--camera` adds the checks that need a live stream and a movable head (playing the
advertised RTSP URI, and asserting AbsoluteMove changes the reported position).

Verified 14/14 against **finch** (the synthetic camera) and against a **real UVC
G5 PTZ** — in the latter, AbsoluteMove moved the physical head and the advertised
URI played `hevc 2688x1512` + AAC.

## `ptz_walk.py` — the full PTZ surface

What `ha_walk.py` only touches (a node exists, one AbsoluteMove lands) this drives
completely, through zeep: **all three move kinds** — Absolute, Relative,
Continuous+Stop — and the **preset lifecycle** (Set, Get, Goto, Remove), which is
what "saved viewpoints" are in ONVIF terms.

```sh
python3 harness/ptz_walk.py <host> <onvif-port>
```

11/11 against **finch** and against the **real G5 PTZ**: GotoPreset recalled the
saved position to within rounding on the physical camera.

Measured, not assumed: the profiles cuckoo advertises are **H.265** (matching the
real camera), and zeep — Home Assistant's own client — parsed them, read the
embedded `PTZConfiguration`, and drove every PTZ operation over them. So on the
evidence, HA's client needs no H.264 profile to negotiate PTZ; the H.265 profile
carrying a PTZConfiguration is sufficient.

**One real limitation it exposes:** `RemovePreset` drops the preset from cuckoo's
view (which is what an ONVIF client sees) but has no measured wire-delete, so a
preset cuckoo wrote to the `ptz1` channel persists on the camera. Creating and
recalling saved viewpoints works; truly deleting one from the device does not.

**Not covered by either harness:** ONVIF PTZ *auxiliary commands*
(`SendAuxiliaryCommand` — wiper/IR/etc.) and *patrol / preset tours / auto-track*.
cuckoo does not advertise or implement those over ONVIF; the camera's own
auto-track lives on the `ptz1` channel (see the guides) but is not surfaced north.

Not wired into `test.sh`: these need a running daemon, the WSDLs, and (for the
full run) a camera. They are acceptance tests you run against a live pair.
