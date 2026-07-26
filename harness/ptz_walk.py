"""The full ONVIF PTZ surface, through Home Assistant's real client (onvif-zeep).

The onboarding walk (ha_walk.py) only proved a PTZ node exists and one AbsoluteMove
lands. This exercises everything a client can drive: all three move kinds
(Absolute, Relative, Continuous+Stop) and the preset lifecycle — Set, Get, Goto,
Remove — which is what "saved viewpoints" are in ONVIF terms.
"""

from __future__ import annotations

import sys
import time

from onvif import ONVIFCamera

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18000

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


cam = ONVIFCamera(HOST, PORT, "", "")
media = cam.create_media_service()
ptz = cam.create_ptz_service()
token = media.GetProfiles()[0].token


def pantilt() -> tuple[float, float]:
    p = ptz.GetStatus({"ProfileToken": token}).Position.PanTilt
    return round(p.x, 4), round(p.y, 4)


# --- configuration a client reads before it can drive --------------------------

cfgs = ptz.GetConfigurations()
check("GetConfigurations", len(cfgs) >= 1, f"{len(cfgs)} config(s)")

node = ptz.GetNodes()[0]
spaces = node.SupportedPTZSpaces
check("node advertises AbsolutePanTilt space", bool(spaces.AbsolutePanTiltPositionSpace))
check("node advertises RelativePanTilt space", bool(spaces.RelativePanTiltTranslationSpace))
check("node advertises a preset limit",
      getattr(node, "MaximumNumberOfPresets", 0) >= 1,
      f"max={getattr(node,'MaximumNumberOfPresets',None)}")

# --- the three move kinds ------------------------------------------------------

ptz.AbsoluteMove({"ProfileToken": token, "Position": {"PanTilt": {"x": 0.0, "y": 0.0}}})
time.sleep(6)
at_zero = pantilt()
check("AbsoluteMove to (0,0)", abs(at_zero[0]) < 0.1, f"landed at {at_zero}")

ptz.RelativeMove({"ProfileToken": token, "Translation": {"PanTilt": {"x": 0.3, "y": 0.0}}})
time.sleep(6)
after_rel = pantilt()
check("RelativeMove nudged pan from the current position", after_rel[0] > at_zero[0],
      f"{at_zero} -> {after_rel}")

before_cont = pantilt()
ptz.ContinuousMove({"ProfileToken": token, "Velocity": {"PanTilt": {"x": -0.5, "y": 0.0}}})
time.sleep(3)
ptz.Stop({"ProfileToken": token, "PanTilt": True})
time.sleep(4)
after_cont = pantilt()
check("ContinuousMove + Stop changed the position", after_cont != before_cont,
      f"{before_cont} -> {after_cont}")

# --- presets = saved viewpoints ------------------------------------------------

# Point somewhere distinctive, then save it as a named viewpoint.
ptz.AbsoluteMove({"ProfileToken": token, "Position": {"PanTilt": {"x": 0.5, "y": -0.3}}})
time.sleep(6)
saved_at = pantilt()

new_token = ptz.SetPreset({"ProfileToken": token, "PresetName": "corner-view"})
check("SetPreset saves the current view", bool(new_token), f"token={new_token}")

presets = ptz.GetPresets({"ProfileToken": token})
names = [getattr(p, "Name", None) for p in presets]
check("GetPresets lists the saved viewpoint", "corner-view" in names,
      f"{len(presets)} preset(s): {names}")

# Move away, then recall the viewpoint and confirm we return to it.
ptz.AbsoluteMove({"ProfileToken": token, "Position": {"PanTilt": {"x": -0.4, "y": 0.2}}})
time.sleep(6)
moved_away = pantilt()

ptz.GotoPreset({"ProfileToken": token, "PresetToken": str(new_token)})
time.sleep(6)
recalled = pantilt()
close = abs(recalled[0] - saved_at[0]) < 0.15 and abs(recalled[1] - saved_at[1]) < 0.15
check("GotoPreset returns to the saved viewpoint", close,
      f"saved {saved_at}, left to {moved_away}, recalled {recalled}")

ptz.RemovePreset({"ProfileToken": token, "PresetToken": str(new_token)})
after_remove = [getattr(p, "Name", None) for p in ptz.GetPresets({"ProfileToken": token})]
check("RemovePreset drops the viewpoint", "corner-view" not in after_remove,
      f"remaining: {after_remove}")

print(f"\n  {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
