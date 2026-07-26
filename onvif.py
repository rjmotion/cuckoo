"""ONVIF front end — SOAP over HTTP.

Clients ask here for what the camera is, which streams exist, where to fetch them,
and how to move the head. Everything is answered out of the device model, so this
module knows nothing about the camera's own protocol; the callables in `Backend`
are the only way it reaches the controller.

Scope is Profile S plus PTZ, Imaging and pull-point Events: enough for Home
Assistant, ONVIF Device Manager, Synology and friends. Not a general ONVIF
implementation — every verb here is one a real client actually sends.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Final
from xml.etree import ElementTree

from model import PAN, TILT, ZOOM, Camera, Position

ONVIF_PORT: Final = 8000
DEVICE_PATH: Final = "/onvif/device_service"
MEDIA_PATH: Final = "/onvif/media_service"
PTZ_PATH: Final = "/onvif/ptz_service"
IMAGING_PATH: Final = "/onvif/imaging_service"
EVENTS_PATH: Final = "/onvif/events_service"
SNAPSHOT_PATH: Final = "/snapshot/"

SOAP: Final = "http://www.w3.org/2003/05/soap-envelope"
NAMESPACES: Final = {
    "s": SOAP,
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "timg": "http://www.onvif.org/ver20/imaging/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "tns1": "http://www.onvif.org/ver10/topics",
}

MANUFACTURER: Final = "cuckoo"
PTZ_NODE: Final = "PTZNode"
PTZ_CONFIG: Final = "PTZConfig"
MOTION_TOPIC: Final = "tns1:RuleEngine/CellMotionDetector/Motion"
OBJECT_TOPIC: Final = "tns1:RuleEngine/MyRuleDetector"
AUDIO_TOPIC: Final = "tns1:AudioAnalytics/Audio/DetectedSound"

# The camera's object names, mapped onto the topics clients already listen for.
DETECTION_TOPICS: Final[dict[str, tuple[str, str]]] = {
    "person": (f"{OBJECT_TOPIC}/PeopleDetect", "IsPeople"),
    "vehicle": (f"{OBJECT_TOPIC}/VehicleDetect", "IsVehicle"),
    "animal": (f"{OBJECT_TOPIC}/DogCatDetect", "IsDogCat"),
    "package": (f"{OBJECT_TOPIC}/PackageDetect", "IsPackage"),
    "face": (f"{OBJECT_TOPIC}/FaceDetect", "IsFace"),
    "licensePlate": (f"{OBJECT_TOPIC}/LicensePlateDetect", "IsLicensePlate"),
}

log = logging.getLogger("cuckoo.onvif")

# ONVIF encoding names -> the codec strings the controller arms with.
ENCODING_TO_CODEC: dict[str, str] = {
    "H264": "h264", "H265": "h265", "HEVC": "h265", "JPEG": "mjpg", "MJPEG": "mjpg"
}


@dataclass
class Backend:
    """Everything the front end is allowed to ask of the controller."""

    camera: Callable[[], Camera | None]
    stream_uri: Callable[[str], str]
    snapshot_uri: Callable[[str], str]
    snapshot: Callable[[], bytes | None] = lambda: None
    move_absolute: Callable[[Position], bool] = lambda _p: False
    move_relative: Callable[[Position], bool] = lambda _p: False
    goto_preset: Callable[[int, int], bool] = lambda _i, _s: False
    set_preset: Callable[[str, int | None], int | None] = lambda _n, _i: None
    remove_preset: Callable[[int], bool] = lambda _i: False
    refresh_position: Callable[[], bool] = lambda: False
    # Re-arm a channel's codec live: (profile/config token, codec "h264"/"h265").
    set_encoder: Callable[[str, str], bool] = lambda _t, _c: False


# ------------------------------------------------------------------------ events


@dataclass
class Event:
    """One thing worth telling a subscriber about."""

    topic: str
    source: str
    name: str
    value: str
    at: float = field(default_factory=time.time)

    def as_xml(self) -> str:
        stamp = utc(self.at)
        return (
            "<wsnt:NotificationMessage>"
            f"<wsnt:Topic Dialect=\"http://docs.oasis-open.org/wsn/t-1/TopicExpression/Simple\">"
            f"{self.topic}</wsnt:Topic>"
            "<wsnt:Message>"
            f'<tt:Message UtcTime="{stamp}" PropertyOperation="Changed">'
            f'<tt:Source><tt:SimpleItem Name="Source" Value="{self.source}"/></tt:Source>'
            f'<tt:Data><tt:SimpleItem Name="{self.name}" Value="{self.value}"/></tt:Data>'
            "</tt:Message></wsnt:Message></wsnt:NotificationMessage>"
        )


class Subscriptions:
    """Pull-point subscriptions, each with its own backlog."""

    DEPTH: Final = 100

    def __init__(self) -> None:
        self._queues: dict[str, deque[Event]] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def create(self) -> str:
        with self._lock:
            self._counter += 1
            identifier = f"sub{self._counter}"
            self._queues[identifier] = deque(maxlen=self.DEPTH)
        return identifier

    def drop(self, identifier: str) -> None:
        with self._lock:
            self._queues.pop(identifier, None)

    def publish(self, event: Event) -> None:
        with self._lock:
            for queue in self._queues.values():
                queue.append(event)

    def pull(self, identifier: str, limit: int = 10) -> list[Event]:
        with self._lock:
            queue = self._queues.get(identifier)
            if queue is None:
                return []
            return [queue.popleft() for _ in range(min(limit, len(queue)))]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._queues)


def motion_event(camera: Camera, active: bool) -> Event:
    return Event(
        topic=MOTION_TOPIC,
        source=f"VideoSource_{camera.mac}",
        name="IsMotion",
        value="true" if active else "false",
    )


def detection_event(camera: Camera, kind: str, active: bool) -> Event:
    """Map one camera detection onto the topic a client is watching.

    Plain motion has a standard topic. Object detections do not, so they use the
    rule-detector topics clients already recognise, and anything unrecognised gets
    its own rule name rather than being dropped or mislabelled as motion.
    """
    if kind == "motion":
        return motion_event(camera, active)
    if kind.startswith("alrm") or kind == "audio":
        return Event(
            topic=AUDIO_TOPIC,
            source=f"AudioSource_{camera.mac}",
            name=kind if kind != "audio" else "IsSoundDetected",
            value="true" if active else "false",
        )
    topic, name = DETECTION_TOPICS.get(
        kind, (f"{OBJECT_TOPIC}/{kind[:1].upper()}{kind[1:]}Detect", f"Is{kind[:1].upper()}{kind[1:]}")
    )
    return Event(
        topic=topic,
        source=f"VideoSource_{camera.mac}",
        name=name,
        value="true" if active else "false",
    )


# -------------------------------------------------------------------------- SOAP


def utc(at: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at if at is not None else time.time()))


def envelope(body: str) -> str:
    declarations = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in NAMESPACES.items())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<s:Envelope {declarations}><s:Body>{body}</s:Body></s:Envelope>"
    )


def fault(reason: str) -> str:
    return envelope(
        "<s:Fault><s:Code><s:Value>s:Receiver</s:Value></s:Code>"
        f"<s:Reason><s:Text xml:lang=\"en\">{reason}</s:Text></s:Reason></s:Fault>"
    )


def local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


@dataclass
class Call:
    """A parsed SOAP request: which verb, and the body element to read from."""

    action: str
    body: ElementTree.Element

    def find(self, name: str) -> ElementTree.Element | None:
        for element in self.body.iter():
            if local_name(element.tag) == name:
                return element
        return None

    def text(self, name: str, default: str = "") -> str:
        element = self.find(name)
        if element is None or element.text is None:
            return default
        return element.text.strip()

    def attribute(self, element_name: str, attribute: str, default: str = "") -> str:
        element = self.find(element_name)
        if element is None:
            return default
        return element.attrib.get(attribute, default)

    def vector(self, element_name: str) -> tuple[float, float] | None:
        """PanTilt and Zoom arrive as x/y attributes on their own element."""
        element = self.find(element_name)
        if element is None:
            return None
        try:
            return float(element.attrib.get("x", "0")), float(element.attrib.get("y", "0"))
        except ValueError:
            return None


def parse_call(payload: bytes) -> Call | None:
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return None
    body = root.find(f"{{{SOAP}}}Body")
    if body is None or len(body) == 0:
        return None
    first = body[0]
    return Call(action=local_name(first.tag), body=first)


# ---------------------------------------------------------------------- services


class Services:
    """Turns parsed calls into SOAP responses, using only the device model."""

    def __init__(self, backend: Backend, host: str, port: int = ONVIF_PORT) -> None:
        self.backend = backend
        self.host = host
        self.port = port
        self.subscriptions = Subscriptions()
        self.started_at = time.time()

    # ------------------------------------------------------------- addressing

    def address(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    # ---------------------------------------------------------------- dispatch

    def handle(self, call: Call) -> str:
        handler = getattr(self, f"_{_snake(call.action)}", None)
        if handler is None:
            log.info("unhandled ONVIF action %s", call.action)
            return fault(f"Action {call.action} is not implemented")
        result = handler(call)
        assert isinstance(result, str)
        return result

    # ------------------------------------------------------------------ device

    def _get_system_date_and_time(self, call: Call) -> str:
        now = time.gmtime()
        return envelope(
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:DateTimeType>NTP</tt:DateTimeType>"
            "<tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>"
            "<tt:UTCDateTime>"
            f"<tt:Time><tt:Hour>{now.tm_hour}</tt:Hour><tt:Minute>{now.tm_min}</tt:Minute>"
            f"<tt:Second>{now.tm_sec}</tt:Second></tt:Time>"
            f"<tt:Date><tt:Year>{now.tm_year}</tt:Year><tt:Month>{now.tm_mon}</tt:Month>"
            f"<tt:Day>{now.tm_mday}</tt:Day></tt:Date>"
            "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )

    def _get_device_information(self, call: Call) -> str:
        camera = self.backend.camera()
        return envelope(
            "<tds:GetDeviceInformationResponse>"
            f"<tds:Manufacturer>{MANUFACTURER}</tds:Manufacturer>"
            f"<tds:Model>{camera.model if camera else 'unknown'}</tds:Model>"
            f"<tds:FirmwareVersion>{camera.firmware if camera else '0'}</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{camera.mac if camera else '000000000000'}</tds:SerialNumber>"
            f"<tds:HardwareId>{camera.model if camera else 'unknown'}</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )

    def _get_capabilities(self, call: Call) -> str:
        return envelope(
            "<tds:GetCapabilitiesResponse><tds:Capabilities>"
            # ONVIF Capabilities is a strict sequence: Device, Events, Imaging,
            # Media, PTZ. A real client (zeep, which Home Assistant uses) validates
            # against the schema and silently drops any element out of order — so a
            # mis-ordered Events block means HA never creates motion sensors.
            f"<tt:Device><tt:XAddr>{self.address(DEVICE_PATH)}</tt:XAddr>"
            "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve>"
            "<tt:DiscoveryBye>true</tt:DiscoveryBye></tt:System></tt:Device>"
            f"<tt:Events><tt:XAddr>{self.address(EVENTS_PATH)}</tt:XAddr>"
            "<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>"
            "<tt:WSPullPointSupport>true</tt:WSPullPointSupport>"
            "<tt:WSPausableSubscriptionManagerInterfaceSupport>false"
            "</tt:WSPausableSubscriptionManagerInterfaceSupport></tt:Events>"
            f"<tt:Imaging><tt:XAddr>{self.address(IMAGING_PATH)}</tt:XAddr></tt:Imaging>"
            f"<tt:Media><tt:XAddr>{self.address(MEDIA_PATH)}</tt:XAddr>"
            "<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>"
            "<tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
            "</tt:StreamingCapabilities></tt:Media>"
            f"<tt:PTZ><tt:XAddr>{self.address(PTZ_PATH)}</tt:XAddr></tt:PTZ>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )

    def _get_services(self, call: Call) -> str:
        entries = [
            ("http://www.onvif.org/ver10/device/wsdl", DEVICE_PATH),
            ("http://www.onvif.org/ver10/media/wsdl", MEDIA_PATH),
            ("http://www.onvif.org/ver20/ptz/wsdl", PTZ_PATH),
            ("http://www.onvif.org/ver20/imaging/wsdl", IMAGING_PATH),
            ("http://www.onvif.org/ver10/events/wsdl", EVENTS_PATH),
        ]
        body = "".join(
            f"<tds:Service><tds:Namespace>{namespace}</tds:Namespace>"
            f"<tds:XAddr>{self.address(path)}</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version>"
            "</tds:Service>"
            for namespace, path in entries
        )
        return envelope(f"<tds:GetServicesResponse>{body}</tds:GetServicesResponse>")

    def _get_scopes(self, call: Call) -> str:
        camera = self.backend.camera()
        scopes = [
            "onvif://www.onvif.org/type/video_encoder",
            "onvif://www.onvif.org/Profile/Streaming",
            f"onvif://www.onvif.org/name/{camera.name if camera else 'cuckoo'}",
        ]
        if camera is not None and camera.is_ptz:
            scopes.append("onvif://www.onvif.org/type/ptz")
        body = "".join(
            "<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef>"
            f"<tt:ScopeItem>{scope}</tt:ScopeItem></tds:Scopes>"
            for scope in scopes
        )
        return envelope(f"<tds:GetScopesResponse>{body}</tds:GetScopesResponse>")

    def _get_service_capabilities(self, call: Call) -> str:
        return envelope(
            "<tds:GetServiceCapabilitiesResponse><tds:Capabilities>"
            '<tds:Network IPFilter="false" ZeroConfiguration="false" IPVersion6="false"/>'
            '<tds:System DiscoveryResolve="false" DiscoveryBye="true"/>'
            "</tds:Capabilities></tds:GetServiceCapabilitiesResponse>"
        )

    # ------------------------------------------------------------------- media

    def _profile_xml(self, camera: Camera, name: str, prefix: str = "trt:Profiles") -> str:
        track = camera.track(name)
        if track is None:
            return ""
        ptz = ""
        if camera.is_ptz:
            ptz = (
                f'<tt:PTZConfiguration token="{PTZ_CONFIG}">'
                f"<tt:Name>{PTZ_CONFIG}</tt:Name><tt:UseCount>1</tt:UseCount>"
                "<tt:NodeToken>" + PTZ_NODE + "</tt:NodeToken>"
                "<tt:DefaultAbsolutePantTiltPositionSpace>"
                "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"
                "</tt:DefaultAbsolutePantTiltPositionSpace>"
                "<tt:DefaultAbsoluteZoomPositionSpace>"
                "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"
                "</tt:DefaultAbsoluteZoomPositionSpace>"
                "</tt:PTZConfiguration>"
            )
        encoding = "H265" if track.codec.value == "h265" else track.codec.value.upper()
        return (
            f'<{prefix} token="{name}" fixed="true"><tt:Name>{name}</tt:Name>'
            f'<tt:VideoSourceConfiguration token="VideoSource">'
            "<tt:Name>VideoSource</tt:Name><tt:UseCount>1</tt:UseCount>"
            "<tt:SourceToken>VideoSource</tt:SourceToken>"
            f'<tt:Bounds x="0" y="0" width="{track.width}" height="{track.height}"/>'
            "</tt:VideoSourceConfiguration>"
            f'<tt:VideoEncoderConfiguration token="{name}">'
            f"<tt:Name>{name}</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>{encoding}</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{track.width}</tt:Width>"
            f"<tt:Height>{track.height}</tt:Height></tt:Resolution>"
            "<tt:Quality>5</tt:Quality>"
            f"<tt:RateControl><tt:FrameRateLimit>{track.fps}</tt:FrameRateLimit>"
            "<tt:EncodingInterval>1</tt:EncodingInterval>"
            f"<tt:BitrateLimit>{track.bitrate // 1000}</tt:BitrateLimit></tt:RateControl>"
            "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
            "</tt:VideoEncoderConfiguration>"
            f"{ptz}</{prefix}>"
        )

    def _get_profiles(self, call: Call) -> str:
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        body = "".join(self._profile_xml(camera, track.name) for track in camera.tracks)
        return envelope(f"<trt:GetProfilesResponse>{body}</trt:GetProfilesResponse>")

    def _get_profile(self, call: Call) -> str:
        camera = self.backend.camera()
        token = call.text("ProfileToken")
        if camera is None or camera.track(token) is None:
            return fault("no such profile")
        return envelope(
            f"<trt:GetProfileResponse>{self._profile_xml(camera, token, 'trt:Profile')}"
            "</trt:GetProfileResponse>"
        )

    def _get_video_sources(self, call: Call) -> str:
        camera = self.backend.camera()
        track = camera.tracks[0] if camera and camera.tracks else None
        width = track.width if track else 1920
        height = track.height if track else 1080
        fps = track.fps if track else 15
        return envelope(
            '<trt:GetVideoSourcesResponse><trt:VideoSources token="VideoSource">'
            f"<tt:Framerate>{fps}</tt:Framerate>"
            f"<tt:Resolution><tt:Width>{width}</tt:Width><tt:Height>{height}</tt:Height>"
            "</tt:Resolution></trt:VideoSources></trt:GetVideoSourcesResponse>"
        )

    def _get_video_encoder_configurations(self, call: Call) -> str:
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        body = "".join(
            f'<trt:Configurations token="{track.name}"><tt:Name>{track.name}</tt:Name>'
            "<tt:UseCount>1</tt:UseCount>"
            f"<tt:Encoding>{'H265' if track.codec.value == 'h265' else track.codec.value.upper()}"
            "</tt:Encoding>"
            f"<tt:Resolution><tt:Width>{track.width}</tt:Width>"
            f"<tt:Height>{track.height}</tt:Height></tt:Resolution>"
            "</trt:Configurations>"
            for track in camera.tracks
        )
        return envelope(
            f"<trt:GetVideoEncoderConfigurationsResponse>{body}"
            "</trt:GetVideoEncoderConfigurationsResponse>"
        )

    def _set_video_encoder_configuration(self, call: Call) -> str:
        """Re-arm a channel's codec on the fly (ONVIF SetVideoEncoderConfiguration).

        Home Assistant never calls this — it consumes the profiles it is given — but
        a fuller ONVIF client can flip a channel between H.264 and H.265 at runtime,
        and it rides the same adoption-time settings path (a fresh ChangeVideoSettings
        to the camera). The token is the channel (video1/…); Encoding is H264/H265.
        """
        camera = self.backend.camera()
        token = call.attribute("Configuration", "token") or call.text("ConfigurationToken")
        encoding = call.text("Encoding")
        if camera is None or not token or camera.track(token) is None:
            return fault("no such video encoder configuration")
        codec = ENCODING_TO_CODEC.get(encoding.upper())
        if codec is None:
            return fault(f"unsupported encoding {encoding!r}")
        if not self.backend.set_encoder(token, codec):
            return fault("could not apply video encoder configuration")
        return envelope("<trt:SetVideoEncoderConfigurationResponse/>")

    def _get_stream_uri(self, call: Call) -> str:
        token = call.text("ProfileToken") or "video1"
        uri = self.backend.stream_uri(token)
        return envelope(
            "<trt:GetStreamUriResponse><trt:MediaUri>"
            f"<tt:Uri>{uri}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri></trt:GetStreamUriResponse>"
        )

    def _get_snapshot_uri(self, call: Call) -> str:
        token = call.text("ProfileToken") or "video1"
        return envelope(
            "<trt:GetSnapshotUriResponse><trt:MediaUri>"
            f"<tt:Uri>{self.backend.snapshot_uri(token)}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT60S</tt:Timeout>"
            "</trt:MediaUri></trt:GetSnapshotUriResponse>"
        )

    # --------------------------------------------------------------------- PTZ

    def _get_nodes(self, call: Call) -> str:
        return envelope(f"<tptz:GetNodesResponse>{self._node_xml('tptz:PTZNode')}</tptz:GetNodesResponse>")

    def _get_node(self, call: Call) -> str:
        return envelope(f"<tptz:GetNodeResponse>{self._node_xml('tptz:PTZNode')}</tptz:GetNodeResponse>")

    def _node_xml(self, element: str) -> str:
        camera = self.backend.camera()
        presets = len(camera.presets) if camera else 0
        return (
            f'<{element} token="{PTZ_NODE}"><tt:Name>{PTZ_NODE}</tt:Name>'
            "<tt:SupportedPTZSpaces>"
            "<tt:AbsolutePanTiltPositionSpace>"
            "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace</tt:URI>"
            '<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>'
            '<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>'
            "</tt:AbsolutePanTiltPositionSpace>"
            "<tt:AbsoluteZoomPositionSpace>"
            "<tt:URI>http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace</tt:URI>"
            "<tt:XRange><tt:Min>0.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
            "</tt:AbsoluteZoomPositionSpace>"
            "<tt:RelativePanTiltTranslationSpace>"
            "<tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace</tt:URI>"
            "<tt:XRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:XRange>"
            "<tt:YRange><tt:Min>-1.0</tt:Min><tt:Max>1.0</tt:Max></tt:YRange>"
            "</tt:RelativePanTiltTranslationSpace>"
            "</tt:SupportedPTZSpaces>"
            f"<tt:MaximumNumberOfPresets>{max(64, presets)}</tt:MaximumNumberOfPresets>"
            "<tt:HomeSupported>false</tt:HomeSupported>"
            f"</{element}>"
        )

    def _get_configurations(self, call: Call) -> str:
        return envelope(
            "<tptz:GetConfigurationsResponse>"
            f'<tptz:PTZConfiguration token="{PTZ_CONFIG}">'
            f"<tt:Name>{PTZ_CONFIG}</tt:Name><tt:UseCount>1</tt:UseCount>"
            f"<tt:NodeToken>{PTZ_NODE}</tt:NodeToken>"
            "</tptz:PTZConfiguration></tptz:GetConfigurationsResponse>"
        )

    def _get_configuration(self, call: Call) -> str:
        return self._get_configurations(call)

    def _get_status(self, call: Call) -> str:
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        self.backend.refresh_position()
        position = camera.motion.position
        pan = camera.pan_range.to_normalised(position.pan)
        tilt = camera.tilt_range.to_normalised(position.tilt)
        zoom = (camera.zoom_range.to_normalised(position.zoom) + 1.0) / 2.0
        state = "IDLE" if camera.motion.settled else "MOVING"
        return envelope(
            "<tptz:GetStatusResponse><tptz:PTZStatus>"
            f'<tt:Position><tt:PanTilt x="{pan:.4f}" y="{tilt:.4f}"/>'
            f'<tt:Zoom x="{zoom:.4f}"/></tt:Position>'
            f"<tt:MoveStatus><tt:PanTilt>{state}</tt:PanTilt>"
            f"<tt:Zoom>{state}</tt:Zoom></tt:MoveStatus>"
            f"<tt:UtcTime>{utc()}</tt:UtcTime>"
            "</tptz:PTZStatus></tptz:GetStatusResponse>"
        )

    def _target_from(self, call: Call, camera: Camera, relative: bool) -> Position:
        current = camera.motion.position
        pan_tilt = call.vector("PanTilt")
        zoom = call.vector("Zoom")
        pan, tilt = current.pan, current.tilt
        if pan_tilt is not None:
            if relative:
                span_pan = camera.pan_range.maximum - camera.pan_range.minimum
                span_tilt = camera.tilt_range.maximum - camera.tilt_range.minimum
                pan = camera.pan_range.clamp(round(pan + pan_tilt[0] * span_pan / 2))
                tilt = camera.tilt_range.clamp(round(tilt + pan_tilt[1] * span_tilt / 2))
            else:
                pan = camera.pan_range.from_normalised(pan_tilt[0])
                tilt = camera.tilt_range.from_normalised(pan_tilt[1])
        zoom_value = current.zoom
        if zoom is not None:
            if relative:
                span = camera.zoom_range.maximum - camera.zoom_range.minimum
                zoom_value = camera.zoom_range.clamp(round(zoom_value + zoom[0] * span))
            else:
                # ONVIF zoom is 0..1 where pan and tilt are -1..1.
                zoom_value = camera.zoom_range.from_normalised(zoom[0] * 2.0 - 1.0)
        return Position(pan=pan, tilt=tilt, zoom=zoom_value, focus=current.focus)

    def _absolute_move(self, call: Call) -> str:
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        moved = self.backend.move_absolute(self._target_from(call, camera, relative=False))
        if not moved:
            return fault("the camera is not accepting movement")
        return envelope("<tptz:AbsoluteMoveResponse/>")

    def _relative_move(self, call: Call) -> str:
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        moved = self.backend.move_absolute(self._target_from(call, camera, relative=True))
        if not moved:
            return fault("the camera is not accepting movement")
        return envelope("<tptz:RelativeMoveResponse/>")

    def _continuous_move(self, call: Call) -> str:
        """The camera has no continuous verb, so velocity becomes one relative step.

        A client holding an arrow key sends these repeatedly, which gives the same
        felt behaviour without pretending to a mode the camera does not have.
        """
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        moved = self.backend.move_absolute(self._target_from(call, camera, relative=True))
        if not moved:
            return fault("the camera is not accepting movement")
        return envelope("<tptz:ContinuousMoveResponse/>")

    def _stop(self, call: Call) -> str:
        # Each step completes on its own, so there is nothing to interrupt.
        self.backend.refresh_position()
        return envelope("<tptz:StopResponse/>")

    def _get_presets(self, call: Call) -> str:
        camera = self.backend.camera()
        if camera is None:
            return fault("no camera")
        body = ""
        for index, preset in sorted(camera.presets.items()):
            pan = camera.pan_range.to_normalised(preset.position.pan)
            tilt = camera.tilt_range.to_normalised(preset.position.tilt)
            zoom = (camera.zoom_range.to_normalised(preset.position.zoom) + 1.0) / 2.0
            body += (
                f'<tptz:Preset token="{index}"><tt:Name>{preset.name}</tt:Name>'
                f'<tt:PTZPosition><tt:PanTilt x="{pan:.4f}" y="{tilt:.4f}"/>'
                f'<tt:Zoom x="{zoom:.4f}"/></tt:PTZPosition></tptz:Preset>'
            )
        return envelope(f"<tptz:GetPresetsResponse>{body}</tptz:GetPresetsResponse>")

    def _goto_preset(self, call: Call) -> str:
        token = call.text("PresetToken")
        try:
            index = int(token)
        except ValueError:
            return fault("preset tokens are numeric here")
        if not self.backend.goto_preset(index, 1000):
            return fault("the camera is not accepting movement")
        return envelope("<tptz:GotoPresetResponse/>")

    def _set_preset(self, call: Call) -> str:
        name = call.text("PresetName") or "preset"
        token = call.text("PresetToken")
        index: int | None
        try:
            index = int(token) if token else None
        except ValueError:
            index = None
        assigned = self.backend.set_preset(name, index)
        if assigned is None:
            return fault("the camera is not accepting presets")
        return envelope(
            f'<tptz:SetPresetResponse><tptz:PresetToken>{assigned}</tptz:PresetToken>'
            "</tptz:SetPresetResponse>"
        )

    def _remove_preset(self, call: Call) -> str:
        try:
            index = int(call.text("PresetToken"))
        except ValueError:
            return fault("preset tokens are numeric here")
        if not self.backend.remove_preset(index):
            return fault("no such preset")
        return envelope("<tptz:RemovePresetResponse/>")

    # ----------------------------------------------------------------- imaging

    def _get_imaging_settings(self, call: Call) -> str:
        return envelope(
            "<timg:GetImagingSettingsResponse><timg:ImagingSettings>"
            "<tt:Brightness>50</tt:Brightness><tt:Contrast>50</tt:Contrast>"
            "<tt:ColorSaturation>50</tt:ColorSaturation><tt:Sharpness>50</tt:Sharpness>"
            "</timg:ImagingSettings></timg:GetImagingSettingsResponse>"
        )

    def _get_options(self, call: Call) -> str:
        limits = "<tt:Min>0</tt:Min><tt:Max>100</tt:Max>"
        return envelope(
            "<timg:GetOptionsResponse><timg:ImagingOptions>"
            f"<tt:Brightness>{limits}</tt:Brightness><tt:Contrast>{limits}</tt:Contrast>"
            f"<tt:ColorSaturation>{limits}</tt:ColorSaturation><tt:Sharpness>{limits}</tt:Sharpness>"
            "</timg:ImagingOptions></timg:GetOptionsResponse>"
        )

    # ------------------------------------------------------------------ events

    def _create_pull_point_subscription(self, call: Call) -> str:
        identifier = self.subscriptions.create()
        address = f"{self.address(EVENTS_PATH)}?sub={identifier}"
        return envelope(
            "<tev:CreatePullPointSubscriptionResponse>"
            "<tev:SubscriptionReference>"
            f"<wsa:Address>{address}</wsa:Address>"
            "</tev:SubscriptionReference>"
            f"<wsnt:CurrentTime>{utc()}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{utc(time.time() + 60)}</wsnt:TerminationTime>"
            "</tev:CreatePullPointSubscriptionResponse>"
        )

    def pull_messages(self, identifier: str, limit: int = 10) -> str:
        events = self.subscriptions.pull(identifier, limit)
        body = "".join(event.as_xml() for event in events)
        return envelope(
            "<tev:PullMessagesResponse>"
            f"<tev:CurrentTime>{utc()}</tev:CurrentTime>"
            f"<tev:TerminationTime>{utc(time.time() + 60)}</tev:TerminationTime>"
            f"{body}</tev:PullMessagesResponse>"
        )

    def _pull_messages(self, call: Call) -> str:
        # Without a subscription id on the URL, serve whichever one exists.
        return self.pull_messages(self._only_subscription())

    def _renew(self, call: Call) -> str:
        return envelope(
            "<wsnt:RenewResponse>"
            f"<wsnt:CurrentTime>{utc()}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{utc(time.time() + 60)}</wsnt:TerminationTime>"
            "</wsnt:RenewResponse>"
        )

    def _unsubscribe(self, call: Call) -> str:
        self.subscriptions.drop(self._only_subscription())
        return envelope("<wsnt:UnsubscribeResponse/>")

    def _get_event_properties(self, call: Call) -> str:
        return envelope(
            "<tev:GetEventPropertiesResponse>"
            "<tev:TopicNamespaceLocation>"
            "http://www.onvif.org/onvif/ver10/topics/topicns.xml"
            "</tev:TopicNamespaceLocation>"
            "<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>"
            f"<wstop:TopicSet xmlns:wstop=\"http://docs.oasis-open.org/wsn/t-1\">"
            "<tns1:RuleEngine><CellMotionDetector><Motion wstop:topic=\"true\"/>"
            "</CellMotionDetector><MyRuleDetector>"
            "<PeopleDetect wstop:topic=\"true\"/><VehicleDetect wstop:topic=\"true\"/>"
            "</MyRuleDetector></tns1:RuleEngine></wstop:TopicSet>"
            "<wsnt:TopicExpressionDialect>"
            "http://docs.oasis-open.org/wsn/t-1/TopicExpression/Concrete"
            "</wsnt:TopicExpressionDialect>"
            "</tev:GetEventPropertiesResponse>"
        )

    def _only_subscription(self) -> str:
        with self.subscriptions._lock:  # noqa: SLF001 - same module
            return next(iter(self.subscriptions._queues), "")


def _snake(action: str) -> str:
    out: list[str] = []
    for index, character in enumerate(action):
        if character.isupper() and index:
            out.append("_")
        out.append(character.lower())
    return "".join(out)


# -------------------------------------------------------------------- transport


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def services(self) -> Services:
        server = self.server
        assert isinstance(server, OnvifServer)
        return server.services

    def log_message(self, format: str, *args: object) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802 - name fixed by http.server
        if self.path.startswith(SNAPSHOT_PATH):
            image = self.services.backend.snapshot()
            if image is None:
                self._send(HTTPStatus.SERVICE_UNAVAILABLE, b"", "text/plain")
                return
            self._send(HTTPStatus.OK, image, "image/jpeg")
            return
        self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - name fixed by http.server
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        payload = self.rfile.read(length) if length else b""
        call = parse_call(payload)
        if call is None:
            self._send(HTTPStatus.BAD_REQUEST, fault("unparseable request").encode(), "text/xml")
            return
        subscription = self._subscription_from_path()
        if call.action == "PullMessages" and subscription:
            body = self.services.pull_messages(subscription)
        elif call.action == "Unsubscribe" and subscription:
            self.services.subscriptions.drop(subscription)
            body = envelope("<wsnt:UnsubscribeResponse/>")
        else:
            body = self.services.handle(call)
        status = HTTPStatus.INTERNAL_SERVER_ERROR if "s:Fault" in body else HTTPStatus.OK
        self._send(status, body.encode(), "application/soap+xml; charset=utf-8")

    def _subscription_from_path(self) -> str:
        _, _, query = self.path.partition("?")
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key == "sub":
                return value
        return ""

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


class OnvifServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, services: Services, port: int = ONVIF_PORT) -> None:
        self.services = services
        super().__init__(("0.0.0.0", port), _Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def start(self) -> None:
        log.info("onvif listening on :%d%s", self.port, DEVICE_PATH)
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
