"""Detection events: camera payloads in, ONVIF topics out. No camera."""

from __future__ import annotations

import events
import onvif
from tests_support import a_camera

CAMERA = a_camera()


def test_plain_motion_starts_and_stops() -> None:
    started = events.parse(events.EVENT_ANALYTICS, {"eventType": "motion", "edgeType": "start"})
    stopped = events.parse(events.EVENT_ANALYTICS, {"eventType": "motion", "edgeType": "stop"})
    assert started == [events.Detection(kind="motion", active=True)]
    assert stopped == [events.Detection(kind="motion", active=False)]


def test_smart_detect_enters_and_leaves_per_object_type() -> None:
    found = events.parse(
        events.EVENT_SMART_DETECT,
        {"eventType": "smartDetectZone", "edgeType": "enter",
         "objectTypes": ["person", "vehicle"], "eventId": "abc"},
    )
    assert [d.kind for d in found] == ["person", "vehicle"]
    assert all(d.active and d.event_id == "abc" for d in found)


def test_leave_is_the_end_of_a_detection_not_a_new_one() -> None:
    found = events.parse(
        events.EVENT_SMART_DETECT, {"edgeType": "leave", "objectTypes": ["person"]}
    )
    assert found == [events.Detection(kind="person", active=False)]


def test_boolean_enter_and_leave_flags_are_understood() -> None:
    """Not every verb spells the edge the same way."""
    assert events.parse(events.EVENT_SMART_AUDIO, {"enter": 1, "alrmSiren": 1}) == [
        events.Detection(kind="alrmSiren", active=True)
    ]
    assert events.parse(events.EVENT_SMART_AUDIO, {"leave": 1, "alrmSmoke": 1}) == [
        events.Detection(kind="alrmSmoke", active=False)
    ]


def test_audio_without_a_class_still_reports_something() -> None:
    assert events.parse(events.EVENT_SMART_AUDIO, {"edgeType": "enter"}) == [
        events.Detection(kind="audio", active=True)
    ]


def test_smart_motion_shares_the_smart_detect_shape() -> None:
    found = events.parse(
        events.EVENT_SMART_MOTION, {"edgeType": "enter", "objectTypes": ["animal"]}
    )
    assert found == [events.Detection(kind="animal", active=True)]


def test_an_event_without_an_edge_is_not_a_detection() -> None:
    assert events.parse(events.EVENT_ANALYTICS, {"eventType": "motion"}) == []


def test_other_verbs_are_not_detections() -> None:
    assert events.parse("EventPoorNetwork", {"edgeType": "start"}) == []
    assert events.parse("EventMotorState", {"edgeType": "start"}) == []


def test_missing_event_type_falls_back_to_motion() -> None:
    assert events.parse(events.EVENT_ANALYTICS, {"edgeType": "start"}) == [
        events.Detection(kind="motion", active=True)
    ]


# ------------------------------------------------------------- mapping to ONVIF


def test_motion_maps_to_the_standard_topic() -> None:
    event = onvif.detection_event(CAMERA, "motion", active=True)
    assert event.topic == onvif.MOTION_TOPIC
    assert event.name == "IsMotion" and event.value == "true"


def test_known_object_types_map_to_the_rule_topics_clients_watch() -> None:
    person = onvif.detection_event(CAMERA, "person", active=True)
    vehicle = onvif.detection_event(CAMERA, "vehicle", active=False)
    assert person.topic.endswith("MyRuleDetector/PeopleDetect") and person.name == "IsPeople"
    assert vehicle.topic.endswith("MyRuleDetector/VehicleDetect") and vehicle.value == "false"


def test_an_unknown_object_type_gets_its_own_rule_rather_than_being_dropped() -> None:
    event = onvif.detection_event(CAMERA, "bicycle", active=True)
    assert event.topic.endswith("MyRuleDetector/BicycleDetect")
    assert event.name == "IsBicycle"


def test_audio_alarms_map_to_the_audio_topic() -> None:
    event = onvif.detection_event(CAMERA, "alrmSmoke", active=True)
    assert event.topic == onvif.AUDIO_TOPIC
    assert event.name == "alrmSmoke"


def test_the_event_xml_carries_topic_source_and_value() -> None:
    xml = onvif.detection_event(CAMERA, "person", active=True).as_xml()
    assert "MyRuleDetector/PeopleDetect" in xml
    assert f'Value="VideoSource_{CAMERA.mac}"' in xml
    assert 'Name="IsPeople" Value="true"' in xml
