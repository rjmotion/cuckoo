"""Home Assistant's ONVIF onboarding walk, via the real client it uses (onvif-zeep).

Every call is one HA's onvif integration actually makes, in order, through zeep —
which validates each response against the official ONVIF WSDL. This is the client
that is strict about schema order — a real client silently drops elements that
# appear out of the ONVIF sequence, so this catches ordering bugs the unit tests
# cannot;
if cuckoo's SOAP is wrong in a way hand-written tests miss, zeep fails here.
"""

from __future__ import annotations

import subprocess
import sys

from onvif import ONVIFCamera

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18000
HAVE_CAMERA = "--camera" in sys.argv

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


# The constructor itself performs GetCapabilities under the hood — a real exchange.
cam = ONVIFCamera(HOST, PORT, "", "")

# 2. clock
dt = cam.devicemgmt.GetSystemDateAndTime()
check("GetSystemDateAndTime (HA computes clock drift)", dt is not None,
      f"type={dt.DateTimeType}")

# 3. capability — HA requires Media, wires Events if present
caps = cam.devicemgmt.GetCapabilities()
check("GetCapabilities advertises Media", caps.Media is not None)
check("GetCapabilities advertises Events", getattr(caps, "Events", None) is not None)

services = cam.devicemgmt.GetServices(False)
namespaces = [s.Namespace for s in services]
check("GetServices lists device+media+ptz+events",
      all(any(k in n for n in namespaces) for k in ("device", "media", "ptz", "events")))

# 4. identity — HA's unique id is the MAC; the info call must succeed
info = cam.devicemgmt.GetDeviceInformation()
check("GetDeviceInformation", bool(info.Model),
      f"model={info.Model} fw={info.FirmwareVersion} serial={info.SerialNumber}")

# 5. media — GetProfiles then GetStreamUri, validated by zeep against the WSDL
media = cam.create_media_service()
profiles = media.GetProfiles()
check("GetProfiles parses under zeep/WSDL validation", len(profiles) >= 1,
      f"{len(profiles)} profile(s)")
token = profiles[0].token if profiles else None
has_ptz_cfg = any(getattr(p, "PTZConfiguration", None) is not None for p in profiles)
check("a profile carries a PTZConfiguration (or HA hides PTZ)", has_ptz_cfg)
enc = profiles[0].VideoEncoderConfiguration if profiles else None
check("profile advertises an encoding", enc is not None,
      f"encoding={getattr(enc,'Encoding',None)}")

if token:
    setup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
    uri = media.GetStreamUri({"StreamSetup": setup, "ProfileToken": token})
    check("GetStreamUri returns a URI", bool(uri.Uri), uri.Uri)

    # follow EXACTLY the URI zeep was handed — "shows video"
    if HAVE_CAMERA:
        probe = subprocess.run(
            ["ffprobe", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
             "-i", uri.Uri, "-show_entries", "stream=codec_name,width,height",
             "-of", "default=noprint_wrappers=1"],
            capture_output=True, text=True, timeout=30,
        )
        check("the advertised stream URI plays real video", "hevc" in probe.stdout,
              probe.stdout.replace("\n", " ").strip()[:70])

    snap = media.GetSnapshotUri({"ProfileToken": token})
    check("GetSnapshotUri returns a URI", bool(snap.Uri), snap.Uri)

# PTZ — enumerate, and (with a camera) actually move
ptz = cam.create_ptz_service()
nodes = ptz.GetNodes()
check("GetNodes enumerates a PTZ node", len(nodes) >= 1)
if token and HAVE_CAMERA:
    import time
    before = ptz.GetStatus({"ProfileToken": token})
    bx = before.Position.PanTilt.x
    ptz.AbsoluteMove({"ProfileToken": token,
                      "Position": {"PanTilt": {"x": 0.25, "y": -0.15}}})
    time.sleep(6)
    after = ptz.GetStatus({"ProfileToken": token})
    check("AbsoluteMove moved the head (position changed)",
          after.Position.PanTilt.x != bx, f"{bx:.3f} -> {after.Position.PanTilt.x:.3f}")

# 6. events — the pull-point lifecycle
events = cam.create_events_service()
sub = events.CreatePullPointSubscription()
check("CreatePullPointSubscription", sub is not None,
      str(getattr(sub, "SubscriptionReference", ""))[:50])

print(f"\n  {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
