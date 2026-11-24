"""zones.py: the zone catalogue, unique ids, and what live updates mean."""

from __future__ import annotations

import copy
import json
import unittest

from _load import load_zones

zones = load_zones()

MAC = "AABBCCDDEEFF"


class ParseCameras(unittest.TestCase):
    def test_zones_lines_and_loiter_zones(self):
        got = zones.parse_cameras(
            [
                {
                    "mac": MAC,
                    "id": "cam1",
                    "name": "Front",
                    "smartDetectZones": [
                        {"id": 1, "name": " Drive ", "color": "#AB46BC", "points": [[0, 0], [1, 0], [0.5, 0.25]]},
                        {"id": 2, "name": ""},
                    ],
                    "smartDetectLines": [{"id": 1, "name": "Gate", "color": "red", "points": [[0.1, 0.5], [0.9, 0.5]]}],
                    "smartDetectLoiterZones": [],
                }
            ]
        )
        self.assertEqual(
            got,
            {
                MAC: {
                    "id": "cam1",
                    "name": "Front",
                    "ptz": False,
                    "connected": True,
                    "zone": [
                        {"id": 1, "name": "Drive", "color": "#ab46bc", "points": [[0.0, 0.0], [1.0, 0.0], [0.5, 0.25]], "types": None, "dwell": None},
                        {"id": 2, "name": "#2", "color": None, "points": [], "types": None, "dwell": None},
                    ],
                    "line": [
                        {"id": 1, "name": "Gate", "color": None, "points": [[0.1, 0.5], [0.9, 0.5]], "types": None, "dwell": None}
                    ],
                    "loiter": [],
                    "missing": (),
                }
            },
        )

    def test_a_left_out_key_is_missing_not_empty(self):
        cam = zones.parse_cameras([{"mac": MAC, "smartDetectZones": [], "smartDetectLines": None}])[MAC]
        self.assertEqual(cam["missing"], ("line", "loiter"))
        self.assertEqual((cam["zone"], cam["line"], cam["loiter"]), ([], [], []))
        self.assertEqual(cam["name"], MAC)  # no name: the mac

    def test_odd_entries_are_skipped(self):
        got = zones.parse_cameras(
            [
                None,
                {"name": "no mac"},
                {"mac": MAC, "smartDetectZones": ["x", {"name": "no id"}, {"id": "7"}, {"id": True}, {"id": 3}]},
            ]
        )
        self.assertEqual(list(got), [MAC])
        self.assertEqual(got[MAC]["zone"], [{"id": 3, "name": "#3", "color": None, "points": [], "types": None, "dwell": None}])

    def test_no_cameras_is_an_answer(self):
        self.assertEqual(zones.parse_cameras([]), {})

    def test_not_a_list_is_an_error(self):
        for answer in (None, {"error": "x"}, "cameras"):
            with self.assertRaises(ValueError):
                zones.parse_cameras(answer)


class ColorsAndPoints(unittest.TestCase):
    def test_color(self):
        self.assertEqual(zones.parse_color("#AB46BC"), "#ab46bc")
        self.assertEqual(zones.parse_color(" 00ff7f "), "#00ff7f")
        for value in (None, "", "red", "#fff", "#12345g", "#1234567", 0xFFFFFF, ["#ffffff"]):
            self.assertIsNone(zones.parse_color(value))

    def test_points(self):
        self.assertEqual(zones.parse_points([[0.25, 0.4], (1, 0), [0.123456, 1.0]]), [[0.25, 0.4], [1.0, 0.0], [0.1235, 1.0]])
        self.assertEqual(zones.parse_points([{"x": 0.1, "y": 0.2}]), [[0.1, 0.2]])
        self.assertEqual(zones.parse_points([[-0.0005, 1.0005]]), [[0.0, 1.0]])  # rounding noise

    def test_points_we_would_have_to_guess_at_give_none(self):
        for value in (
            None,
            "0.1,0.2",
            [[0.1, 0.2], [640, 360]],  # pixels?
            [[10, 20], [50, 60]],  # percent?
            [[0.1, 0.2, 0.3]],
            [[0.1, "0.2"]],
            [[0.1, True]],
            [[0.1, float("nan")]],
            [0.1, 0.2],
            [{"x": 0.1}],
        ):
            self.assertEqual(zones.parse_points(value), [])


class Overlay(unittest.TestCase):
    def setUp(self):
        self.catalogue = zones.parse_cameras(
            [
                {
                    "mac": MAC,
                    "id": "cam1",
                    "name": "Front",
                    "smartDetectZones": [
                        {"id": 1, "name": "Drive", "color": "#AB46BC", "points": [[0, 0], [1, 0], [0.5, 0.5]]},
                        {"id": 2, "name": "Lawn"},
                    ],
                    "smartDetectLines": [{"id": 1, "name": "Gate", "points": [[0.1, 0.5], [0.9, 0.5]]}],
                    "smartDetectLoiterZones": [{"id": 1, "name": "Porch", "color": "#00ff00"}],
                },
                {"mac": "112233445566", "id": "cam2", "name": "No zones", "smartDetectZones": []},
                {"mac": "665544332211", "name": "No id", "smartDetectZones": [{"id": 1}]},
            ]
        )
        self.occupied = {(MAC, 1): (True, ["person", "vehicle"]), (MAC, 2): (False, [])}

    def payload(self, live=True):
        return zones.overlay_payload(self.catalogue, lambda mac, zid: self.occupied[mac, zid], live)

    def test_payload(self):
        self.assertEqual(
            self.payload(),
            {
                "cameras": {
                    "cam1": {
                        "mac": MAC,
                        "name": "Front",
                        "connected": True,
                        "aligned": None,
                        "zones": [
                            {
                                "id": 1,
                                "kind": "zone",
                                "name": "Drive",
                                "color": "#ab46bc",
                                "types": None,
                                "dwell": None,
                                "points": [[0.0, 0.0], [1.0, 0.0], [0.5, 0.5]],
                                "occupied": True,
                                "detected": "person,vehicle",
                            },
                            {"id": 2, "kind": "zone", "name": "Lawn", "color": None, "types": None, "dwell": None, "points": [], "occupied": False, "detected": None},
                            {"id": 1, "kind": "line", "name": "Gate", "color": None, "types": None, "dwell": None, "points": [[0.1, 0.5], [0.9, 0.5]], "occupied": None, "detected": None},
                            {"id": 1, "kind": "loiter", "name": "Porch", "color": "#00ff00", "types": None, "dwell": None, "points": [], "occupied": None, "detected": None},
                        ],
                    }
                }
            },
        )

    HOME = {"id": "p1", "name": "HomeView", "slot": -1, "ptz": {"pan": 25300, "tilt": 11700, "zoom": 0, "focus": 0}}

    def position(self, pan=25300, tilt=11700, zoom=0):
        return {"degree": {"pan": 73, "tilt": 27, "zoom": 0}, "steps": {"focus": 0, "pan": pan, "tilt": tilt, "zoom": zoom}}

    def test_at_home(self):
        self.assertIs(zones.ptz_aligned(self.HOME, self.position()), True)
        self.assertIs(zones.ptz_aligned(self.HOME, self.position(pan=25310, tilt=11690, zoom=3)), True)  # settling

    def test_moved(self):
        self.assertIs(zones.ptz_aligned(self.HOME, self.position(pan=25400)), False)  # one degree
        self.assertIs(zones.ptz_aligned(self.HOME, self.position(tilt=11600)), False)
        self.assertIs(zones.ptz_aligned(self.HOME, self.position(zoom=50)), False)

    def test_cannot_tell(self):
        for home, position in (
            (None, self.position()),
            (self.HOME, None),
            ({"slot": -1}, self.position()),
            (self.HOME, {"degree": {"pan": 73, "tilt": 27, "zoom": 0}}),
            ({"ptz": {"pan": 25300, "tilt": 11700}}, self.position()),
            ({"ptz": {"pan": "25300", "tilt": 11700, "zoom": 0}}, self.position()),
            (self.HOME, {"steps": {"pan": True, "tilt": 11700, "zoom": 0}}),
            ([], "x"),
        ):
            self.assertIsNone(zones.ptz_aligned(home, position), (home, position))

    def test_aligned_only_for_a_camera_that_can_move(self):
        occupancy = lambda mac, zid: (False, [])  # noqa: E731
        asked = []

        def aligned(mac):
            asked.append(mac)
            return False

        self.assertIsNone(zones.overlay_payload(self.catalogue, occupancy, True, aligned)["cameras"]["cam1"]["aligned"])
        self.assertEqual(asked, [])
        self.catalogue[MAC]["ptz"] = True
        self.assertIs(zones.overlay_payload(self.catalogue, occupancy, True, aligned)["cameras"]["cam1"]["aligned"], False)
        self.assertIsNone(zones.overlay_payload(self.catalogue, occupancy, True, {}.get)["cameras"]["cam1"]["aligned"])
        self.assertIsNone(zones.overlay_payload(self.catalogue, occupancy, True)["cameras"]["cam1"]["aligned"])

    def test_ptz_flag(self):
        got = zones.parse_cameras(
            [
                {"mac": "A", "featureFlags": {"isPtz": True}},
                {"mac": "B", "featureFlags": {"isPtz": False}},
                {"mac": "C", "featureFlags": None},
                {"mac": "D", "featureFlags": {"isPtz": "yes"}},
            ]
        )
        self.assertEqual([cam["ptz"] for cam in got.values()], [True, False, False, False])

    def test_one_thing_in_the_zone(self):
        self.occupied[MAC, 1] = (True, ["person"])
        self.assertEqual(self.payload()["cameras"]["cam1"]["zones"][0]["detected"], "person")

    def test_occupancy_unknown_while_not_live(self):
        shapes = self.payload(live=False)["cameras"]["cam1"]["zones"]
        self.assertEqual({(s["occupied"], s["detected"]) for s in shapes}, {(None, None)})
        self.assertEqual(shapes[0]["points"], [[0.0, 0.0], [1.0, 0.0], [0.5, 0.5]])  # the shapes are still known

    def test_nothing_set_up(self):
        self.assertEqual(zones.overlay_payload(None, lambda mac, zid: (False, []), False), {"cameras": {}})
        self.assertEqual(zones.overlay_payload({}, lambda mac, zid: (False, []), True), {"cameras": {}})

    def test_is_plain_json(self):
        import json

        self.assertEqual(json.loads(json.dumps(self.payload())), self.payload())

    def test_with_the_tracker(self):
        tracker = zones.ZoneTracker({"cam1": (MAC, "Front")}.get, lambda *a: None, lambda *a: None)
        self.assertEqual(tracker.occupancy(MAC, 1), (False, []))
        tracker.detection(
            "e1",
            {"type": "smartDetectZone", "camera": "cam1", "smartDetectTypes": ["person"], "metadata": {"zonesStatus": {"1": {"status": "enter"}}}},
        )
        self.assertEqual(tracker.occupancy(MAC, 1), (True, ["person"]))
        shapes = zones.overlay_payload(self.catalogue, tracker.occupancy, True)["cameras"]["cam1"]["zones"]
        self.assertEqual([(s["occupied"], s["detected"]) for s in shapes[:2]], [(True, "person"), (False, None)])
        tracker.detection("e1", {"end": 1})
        self.assertEqual(tracker.occupancy(MAC, 1), (False, []))


# The zone lists of a real camera (a G6 PTZ), as Protect sends them. Zones 4+8
# and 5+9 share a mergeId: Protect shows each pair as one zone.
REAL_CAMERA = json.loads(
    """
{"id": "cam1", "mac": "AABBCCDDEEFF", "name": "Front Door", "micVolume": 100,
 "smartDetectZones": [
  {"id": 3, "name": "Doorstep", "color": "#AB46BC", "points": [[0.839, 0.349], [0.874, 0.331], [1, 0.465], [1, 1], [0.277, 1], [0.207, 0.625], [0.516, 0.487]], "sensitivity": 74, "objectTypes": ["animal", "face", "vehicle", "person"], "isTriggerLightEnabled": true, "source": "unifi-protect", "triggerAccessTypes": [], "enableAccessLPOnlyMode": false, "mergeId": null},
  {"id": 4, "name": " Front Yard", "color": "#586CED", "points": [[0.615, 0.212], [0.87, 0.333], [0.796, 0.36], [0.712, 0.397], [0.333, 0.567]], "sensitivity": 50, "objectTypes": ["animal", "face", "vehicle"], "isTriggerLightEnabled": true, "source": "unifi-protect", "triggerAccessTypes": [], "enableAccessLPOnlyMode": false, "mergeId": "1794971031812"},
  {"id": 5, "name": "Street", "color": "#586CED", "points": [[0, 0], [0.832, 0], [0.814, 0.242], [0.589, 0.23], [0.4, 0.466], [0.213, 0.712], [0.272, 1], [0, 1]], "sensitivity": 50, "objectTypes": ["vehicle", "animal", "face"], "isTriggerLightEnabled": false, "source": "unifi-protect", "triggerAccessTypes": [], "enableAccessLPOnlyMode": false, "mergeId": "1794971052289"}],
 "smartDetectLines": [],
 "smartDetectLoiterZones": [
  {"id": 8, "name": " Front Yard", "color": "#586CED", "points": [[0.615, 0.212], [0.87, 0.333], [0.796, 0.36], [0.712, 0.397], [0.333, 0.567]], "sensitivity": 50, "isTriggerLightEnabled": true, "triggerAccessTypes": [], "mergeId": "1794971031812", "loiterTriggers": [{"loiterTriggerTime": 10000, "objectType": "person"}, {"loiterTriggerTime": 10000, "objectType": "vehicle"}]},
  {"id": 9, "name": "Street", "color": "#586CED", "points": [[0, 0], [0.832, 0], [0.814, 0.242], [0.589, 0.23], [0.4, 0.466], [0.213, 0.712], [0.272, 1], [0, 1]], "sensitivity": 50, "isTriggerLightEnabled": false, "triggerAccessTypes": [], "mergeId": "1794971052289", "loiterTriggers": [{"loiterTriggerTime": 10000, "objectType": "person"}, {"loiterTriggerTime": 10000, "objectType": "vehicle"}]}]}
"""
)
ZONES, LINES, LOITER = "smartDetectZones", "smartDetectLines", "smartDetectLoiterZones"
TRIANGLE = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]


def text(value):
    """Exactly what would be sent."""
    return json.dumps(value)


def by_id(zones_list, zone_id):
    return next(z for z in zones_list if z["id"] == zone_id)


class EditZone(unittest.TestCase):
    """zone_change: the only thing that decides what is written to Protect."""

    def setUp(self):
        self.camera = copy.deepcopy(REAL_CAMERA)

    def change(self, kind, zone_id, **fields):
        result = zones.zone_change(self.camera, kind, zone_id, **fields)
        self.assertEqual(self.camera, REAL_CAMERA, "the camera as read must not be modified")
        self.assertEqual(text(self.camera), text(REAL_CAMERA))
        return result

    def assert_only_changed(self, body, changed: dict):
        """changed: {(list key, zone id): {field: new value}}; all else must be exactly as read."""
        for key, new_list in body.items():
            self.assertEqual([z["id"] for z in new_list], [z["id"] for z in REAL_CAMERA[key]], "same zones, same order")
            for new, old in zip(new_list, REAL_CAMERA[key]):
                expected = {**old, **changed.get((key, old["id"]), {})}
                self.assertEqual(text(new), text(expected))  # same fields, same order, same text
                self.assertEqual(list(new), list(old))
        for key, zone_id in changed:
            self.assertIn(key, body)

    def test_points_of_a_zone_on_its_own(self):
        body, snapshot = self.change("zone", 3, points=[[0.1, 0.2], [1.0, 0.25], [1, 1], [0.0, 0.123456]])
        self.assertEqual(list(body), [ZONES])  # only the list it is in is sent
        raw = [[0.1, 0.2], [1, 0.25], [1, 1], [0, 0.123]]
        self.assert_only_changed(body, {(ZONES, 3): {"points": raw}})
        self.assertEqual(text(by_id(body[ZONES], 3)["points"]), "[[0.1, 0.2], [1, 0.25], [1, 1], [0, 0.123]]")
        self.assertEqual(snapshot, {"changed": [["zone", 3]], "lists": {ZONES: REAL_CAMERA[ZONES]}})
        self.assertEqual(text(snapshot["lists"][ZONES]), text(REAL_CAMERA[ZONES]))

    def test_a_merged_zone_changes_with_its_other_half(self):
        body, snapshot = self.change("zone", 4, points=TRIANGLE)
        self.assertEqual(sorted(body), sorted([ZONES, LOITER]))
        self.assert_only_changed(body, {(ZONES, 4): {"points": TRIANGLE}, (LOITER, 8): {"points": TRIANGLE}})
        self.assertEqual(snapshot["changed"], [["zone", 4], ["loiter", 8]])
        self.assertEqual(snapshot["lists"], {ZONES: REAL_CAMERA[ZONES], LOITER: REAL_CAMERA[LOITER]})
        # the loiter half keeps what only it has
        self.assertEqual(by_id(body[LOITER], 8)["loiterTriggers"], by_id(REAL_CAMERA[LOITER], 8)["loiterTriggers"])
        self.assertNotIn("objectTypes", by_id(body[LOITER], 8))

    def test_and_the_other_way_round(self):
        body, snapshot = self.change("loiter", 9, color="#00ff7f")
        self.assert_only_changed(body, {(ZONES, 5): {"color": "#00FF7F"}, (LOITER, 9): {"color": "#00FF7F"}})
        self.assertEqual(snapshot["changed"], [["loiter", 9], ["zone", 5]])

    def test_rename(self):
        body, _ = self.change("zone", 4, name="  Driveway ")
        self.assert_only_changed(body, {(ZONES, 4): {"name": "Driveway"}, (LOITER, 8): {"name": "Driveway"}})

    def test_all_three_at_once(self):
        body, _ = self.change("zone", 3, points=TRIANGLE, name="Door", color="#123abc")
        self.assert_only_changed(body, {(ZONES, 3): {"points": TRIANGLE, "name": "Door", "color": "#123ABC"}})

    def test_unknown_fields_are_kept(self):
        self.camera[ZONES][0]["somethingNew"] = {"nested": [1, 2.5, None, "x"]}
        self.camera[ZONES][1]["alsoNew"] = 7
        expected = copy.deepcopy(self.camera)
        body, _ = zones.zone_change(self.camera, "zone", 3, points=TRIANGLE)
        expected[ZONES][0]["points"] = TRIANGLE
        self.assertEqual(text(body), text({ZONES: expected[ZONES]}))

    def test_what_the_overlay_shows_is_no_change(self):
        """The card sends back the points it was given (floats): nothing must be written."""
        shown = zones.overlay_payload(zones.parse_cameras([REAL_CAMERA]), lambda *a: (False, []), True)
        for shape in shown["cameras"]["cam1"]["zones"]:
            self.assertIsNone(
                self.change(shape["kind"], shape["id"], points=shape["points"], name=shape["name"], color=shape["color"]),
                shape,
            )

    def test_same_name_bar_the_spaces_protect_keeps_is_no_change(self):
        self.assertIsNone(self.change("zone", 4, name="Front Yard"))
        body, _ = self.change("zone", 4, name="Front Yard", color="#000000")
        self.assertEqual(by_id(body[ZONES], 4)["name"], " Front Yard")  # still exactly as it was

    def test_points_are_stored_as_protect_does(self):
        self.assertEqual(text(zones.raw_points("zone", [[0, 0.0], [1.0, 1], [0.33349, 0.6668]])), "[[0, 0], [1, 1], [0.333, 0.667]]")
        self.assertEqual(zones.raw_points("line", [[0.2, 0.5], [0.8, 0.5]]), [[0.2, 0.5], [0.8, 0.5]])

    def test_points_that_are_refused(self):
        for kind, points in (
            ("zone", [[0.1, 0.1], [0.9, 0.1]]),  # too few
            ("loiter", [[0.1, 0.1], [0.9, 0.1]]),
            ("line", [[0.1, 0.1]]),
            ("zone", [[0.1, 0.1], [0.1, 0.1], [0.9, 0.9]]),  # too few different ones
            ("zone", [[0.1, 0.1], [0.1001, 0.1], [0.9, 0.9]]),  # ... once rounded
            ("zone", [[0.1, 0.1], [0.9, 0.1], [0.5, 1.01]]),  # outside the picture
            ("zone", [[-0.01, 0.1], [0.9, 0.1], [0.5, 0.5]]),
            ("zone", [[0.1, 0.1], [0.9, 0.1], [0.5, float("nan")]]),
            ("zone", [[0.1, 0.1], [0.9, 0.1], [0.5, "0.5"]]),
            ("zone", [[0.1, 0.1], [0.9, 0.1], [0.5, True]]),
            ("zone", [[0.1, 0.1], [0.9, 0.1], [0.5]]),
            ("zone", [[0.1, 0.1], [0.9, 0.1], 0.5]),
            ("zone", "0.1,0.1"),
            ("zone", []),
            ("zone", [[n / 100, 0.5] for n in range(zones.MAX_POINTS + 1)]),
        ):
            with self.assertRaises(zones.ZoneEditError, msg=points) as raised:
                self.change(kind, 3 if kind == "zone" else 8, points=points)
            self.assertEqual(raised.exception.code, "invalid_points")

    def test_points_must_suit_the_other_half_too(self):
        self.camera[LINES] = [{"id": 1, "name": "Gate", "points": [[0.1, 0.5], [0.9, 0.5]], "mergeId": "1794971031812"}]
        with self.assertRaises(zones.ZoneEditError):
            zones.zone_change(self.camera, "line", 1, points=[[0.2, 0.5], [0.8, 0.5]])  # two points: no zone

    def test_other_things_that_are_refused(self):
        for code, kind, zone_id, fields in (
            ("invalid_name", "zone", 3, {"name": "   "}),
            ("invalid_name", "zone", 3, {"name": "x" * (zones.MAX_NAME + 1)}),
            ("invalid_name", "zone", 3, {"name": 5}),
            ("invalid_color", "zone", 3, {"color": "red"}),
            ("invalid_color", "zone", 3, {"color": "#fff"}),
            ("invalid_color", "zone", 3, {"color": "586CED"}),
            ("nothing_to_change", "zone", 3, {}),
            ("invalid_kind", "area", 3, {"name": "x"}),
            ("zone_not_found", "zone", 99, {"name": "x"}),
            ("zone_not_found", "zone", 8, {"name": "x"}),  # 8 is a loiter zone
            ("zone_not_found", "line", 3, {"name": "x"}),
            ("zone_not_found", "zone", True, {"name": "x"}),
            ("zone_not_found", "zone", "3", {"name": "x"}),
        ):
            with self.assertRaises(zones.ZoneEditError, msg=(kind, zone_id, fields)) as raised:
                self.change(kind, zone_id, **fields)
            self.assertEqual(raised.exception.code, code)

    def test_an_answer_without_the_list_is_refused(self):
        del self.camera[ZONES]
        with self.assertRaises(zones.ZoneEditError):
            zones.zone_change(self.camera, "zone", 3, name="x")
        with self.assertRaises(zones.ZoneEditError):
            zones.zone_change(None, "zone", 3, name="x")


class UndoZone(unittest.TestCase):
    def after(self, body):
        return {**copy.deepcopy(REAL_CAMERA), **copy.deepcopy(body)}

    def test_puts_back_exactly_what_was_there(self):
        for kind, zone_id, fields in (
            ("zone", 3, {"points": TRIANGLE}),
            ("zone", 4, {"points": TRIANGLE, "name": "Driveway", "color": "#000000"}),
            ("loiter", 9, {"name": "Road"}),
        ):
            body, snapshot = zones.zone_change(REAL_CAMERA, kind, zone_id, **fields)
            snapshot = json.loads(json.dumps(snapshot))  # as it comes back from disk
            changed = self.after(body)
            back = zones.zone_undo(changed, snapshot)
            self.assertEqual(sorted(back), sorted(body))
            self.assertEqual(text(self.after(back)), text(REAL_CAMERA))
            self.assertIsNone(zones.zone_undo(self.after(back), snapshot))  # nothing left to put back

    def test_keeps_what_was_done_since(self):
        body, snapshot = zones.zone_change(REAL_CAMERA, "zone", 3, points=TRIANGLE)
        now = self.after(body)
        now[ZONES].append({"id": 6, "name": "New in the Protect app", "points": TRIANGLE})
        by_id(now[ZONES], 5)["sensitivity"] = 80
        back = zones.zone_undo(now, snapshot)
        self.assertEqual(list(back), [ZONES])
        self.assertEqual(text(by_id(back[ZONES], 3)), text(by_id(REAL_CAMERA[ZONES], 3)))
        self.assertEqual(by_id(back[ZONES], 5)["sensitivity"], 80)
        self.assertEqual([z["id"] for z in back[ZONES]], [3, 4, 5, 6])

    def test_a_zone_deleted_since_cannot_be_put_back(self):
        body, snapshot = zones.zone_change(REAL_CAMERA, "zone", 4, points=TRIANGLE)
        now = self.after(body)
        now[LOITER] = [z for z in now[LOITER] if z["id"] != 8]
        with self.assertRaises(zones.ZoneEditError) as raised:
            zones.zone_undo(now, snapshot)
        self.assertEqual(raised.exception.code, "zone_not_found")

    def test_unreadable_history(self):
        for snapshot in ({"changed": [["zone", 3]], "lists": {}}, {"changed": [["area", 3]], "lists": {}}):
            with self.assertRaises(zones.ZoneEditError) as raised:
                zones.zone_undo(REAL_CAMERA, snapshot)
            self.assertEqual(raised.exception.code, "bad_history")

    def test_history_keeps_the_last_ten(self):
        history = []
        for n in range(zones.KEEP_HISTORY + 3):
            history = zones.remember(history, {"n": n})
        self.assertEqual([h["n"] for h in history], list(range(3, zones.KEEP_HISTORY + 3)))


class ZoneKey(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(zones.zone_key(f"zone_{MAC}_loiter_12"), (MAC, "loiter", 12))

    def test_tail(self):
        self.assertEqual(zones.zone_key(f"zone_{MAC}_zone_3_occupied", "_occupied"), (MAC, "zone", 3))

    def test_one_is_not_taken_for_the_other(self):
        self.assertIsNone(zones.zone_key(f"zone_{MAC}_zone_3_occupied"))
        self.assertIsNone(zones.zone_key(f"zone_{MAC}_zone_3", "_occupied"))

    def test_other_ids(self):
        for uid in ("", "rule_abc", f"zone_{MAC}_zone_x", f"zones_{MAC}_zone_1"):
            self.assertIsNone(zones.zone_key(uid))


class EditTypesAndDwell(unittest.TestCase):
    """Changing what a zone detects, on the camera as Protect sent it."""

    def test_object_types_of_a_detection_zone(self):
        body, snapshot = zones.zone_change(REAL_CAMERA, "zone", 4, types=["animal", "face", "person"])
        self.assertEqual(list(body), ["smartDetectZones"])
        was = {z["id"]: z for z in REAL_CAMERA["smartDetectZones"]}
        for zone in body["smartDetectZones"]:
            expected = dict(was[zone["id"]])
            if zone["id"] == 4:
                expected["objectTypes"] = ["animal", "face", "person"]
            self.assertEqual(zone, expected)
        self.assertEqual(snapshot["changed"], [["zone", 4]])  # not the loiter zone merged with it
        self.assertEqual(snapshot["lists"], {"smartDetectZones": REAL_CAMERA["smartDetectZones"]})

    def test_same_types_is_no_change(self):
        same = list(next(z for z in REAL_CAMERA["smartDetectZones"] if z["id"] == 4)["objectTypes"])
        self.assertIsNone(zones.zone_change(REAL_CAMERA, "zone", 4, types=same))

    def test_bad_types(self):
        for kind, zone_id, types in (
            ("zone", 4, []),
            ("zone", 4, "person"),
            ("zone", 4, ["person", "ufo"]),
            ("zone", 4, ["person", 5]),
            ("loiter", 8, ["person"]),  # a loiter zone has dwell times, not object types
        ):
            with self.assertRaises(zones.ZoneEditError, msg=(kind, zone_id, types)) as raised:
                zones.zone_change(REAL_CAMERA, kind, zone_id, types=types)
            self.assertEqual(raised.exception.code, "invalid_types")

    def test_dwell_of_a_loiter_zone(self):
        body, snapshot = zones.zone_change(REAL_CAMERA, "loiter", 9, dwell={"person": 3, "vehicle": 10})
        self.assertEqual(list(body), ["smartDetectLoiterZones"])
        was = {z["id"]: z for z in REAL_CAMERA["smartDetectLoiterZones"]}
        for zone in body["smartDetectLoiterZones"]:
            expected = dict(was[zone["id"]])
            if zone["id"] == 9:
                expected["loiterTriggers"] = [
                    {"loiterTriggerTime": 3000, "objectType": "person"},
                    {"loiterTriggerTime": 10000, "objectType": "vehicle"},
                ]
            self.assertEqual(zone, expected)
        self.assertEqual(text(body["smartDetectLoiterZones"][1]["loiterTriggers"][0]), '{"loiterTriggerTime": 3000, "objectType": "person"}')
        self.assertEqual(snapshot["changed"], [["loiter", 9]])

    def test_dwell_replaces_the_set(self):
        body, _ = zones.zone_change(REAL_CAMERA, "loiter", 9, dwell={"person": 2.5})
        zone = next(z for z in body["smartDetectLoiterZones"] if z["id"] == 9)
        self.assertEqual(zone["loiterTriggers"], [{"loiterTriggerTime": 2500, "objectType": "person"}])

    def test_bad_dwell(self):
        for kind, zone_id, dwell in (
            ("loiter", 9, {}),
            ("loiter", 9, {"person": 0}),
            ("loiter", 9, {"person": 0.1}),
            ("loiter", 9, {"person": 601}),
            ("loiter", 9, {"person": True}),
            ("loiter", 9, {"person": "5"}),
            ("loiter", 9, {"ufo": 5}),
            ("loiter", 9, [5]),
            ("zone", 4, {"person": 5}),  # a detection zone has no dwell time
        ):
            with self.assertRaises(zones.ZoneEditError, msg=(kind, zone_id, dwell)) as raised:
                zones.zone_change(REAL_CAMERA, kind, zone_id, dwell=dwell)
            self.assertEqual(raised.exception.code, "invalid_dwell")

    def test_undo_puts_the_dwell_back(self):
        body, snapshot = zones.zone_change(REAL_CAMERA, "loiter", 9, dwell={"person": 3})
        changed = {**REAL_CAMERA, **body}
        back = zones.zone_undo(changed, snapshot)
        self.assertEqual(back, {"smartDetectLoiterZones": REAL_CAMERA["smartDetectLoiterZones"]})

    def test_with_a_new_outline_only_the_outline_goes_to_the_merged_zone(self):
        points = [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]]
        body, snapshot = zones.zone_change(REAL_CAMERA, "zone", 4, points=points, types=["person"])
        self.assertEqual(snapshot["changed"], [["zone", 4], ["loiter", 8]])
        loiter = next(z for z in body["smartDetectLoiterZones"] if z["id"] == 8)
        was = next(z for z in REAL_CAMERA["smartDetectLoiterZones"] if z["id"] == 8)
        self.assertEqual(loiter, {**was, "points": points})

    def test_catalogue_and_overlay_show_them(self):
        got = zones.parse_cameras([REAL_CAMERA])[REAL_CAMERA["mac"]]
        self.assertEqual(got["loiter"][1]["dwell"], {"person": 10.0, "vehicle": 10.0})
        self.assertIsNone(got["zone"][0]["dwell"])
        shapes = zones.overlay_payload({REAL_CAMERA["mac"]: got}, lambda mac, zid: (False, []), True)
        by = {(z["kind"], z["id"]): z for z in shapes["cameras"]["cam1"]["zones"]}
        self.assertEqual(by["loiter", 9]["dwell"], {"person": 10.0, "vehicle": 10.0})
        self.assertEqual(by["loiter", 9]["types"], ["person", "vehicle"])
        self.assertEqual(by["zone", 4]["types"], next(z for z in REAL_CAMERA["smartDetectZones"] if z["id"] == 4)["objectTypes"])


class ZoneObjectTypes(unittest.TestCase):
    """What a zone is set to detect, in the shapes Protect sends (21 Nov diagnostics)."""

    def test_detection_zone(self):
        zone = {"id": 4, "objectTypes": ["animal", "face", "person", "vehicle"], "loiterTriggers": None}
        self.assertEqual(zones.parse_types(zone), ("animal", "face", "person", "vehicle"))

    def test_loiter_zone(self):
        zone = {
            "id": 8,
            "loiterTriggers": [
                {"loiterTriggerTime": 10000, "objectType": "person"},
                {"loiterTriggerTime": 10000, "objectType": "vehicle"},
                {"loiterTriggerTime": 5000, "objectType": "vehicle"},
                "junk",
            ],
        }
        self.assertEqual(zones.parse_types(zone), ("person", "vehicle"))

    def test_not_said(self):
        for zone in ({}, {"objectTypes": []}, {"objectTypes": "person"}, {"objectTypes": [1, None, ""]}):
            self.assertIsNone(zones.parse_types(zone), zone)

    def test_in_the_catalogue(self):
        got = zones.parse_cameras([{"mac": MAC, "smartDetectZones": [{"id": 3, "objectTypes": ["person"]}]}])
        self.assertEqual(got[MAC]["zone"][0]["types"], ("person",))

    def test_zone_detects(self):
        types = ("animal", "face", "person")
        self.assertTrue(zones.zone_detects(types, "person"))
        self.assertTrue(zones.zone_detects(types, "face"))
        self.assertFalse(zones.zone_detects(types, "vehicle"))
        self.assertFalse(zones.zone_detects(types, "licensePlate"))  # a plate is on a vehicle
        self.assertTrue(zones.zone_detects(("vehicle",), "licensePlate"))
        self.assertTrue(zones.zone_detects(("face", "vehicle"), "face"))  # listed itself, though no person
        self.assertTrue(zones.zone_detects(types, "ufo"))  # a type we don't know
        self.assertTrue(zones.zone_detects(None, "vehicle"))  # a zone that doesn't say


class TrackerZoneTypes(unittest.TestCase):
    """Zone 1 detects people only, zone 2 everything (a zone for the whole yard)."""

    def setUp(self):
        self.hits = []
        self.types = {("zone", 1): ("animal", "face", "person"), ("zone", 2): ("animal", "face", "person", "vehicle")}
        self.tracker = zones.ZoneTracker(
            {"cam1": (MAC, "Front")}.get,
            lambda mac, kind, zid, event_type, attrs: self.hits.append((kind, zid, event_type)),
            lambda mac, zid, occupied, seen: None,
            clock=lambda: 1000.0,
            zone_types=lambda mac, kind, zid: self.types.get((kind, zid)) if mac == MAC else None,
        )

    def update(self, types, z1, z2, **more):
        status = {"1": {"level": 37, "status": z1}, "2": {"level": 52, "status": z2}}
        data = {"type": "smartDetectZone", "camera": "cam1", "smartDetectTypes": types, **more}
        self.tracker.detection("e1", data | {"metadata": {"zonesStatus": status}})

    def test_everything_seen_counts_for_every_zone_that_detects_it(self):
        """Seen live (21 Nov 17:54): a person in both zones and a passing car, in one detection."""
        self.update(["person"], "none", "enter")
        self.update(["person"], "enter", "moving")
        self.update(["person", "vehicle"], "moving", "moving")
        self.update(["face", "person", "vehicle"], "moving", "moving")
        self.assertEqual(
            self.hits,
            [("zone", 2, "person"), ("zone", 1, "person"), ("zone", 2, "vehicle"), ("zone", 2, "face"), ("zone", 1, "face")],
        )
        self.assertEqual(self.tracker.occupancy(MAC, 1), (True, ["face", "person"]))  # it detects no vehicles
        self.assertEqual(self.tracker.occupancy(MAC, 2), (True, ["face", "person", "vehicle"]))
        self.update(["face", "person", "vehicle"], "leave", "moving")
        self.assertEqual(self.tracker.occupancy(MAC, 1), (False, []))
        self.assertEqual(self.tracker.occupancy(MAC, 2), (True, ["face", "person", "vehicle"]))

    def test_thumbnails_change_nothing(self):
        self.update(["person", "vehicle"], "enter", "enter")
        thumb = {"type": "vehicle", "labels": ["smartDetectType:vehicle"], "attributes": {"zone": [1, 2]}}
        self.tracker.detection("e1", {"metadata": {"detectedThumbnails": [thumb]}})
        self.assertEqual(self.hits, [("zone", 1, "person"), ("zone", 2, "person"), ("zone", 2, "vehicle")])

    def test_settings_changed_since_we_read_them(self):
        """Protect says a vehicle-only detection is in the zone: it fires, whatever our copy says."""
        self.update(["vehicle"], "enter", "none")
        self.assertEqual(self.hits, [("zone", 1, "vehicle")])
        self.assertEqual(self.tracker.occupancy(MAC, 1), (True, ["vehicle"]))

    def test_a_zone_that_does_not_say_is_not_filtered(self):
        del self.types[("zone", 1)]
        self.update(["person", "vehicle"], "enter", "enter")
        self.assertIn(("zone", 1, "vehicle"), self.hits)


class Tracker(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.hits = []
        self.occupancy = []
        self.cameras = {"cam1": (MAC, "Front")}
        self.tracker = zones.ZoneTracker(
            self.cameras.get,
            lambda mac, kind, zid, event_type, attrs: self.hits.append((mac, kind, zid, event_type, attrs)),
            lambda mac, zid, occupied, seen: self.occupancy.append((mac, zid, occupied, seen)),
            clock=lambda: self.now,
        )

    def fired(self):
        return [(kind, zid, event_type) for _, kind, zid, event_type, _ in self.hits]

    def add(self, event_id="e1", **data):
        self.tracker.detection(event_id, {"type": "smartDetectZone", "camera": "cam1", **data})

    def test_zone_hit_with_type(self):
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"level": 72, "status": "enter"}}})
        self.assertEqual(
            self.hits,
            [(MAC, "zone", 1, "person", {"camera": "Front", "detection_event_id": "e1", "confidence": 72})],
        )
        self.assertEqual(self.occupancy, [(MAC, 1, True, ["person"])])

    def test_fires_once_per_detection_zone_and_type(self):
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"level": 72, "status": "enter"}}})
        self.tracker.detection("e1", {"metadata": {"zonesStatus": {"1": {"level": 80, "status": "moving"}}}})
        self.tracker.detection("e1", {"smartDetectTypes": ["person", "licensePlate"]})
        self.assertEqual(self.fired(), [("zone", 1, "person"), ("zone", 1, "license_plate")])

    def test_unknown_types_fire_detected_once(self):
        self.add(smartDetectTypes=["ufo", "ghost"], metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.assertEqual(self.fired(), [("zone", 1, "detected")])

    def test_leave_and_end_empty_the_zone(self):
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"status": "enter"}, "2": {"status": "enter"}}})
        self.tracker.detection("e1", {"metadata": {"zonesStatus": {"1": {"status": "leave"}}}})
        self.assertEqual(self.occupancy[-1], (MAC, 1, False, []))
        self.tracker.detection("e1", {"end": 123})
        self.assertEqual(self.occupancy[-1], (MAC, 2, False, []))
        self.assertEqual(len(self.occupancy), 4)

    def test_entering_a_second_zone_during_a_detection(self):
        """One object walking from zone 2 into zone 1: each zone fires once, and 2 empties when it leaves."""
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"2": {"level": 60, "status": "enter"}}})
        self.tracker.detection("e1", {"metadata": {"zonesStatus": {"2": {"level": 65, "status": "moving"}}}})
        self.tracker.detection(
            "e1",
            {"metadata": {"zonesStatus": {"1": {"level": 74, "status": "enter"}, "2": {"level": 65, "status": "moving"}}}},
        )
        self.assertEqual(self.tracker.occupancy(MAC, 1), (True, ["person"]))
        self.assertEqual(self.tracker.occupancy(MAC, 2), (True, ["person"]))  # overlapping: in both
        self.tracker.detection(
            "e1",
            {"metadata": {"zonesStatus": {"1": {"level": 80, "status": "moving"}, "2": {"level": 0, "status": "leave"}}}},
        )
        self.assertEqual(self.fired(), [("zone", 2, "person"), ("zone", 1, "person")])
        self.assertEqual(self.hits[1][4]["confidence"], 74)
        self.assertEqual(
            self.occupancy,
            [(MAC, 2, True, ["person"]), (MAC, 1, True, ["person"]), (MAC, 2, False, [])],
        )
        self.assertEqual(self.tracker.occupancy(MAC, 2), (False, []))

    def test_second_zone_named_alone_keeps_the_first(self):
        """An update that only names the new zone says nothing about the old one."""
        self.add(smartDetectTypes=["vehicle"], metadata={"zonesStatus": {"2": {"status": "enter"}}})
        self.tracker.detection("e1", {"metadata": {"zonesStatus": {"1": {"status": "enter"}}}})
        self.assertEqual(self.fired(), [("zone", 2, "vehicle"), ("zone", 1, "vehicle")])
        self.assertEqual(self.tracker.occupancy(MAC, 2), (True, ["vehicle"]))
        self.tracker.detection("e1", {"metadata": {"zonesStatus": {"2": {"status": "none"}}}})
        self.assertEqual(self.tracker.occupancy(MAC, 2), (False, []))
        self.assertEqual(self.tracker.occupancy(MAC, 1), (True, ["vehicle"]))

    def test_what_it_is_arriving_later_shows_in_the_occupied_zone(self):
        """Seen live: "face" came with the zone, "person" a moment later in an update naming no zone."""
        self.add(smartDetectTypes=["face"], metadata={"zonesStatus": {"1": {"level": 59, "status": "enter"}}})
        self.tracker.detection("e1", {"smartDetectTypes": ["face", "person"]})
        self.assertEqual(self.occupancy, [(MAC, 1, True, ["face"]), (MAC, 1, True, ["face", "person"])])
        self.tracker.detection("e1", {"smartDetectTypes": ["face", "person"]})  # nothing new: nothing sent
        self.assertEqual(len(self.occupancy), 2)
        self.tracker.detection("e1", {"end": 5})
        self.assertEqual(self.occupancy[-1], (MAC, 1, False, []))

    def test_a_late_type_does_not_keep_a_silent_detection_alive(self):
        self.add(smartDetectTypes=["face"], metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.now += zones.OCCUPANCY_STALE - 1
        self.tracker.detection("e1", {"smartDetectTypes": ["face", "person"]})
        self.now += 2
        self.tracker.sweep()
        self.assertEqual(self.tracker.occupancy(MAC, 1), (False, []))

    def test_a_single_thing_is_in_every_zone_of_its_detection(self):
        self.add(smartDetectTypes=["vehicle"], metadata={"zonesStatus": {"2": {"status": "enter"}}})
        self.tracker.detection("e1", {"smartDetectTypes": ["vehicle", "licensePlate"]})
        self.tracker.detection("e1", {"metadata": {"zonesStatus": {"1": {"status": "enter"}}}})
        self.assertEqual(
            self.fired(),
            [("zone", 2, "vehicle"), ("zone", 2, "license_plate"), ("zone", 1, "vehicle"), ("zone", 1, "license_plate")],
        )

    def test_leave_for_a_zone_it_never_entered_is_no_hit(self):
        """Seen live (21 Nov 18:36): a car in zone 2 only; the last update says "leave" for every zone."""
        status = {"1": {"level": 0, "status": "none"}, "4294967295": {"level": 0, "status": "none"}}
        self.add(smartDetectTypes=["vehicle"], metadata={"zonesStatus": status | {"2": {"level": 64, "status": "enter"}}})
        left = {"1": {"level": 0, "status": "leave"}, "4294967295": {"level": 0, "status": "leave"}}
        self.tracker.detection(
            "e1",
            {
                "type": "smartDetectZone",
                "smartDetectTypes": ["vehicle"],
                "end": 9,
                "metadata": {"zonesStatus": left | {"2": {"level": 64, "status": "leave"}}},
            },
        )
        self.tracker.detection("e1", {"labels": ["smartDetectType:vehicle", "zone:cam1:2"]})
        self.assertEqual(self.fired(), [("zone", 2, "vehicle")])
        self.assertEqual(self.occupancy, [(MAC, 2, True, ["vehicle"]), (MAC, 2, False, [])])

    def test_two_detections_in_one_zone(self):
        status = {"metadata": {"zonesStatus": {"1": {"status": "enter"}}}}
        self.add("e1", smartDetectTypes=["person"], **status)
        self.add("e2", smartDetectTypes=["vehicle"], **status)
        self.tracker.detection("e1", {"end": 1})
        self.assertEqual(
            self.occupancy,
            [(MAC, 1, True, ["person"]), (MAC, 1, True, ["person", "vehicle"]), (MAC, 1, True, ["vehicle"])],
        )

    def test_status_none_is_no_hit(self):
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"status": "none"}}})
        self.assertEqual((self.hits, self.occupancy), ([], []))

    def test_plain_status_values(self):
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"1": "enter"}})
        self.assertEqual(self.fired(), [("zone", 1, "person")])
        self.assertIsNone(self.hits[0][4]["confidence"])

    def test_line_and_loiter(self):
        self.add(
            type="smartDetectLine", smartDetectTypes=["vehicle"], metadata={"linesStatus": {"4": {"status": "AtoB"}}}
        )
        self.add(
            "e2",
            type="smartDetectLoiterZone",
            smartDetectTypes=["person"],
            metadata={"loiterStatus": {"2": {"level": 80, "status": "loiter"}}},
        )
        self.assertEqual(self.fired(), [("line", 4, "vehicle"), ("loiter", 2, "person")])
        self.assertEqual(self.occupancy, [])  # only detection zones have occupancy

    def test_labels(self):
        self.add(
            labels=["smartDetectType:person", "zone:cam1:5"],
            metadata={"detectedThumbnails": [{"labels": ["line:cam1:2", "zone:cam1:x"]}]},
        )
        self.assertEqual(self.fired(), [("zone", 5, "person"), ("line", 2, "person")])

    def test_a_bad_zone_id_does_not_lose_the_rest(self):
        self.add(
            smartDetectTypes=["person"],
            metadata={"zonesStatus": {"oops": {"status": "enter"}, "2": {"status": "enter"}, "": "enter"}},
        )
        self.assertEqual(self.fired(), [("zone", 2, "person")])

    def test_odd_shapes_do_not_raise(self):
        self.add(smartDetectTypes="person", metadata="x", labels="zone:cam1:1")
        self.add("e2", smartDetectTypes=[None, 3], metadata={"detectedThumbnails": [None, {"labels": None}]})
        self.tracker.detection("e3", None)
        self.tracker.detection(None, {"type": "smartDetectZone"})
        self.assertEqual(self.hits, [])

    def test_other_events_are_ignored(self):
        self.tracker.detection("m1", {"type": "motion", "camera": "cam1", "metadata": {"zonesStatus": {"1": "enter"}}})
        self.tracker.detection("x1", {"metadata": {"zonesStatus": {"1": "enter"}}})  # an update of one never seen
        self.assertEqual((self.hits, self.occupancy), ([], []))

    def test_unknown_camera(self):
        self.add(camera="other", smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.assertEqual((self.hits, self.occupancy), ([], []))
        self.cameras["other"] = ("112233445566", "Back")  # the zone list caught up
        self.tracker.detection("e1", {"metadata": {"zonesStatus": {"1": {"status": "moving"}}}})
        self.assertEqual(self.fired(), [("zone", 1, "person")])

    # -- a hit that arrives before the object type: one event, not two

    def test_untyped_hit_waits_for_the_type(self):
        self.add(metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.assertEqual(self.hits, [])
        self.assertEqual(self.occupancy, [(MAC, 1, True, [])])  # occupancy doesn't wait
        self.now += 1
        self.tracker.sweep()
        self.assertEqual(self.hits, [])
        self.tracker.detection("e1", {"smartDetectTypes": ["person"]})
        self.assertEqual(self.fired(), [("zone", 1, "person")])
        self.now += 60
        self.tracker.sweep()
        self.assertEqual(self.fired(), [("zone", 1, "person")])

    def test_untyped_hit_fires_detected_after_the_grace(self):
        self.add(metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.now += zones.UNTYPED_GRACE
        self.tracker.sweep()
        self.tracker.sweep()
        self.assertEqual(self.fired(), [("zone", 1, "detected")])

    def test_untyped_hit_fires_detected_when_the_detection_ends(self):
        self.add(metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.tracker.detection("e1", {"end": 5})
        self.assertEqual(self.fired(), [("zone", 1, "detected")])

    def test_a_type_that_comes_after_the_grace_still_fires(self):
        self.add(metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.now += zones.UNTYPED_GRACE
        self.tracker.sweep()
        self.tracker.detection("e1", {"smartDetectTypes": ["person"]})
        self.assertEqual(self.fired(), [("zone", 1, "detected"), ("zone", 1, "person")])

    # -- occupancy housekeeping

    def test_silent_detection_goes_stale(self):
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.now += zones.OCCUPANCY_STALE - 1
        self.tracker.sweep()
        self.assertEqual(len(self.occupancy), 1)
        self.now += 2
        self.tracker.sweep()
        self.assertEqual(self.occupancy[-1], (MAC, 1, False, []))

    def test_an_update_keeps_it_fresh(self):
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.now += zones.OCCUPANCY_STALE - 1
        self.tracker.detection("e1", {"metadata": {"zonesStatus": {"1": {"status": "moving"}}}})
        self.now += 2
        self.tracker.sweep()
        self.assertEqual(self.occupancy, [(MAC, 1, True, ["person"])])

    def test_clear_occupancy(self):
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"status": "enter"}, "2": {"status": "enter"}}})
        self.tracker.clear_occupancy()
        self.assertEqual(self.occupancy[2:], [(MAC, 1, False, []), (MAC, 2, False, [])])
        self.tracker.clear_occupancy()
        self.assertEqual(len(self.occupancy), 4)

    def test_clear_occupancy_of_one_camera(self):
        self.cameras["cam2"] = (OTHER, "Back")
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.add("e2", camera="cam2", smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"status": "enter"}}})
        del self.occupancy[:]
        self.tracker.clear_occupancy(OTHER)  # it went offline, or was removed
        self.assertEqual(self.occupancy, [(OTHER, 1, False, [])])
        self.assertEqual(self.tracker.occupancy(MAC, 1), (True, ["person"]))

    def test_a_detection_of_a_removed_camera_is_ignored(self):
        self.add(smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"status": "enter"}}})
        del self.cameras["cam1"]  # removed from Protect
        del self.hits[:], self.occupancy[:]
        self.tracker.detection("e1", {"metadata": {"zonesStatus": {"2": {"status": "enter"}}}})
        self.add("e2", smartDetectTypes=["person"], metadata={"zonesStatus": {"1": {"status": "enter"}}})
        self.tracker.sweep()
        self.assertEqual((self.hits, self.occupancy), ([], []))

    def test_remembers_a_limited_number_of_detections(self):
        for n in range(zones.REMEMBER_EVENTS + 1):
            self.add(f"e{n}", smartDetectTypes=["person"])
        self.tracker.detection("e0", {"metadata": {"zonesStatus": {"1": {"status": "enter"}}}})  # forgotten
        self.tracker.detection("e1", {"metadata": {"zonesStatus": {"1": {"status": "enter"}}}})
        self.assertEqual([h[4]["detection_event_id"] for h in self.hits], ["e1"])



OTHER = "112233445566"


def camera(mac, cam_id, **more):
    return {"mac": mac, "id": cam_id, "name": cam_id, "smartDetectZones": [{"id": 1, "name": "Yard"}], "smartDetectLines": [], "smartDetectLoiterZones": [], **more}


class OfflineCamera(unittest.TestCase):
    """Offline: still in Protect's list, with its zones. Everything is kept."""

    def test_connected_unless_protect_says_otherwise(self):
        for more, expected in (
            ({}, True),
            ({"isConnected": True}, True),
            ({"isConnected": False}, False),
            ({"state": "CONNECTED"}, True),
            ({"state": "DISCONNECTED"}, False),
            ({"state": "CONNECTING"}, False),
            ({"isConnected": True, "state": "DISCONNECTED"}, True),
            ({"isConnected": None, "state": 7}, True),
        ):
            got = zones.parse_cameras([camera(MAC, "cam1", **more)])
            self.assertIs(got[MAC]["connected"], expected, more)
            self.assertIs(zones.camera_connected(got, MAC), expected, more)
            self.assertEqual([z["id"] for z in got[MAC]["zone"]], [1], "its zones are still read")

    def test_nothing_of_an_offline_camera_is_removed(self):
        cameras = zones.parse_cameras([camera(MAC, "cam1", isConnected=False)])
        keys = [(MAC, "zone", 1), (MAC,)]
        counts = {}
        for _ in range(5):
            self.assertEqual(zones.gone(keys, keys, cameras, counts), [])
        self.assertEqual(counts, {})

    def test_overlay_shows_it_as_not_connected_and_its_zones_as_unknown(self):
        cameras = zones.parse_cameras([camera(MAC, "cam1", isConnected=False, featureFlags={"isPtz": True})])
        entry = zones.overlay_payload(cameras, lambda mac, zid: (True, ["person"]), True, lambda mac: True)["cameras"]["cam1"]
        self.assertIs(entry["connected"], False)
        self.assertIsNone(entry["aligned"])
        self.assertEqual([(z["occupied"], z["detected"]) for z in entry["zones"]], [(None, None)])


class RemovedCamera(unittest.TestCase):
    """Removed: no longer in Protect's list. Its entities go, after two reads agree."""

    def setUp(self):
        self.both = zones.parse_cameras([camera(MAC, "cam1"), camera(OTHER, "cam2")])
        self.one = zones.parse_cameras([camera(MAC, "cam1")])
        self.registered = [(MAC, "zone", 1), (MAC,), (OTHER, "zone", 1), (OTHER,)]
        self.counts = {}

    def current(self, cameras):
        return {(mac, "zone", z["id"]) for mac, cam in cameras.items() for z in cam["zone"]} | {(mac,) for mac in cameras}

    def gone(self, cameras):
        return zones.gone(self.registered, self.current(cameras), cameras, self.counts)

    def test_not_connected_and_not_in_the_list(self):
        self.assertFalse(zones.camera_connected(self.one, OTHER))
        self.assertFalse(zones.camera_connected(None, OTHER))
        self.assertFalse(zones.camera_connected({}, OTHER))

    def test_removed_after_two_reads_in_a_row(self):
        self.assertEqual(self.gone(self.both), [])
        self.assertEqual(self.gone(self.one), [])
        self.assertEqual(self.gone(self.one), [(OTHER, "zone", 1), (OTHER,)])
        self.assertEqual(self.counts, {})

    def test_one_odd_answer_removes_nothing(self):
        for _ in range(3):
            self.assertEqual(self.gone(self.one), [])
            self.assertEqual(self.gone(self.both), [])
        self.assertEqual(self.counts, {})

    def test_a_camera_protect_no_longer_manages_counts_as_removed(self):
        cameras = zones.parse_cameras([camera(MAC, "cam1"), camera(OTHER, "cam2", isAdopted=False)])
        self.assertEqual(cameras, self.one)

    def test_a_replacement_is_another_camera(self):
        """A new device has another mac and id: nothing of the old one is carried over."""
        new = "AABBCC000001"
        cameras = zones.parse_cameras([camera(MAC, "cam1"), camera(new, "cam3")])
        self.gone(cameras)
        self.assertEqual(self.gone(cameras), [(OTHER, "zone", 1), (OTHER,)])
        self.assertNotIn(OTHER, cameras)
        self.assertEqual(self.current(cameras) - set(self.registered), {(new, "zone", 1), (new,)})

    def test_a_camera_that_does_not_say_keeps_its_entities(self):
        """Its answer left the zones out altogether, rather than listing none."""
        silent = zones.parse_cameras([camera(MAC, "cam1"), {"mac": OTHER, "id": "cam2", "name": "cam2"}])
        current = {(MAC, "zone", 1), (MAC,)}
        for _ in range(3):
            self.assertEqual(zones.gone(self.registered, current, silent, self.counts), [])

    def test_a_zone_deleted_from_a_camera_that_stays(self):
        cameras = zones.parse_cameras([camera(MAC, "cam1"), camera(OTHER, "cam2", smartDetectZones=[])])
        current = {(MAC, "zone", 1), (MAC,), (OTHER,)}
        zones.gone(self.registered, current, cameras, self.counts)
        self.assertEqual(zones.gone(self.registered, current, cameras, self.counts), [(OTHER, "zone", 1)])

    def test_overlay_leaves_it_out(self):
        self.assertEqual(list(zones.overlay_payload(self.one, lambda mac, zid: (False, []), True)["cameras"]), ["cam1"])


if __name__ == "__main__":
    unittest.main()
