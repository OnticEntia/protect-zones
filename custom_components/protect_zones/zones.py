"""What Protect's answers mean, without Home Assistant.

Everything here is plain Python (no homeassistant import, no relative import),
so the tests can load this file on its own:

  - parse_cameras   the zone catalogue from Protect's camera list
  - overlay_payload the zones as shapes to draw over a camera's picture
  - zone_change / zone_undo   what to send Protect to change a zone, or to
                    put it back (the only part that leads to a write)
  - zone_key        (camera mac, kind, zone id) from an entity's unique id
  - ZoneTracker     Protect's live detection updates -> zone hits and occupancy
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable
import copy
import math
import re
import time
from typing import Any

# our zone kind -> the key of a camera's zones of that kind
ZONE_KINDS = (("zone", "smartDetectZones"), ("line", "smartDetectLines"), ("loiter", "smartDetectLoiterZones"))

ZONE_EVENT_TYPES = ("smartDetectZone", "smartDetectLine", "smartDetectLoiterZone")
# event metadata key -> our zone kind
STATUS_KEYS = (("zonesStatus", "zone"), ("linesStatus", "line"), ("loiterStatus", "loiter"))
OBJECT_TYPES = {
    "person": "person",
    "vehicle": "vehicle",
    "animal": "animal",
    "package": "package",
    "licensePlate": "license_plate",
    "face": "face",
}
DETECTED = "detected"  # a zone hit with no (or an unknown) object type
# A face is seen on a person and a plate on a vehicle: they are in the zones
# that the person or vehicle is in.
TYPE_GROUPS = {"face": "person", "licensePlate": "vehicle"}
REMEMBER_EVENTS = 200
# A detection that stops reporting without ever saying it ended (a missed
# packet) stops counting as "in the zone" after this many seconds.
OCCUPANCY_STALE = 600
# A zone hit that arrives before Protect says what was seen waits this long
# for the object type, so one detection doesn't fire "detected" and then
# "person". If no type came by then (or the detection ended), it fires as
# "detected".
UNTYPED_GRACE = 5


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _group(object_type: str) -> str:
    return TYPE_GROUPS.get(object_type, object_type)


# ------------------------------------------------------------- catalogue

_COLOR = re.compile(r"#?([0-9a-fA-F]{6})")


def parse_color(value: Any) -> str | None:
    """A zone's colour as "#rrggbb" (lower case), or None if it has none we understand."""
    if isinstance(value, str) and (match := _COLOR.fullmatch(value.strip())):
        return f"#{match[1].lower()}"
    return None


def parse_points(value: Any) -> list[list[float]]:
    """A zone's outline as [[x, y], ...], each 0..1 from the top left of the
    camera's picture, which is how Protect stores them ([[0.25, 0.4], ...]).

    Anything else (not pairs of numbers, or numbers outside 0..1, which would
    be some other unit we'd have to guess at) gives []: no outline is better
    than a wrong one.
    """
    if not isinstance(value, list):
        return []
    points = []
    for point in value:
        if isinstance(point, dict):
            point = [point.get("x"), point.get("y")]
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return []
        pair = []
        for number in point:
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                return []
            if not -0.001 <= number <= 1.001:
                return []
            pair.append(round(min(max(float(number), 0.0), 1.0), 4))
        points.append(pair)
    return points



def parse_types(zone: dict[str, Any]) -> tuple[str, ...] | None:
    """The object types a zone is set to detect, in Protect's words ("person",
    "licensePlate", ...), or None if it doesn't say.

    A detection zone lists them as objectTypes, a loiter zone as the
    objectType of each of its loiterTriggers.
    """
    types = zone.get("objectTypes")
    if not isinstance(types, list):
        triggers = zone.get("loiterTriggers")
        types = [t.get("objectType") for t in triggers if isinstance(t, dict)] if isinstance(triggers, list) else []
    types = tuple(dict.fromkeys(t for t in types if isinstance(t, str) and t))
    return types or None


def parse_dwell(zone: dict[str, Any]) -> dict[str, float] | None:
    """How long each kind of object must stay in a loiter zone before it
    counts, {object type: seconds}; None for a zone that has no such setting.

    Protect keeps it as loiterTriggers: [{"objectType", "loiterTriggerTime"
    (milliseconds)}].
    """
    triggers = zone.get("loiterTriggers")
    if not isinstance(triggers, list):
        return None
    dwell: dict[str, float] = {}
    for trigger in triggers:
        if not isinstance(trigger, dict) or not isinstance(trigger.get("objectType"), str):
            continue
        time_ms = trigger.get("loiterTriggerTime")
        if isinstance(time_ms, bool) or not isinstance(time_ms, (int, float)):
            continue
        dwell.setdefault(trigger["objectType"], time_ms / 1000)
    return dwell or None


def zone_detects(types: tuple[str, ...] | None, object_type: str) -> bool:
    """Can a zone set to detect `types` (parse_types) have this in it?

    A face or a plate counts if the zone detects it or what carries it
    (TYPE_GROUPS). A zone that doesn't say what it detects can have anything,
    and so can a type we don't know.
    """
    if types is None or object_type not in OBJECT_TYPES:
        return True
    return object_type in types or _group(object_type) in types


def _connected(cam: dict[str, Any]) -> bool:
    """False only if Protect says the camera isn't connected; if it doesn't
    say, the camera is taken to be."""
    if isinstance(cam.get("isConnected"), bool):
        return cam["isConnected"]
    if isinstance(cam.get("state"), str):
        return cam["state"].upper() == "CONNECTED"
    return True


def camera_connected(cameras: dict[str, dict[str, Any]] | None, mac: str) -> bool:
    """Can this camera report anything now: it is in Protect's list (cameras
    is parse_cameras' result) and not offline."""
    cam = (cameras or {}).get(mac)
    return bool(cam) and cam.get("connected") is not False


def gone(
    registered: Iterable[tuple[str, ...]],
    current: Collection[tuple[str, ...]],
    cameras: dict[str, dict[str, Any]],
    counts: dict[tuple[str, ...], int],
    reads: int = 2,
) -> list[tuple[str, ...]]:
    """Which of the registered entities are for something Protect no longer has.

    Call it once per successful read of the camera list. registered and
    current are keys that start with the camera's mac and, for a zone's
    entity, go on with the kind (zone / line / loiter) and the zone id:
    registered the entities there are, current the ones there should be.
    counts is kept between calls: a key is returned (and is then to be
    removed) once `reads` reads in a row had no place for it, so one odd
    answer can't wipe entities.

    A camera that is offline is still listed, with its zones, so nothing of
    it is returned. A camera whose answer left a kind of zone out altogether
    (rather than listing none) says nothing either way about those zones, nor
    about an entity of the camera as a whole (a key of just the mac).
    """
    registered = list(registered)
    for key in [k for k in counts if k in current or k not in registered]:
        counts.pop(key)
    result = []
    for key in registered:
        if key in current:
            continue
        unsaid = cameras[key[0]]["missing"] if key[0] in cameras else ()
        if (key[1] in unsaid) if len(key) > 1 else bool(unsaid):
            continue
        counts[key] = counts.get(key, 0) + 1
        if counts[key] >= reads:
            counts.pop(key)
            result.append(key)
    return result


def parse_cameras(cameras: Any) -> dict[str, dict[str, Any]]:
    """Protect's camera list -> {camera mac: {"id", "name", "zone": [...],
    "line": [...], "loiter": [...], "missing": (...), "ptz": bool,
    "connected": bool}}, each zone {"id", "name", "color", "points", "types",
    "dwell"} (parse_color, parse_points, parse_types, parse_dwell).

    A camera that is offline is still in Protect's list, with its zones:
    "connected" is False for it (camera_connected). One that was removed from
    Protect is no longer in the list, and one that Protect lists but no
    longer manages (isAdopted false) is left out here, which is the same.

    "missing" names the kinds whose key Protect left out (or sent as something
    other than a list) for that camera: that is "didn't say", which is not the
    same as an empty list ("has none").

    Raises ValueError if the answer isn't a list at all.
    """
    if not isinstance(cameras, list):
        raise ValueError("Protect gave an unexpected answer for the cameras")
    result: dict[str, dict[str, Any]] = {}
    for cam in cameras:
        if not isinstance(cam, dict) or not cam.get("mac") or cam.get("isAdopted") is False:
            continue
        mac = str(cam["mac"])
        flags = cam.get("featureFlags")
        entry: dict[str, Any] = {
            "id": cam.get("id"),
            "name": cam.get("name") or mac,
            "ptz": isinstance(flags, dict) and flags.get("isPtz") is True,  # it can move
            "connected": _connected(cam),
        }
        missing = []
        for kind, key in ZONE_KINDS:
            zones = cam.get(key)
            if not isinstance(zones, list):
                missing.append(kind)
                zones = []
            entry[kind] = [
                {
                    "id": z["id"],
                    "name": str(z.get("name") or "").strip() or f"#{z['id']}",
                    "color": parse_color(z.get("color")),
                    "points": parse_points(z.get("points")),
                    "types": parse_types(z),
                    "dwell": parse_dwell(z),
                }
                for z in zones
                if isinstance(z, dict) and isinstance(z.get("id"), int) and not isinstance(z.get("id"), bool)
            ]
        entry["missing"] = tuple(missing)
        result[mac] = entry
    return result


# How far (in the camera's own steps) a camera may be from its home position
# and still count as there. Pan and tilt are about 100 steps to a degree, zoom
# runs 0..1000.
ALIGNED_WITHIN = {"pan": 20, "tilt": 20, "zoom": 5}


def ptz_aligned(home: Any, position: Any) -> bool | None:
    """Is a camera that can move at its home position?

    home is Protect's answer to cameras/<id>/ptz/home, {"slot": -1, "ptz":
    {"pan", "tilt", "zoom", ...}}, and position its answer to
    cameras/<id>/ptz/position, {"steps": {"pan", "tilt", "zoom", ...}, ...};
    both in the camera's steps. None if either isn't that, so we can't tell.
    """
    at_home = home.get("ptz") if isinstance(home, dict) else None
    now = position.get("steps") if isinstance(position, dict) else None
    if not isinstance(at_home, dict) or not isinstance(now, dict):
        return None
    for axis, within in ALIGNED_WITHIN.items():
        a, b = at_home.get(axis), now.get(axis)
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in (a, b)):
            return None
        if abs(a - b) > within:
            return False
    return True


def overlay_payload(
    cameras: dict[str, dict[str, Any]] | None,
    occupancy: Callable[[str, int], tuple[bool, list[str]]],
    live: bool,
    aligned: Callable[[str], bool | None] | None = None,
) -> dict[str, Any]:
    """The zones as shapes to draw over each camera's picture:

    {"cameras": {<Protect camera id>: {"mac", "name", "connected", "aligned", "zones": [
        {"id", "kind", "name", "color", "types", "dwell", "points", "occupied",
         "detected"}]}}}

    types is what the zone is set to detect, in Protect's words (None if it
    doesn't say); dwell, for a loiter zone, how many seconds each of them
    must stay ({object type: seconds}).

    cameras is parse_cameras' result; occupancy(mac, zone id) says whether a
    detection zone is occupied and by what. Cameras without an id or without
    any zone are left out.

    connected is false for a camera that is offline. occupied / detected are
    None for lines and loiter zones (they have no occupancy), for the zones
    of a camera that is offline, and for every zone while `live` is false
    (the live stream is down or the zone list is stale, so we can't tell). detected is what is in
    the zone ("person", or "person,vehicle" if more than one kind), None if
    it is empty.

    aligned says whether a camera that can move is at its home position
    (aligned(mac): True, False, or None if that isn't known now; ptz_aligned).
    Protect doesn't record where a camera looked when a zone was drawn; home
    is where it returns to by itself, so that is taken to be it. None for a
    camera that can't move.
    """
    result: dict[str, Any] = {}
    for mac, cam in (cameras or {}).items():
        if not cam.get("id"):
            continue
        shapes = []
        connected = cam.get("connected") is not False
        for kind, _ in ZONE_KINDS:
            for zone in cam.get(kind) or []:
                occupied = detected = None
                if kind == "zone" and live and connected:
                    occupied, seen = occupancy(mac, zone["id"])
                    occupied = bool(occupied)
                    detected = ",".join(seen) if occupied and seen else None
                shapes.append(
                    {
                        "id": zone["id"],
                        "kind": kind,
                        "name": zone["name"],
                        "color": zone.get("color"),
                        "types": list(zone["types"]) if zone.get("types") else None,
                        "dwell": zone.get("dwell"),
                        "points": zone.get("points") or [],
                        "occupied": occupied,
                        "detected": detected,
                    }
                )
        if shapes:
            result[str(cam["id"])] = {
                "mac": mac,
                "name": cam["name"],
                "connected": connected,
                "aligned": aligned(mac) if aligned is not None and cam.get("ptz") and connected else None,
                "zones": shapes,
            }
    return {"cameras": result}


# ----------------------------------------------------------------- editing
#
# Protect keeps a camera's zones in three lists on the camera
# (smartDetectZones, smartDetectLines, smartDetectLoiterZones). A zone there
# looks like this (a detection zone; the others have other extra fields):
#
#   {"id": 4, "name": " Front Yard", "color": "#586CED",
#    "points": [[0.615, 0.212], [0.87, 0.333], [1, 0.465], ...],
#    "sensitivity": 50, "objectTypes": [...], "mergeId": "1794971031812", ...}
#
# To change one, the whole list it is in is sent back. Everything we don't
# mean to change (other zones, and every other field of the changed one,
# known to us or not) goes back exactly as it was read.
#
# mergeId: Protect shows a detection zone and a loiter zone that share a
# mergeId as ONE zone (same name, colour and outline, in two lists). So a
# change to one is made to all of them, or they would come apart.

MIN_POINTS = {"zone": 3, "loiter": 3, "line": 2}
MAX_POINTS = 32
MAX_NAME = 64
# how long a thing must stay in a loiter zone, in seconds (Protect's own
# screens offer 10 and up; what it accepts below that is for it to say)
MIN_DWELL = 0.5
MAX_DWELL = 600
KEEP_HISTORY = 10
_KIND_KEY = dict(ZONE_KINDS)
_HEX = re.compile(r"#[0-9a-fA-F]{6}")


class ZoneEditError(ValueError):
    """A change that can't be made. code is short and fixed (for programs), the text is for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def raw_points(kind: str, points: Any) -> list[list[int | float]]:
    """[[x, y], ...] (each 0..1 from the top left) as Protect stores them:
    three decimals, and a whole 0 or 1 as an integer. Raises ZoneEditError if
    they aren't a usable outline for this kind of zone."""
    if not isinstance(points, list):
        raise ZoneEditError("invalid_points", "The points must be a list of [x, y] pairs")
    raw = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ZoneEditError("invalid_points", "Each point must be an [x, y] pair")
        pair: list[int | float] = []
        for number in point:
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ZoneEditError("invalid_points", "Each point must be two numbers")
            if not 0 <= number <= 1:
                raise ZoneEditError("invalid_points", "Each point must be within the picture (0 to 1)")
            number = round(float(number), 3)
            pair.append(int(number) if number in (0.0, 1.0) else number)
        raw.append(pair)
    least = MIN_POINTS[kind]
    if len(raw) > MAX_POINTS:
        raise ZoneEditError("invalid_points", f"At most {MAX_POINTS} points")
    if len({tuple(p) for p in raw}) < least:
        raise ZoneEditError("invalid_points", f"At least {least} different points")
    return raw


def zone_change(
    camera: Any,
    kind: str,
    zone_id: int,
    *,
    points: Any = None,
    name: Any = None,
    color: Any = None,
    types: Any = None,
    dwell: Any = None,
) -> tuple[dict[str, list[Any]], dict[str, Any]] | None:
    """What to send Protect to change one zone of a camera.

    camera is the camera exactly as just read from Protect. Only the fields
    given (not None) are changed: points [[x, y], ...] 0..1 from the top left,
    name, color "#rrggbb"; and, for this zone alone (not those Protect shows
    as one with it):
      types  what a detection zone detects, ["person", "vehicle", ...];
      dwell  what a loiter zone detects and after how many seconds,
             {"person": 5, ...}: it replaces the whole set.

    Returns (body, snapshot), or None if nothing would change:
      body      {list key: the whole new list} for each list that changes;
                this is what is sent (PATCH cameras/<id>).
      snapshot  {"changed": [[kind, id], ...], "lists": {list key: the list
                as it was}} for those same lists, for zone_undo.

    The camera passed in is not modified. Raises ZoneEditError if the change
    is not valid or the zone isn't there.
    """
    if kind not in _KIND_KEY:
        raise ZoneEditError("invalid_kind", "The kind must be zone, line or loiter")
    if not isinstance(camera, dict):
        raise ZoneEditError("unexpected_answer", "Protect gave an unexpected answer for the camera")
    if all(value is None for value in (points, name, color, types, dwell)):
        raise ZoneEditError("nothing_to_change", "Give the points, name, colour, types or dwell to change")
    fields: dict[str, Any] = {}
    if points is not None:
        fields["points"] = raw_points(kind, points)
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            raise ZoneEditError("invalid_name", "The name can't be empty")
        if len(name.strip()) > MAX_NAME:
            raise ZoneEditError("invalid_name", f"The name can be at most {MAX_NAME} characters")
        fields["name"] = name.strip()
    if color is not None:
        if not isinstance(color, str) or not _HEX.fullmatch(color):
            raise ZoneEditError("invalid_color", 'The colour must look like "#rrggbb"')
        fields["color"] = color.upper()  # as Protect writes them

    target = _find(camera, kind, zone_id)
    if target is None:
        raise ZoneEditError("zone_not_found", "Protect has no such zone on this camera (any more)")
    own: dict[str, Any] = {}  # for this zone alone
    if types is not None:
        if kind != "zone" or not isinstance(target.get("objectTypes"), list):
            raise ZoneEditError("invalid_types", "Only a detection zone has object types to change")
        own["objectTypes"] = _object_types(types, target["objectTypes"])
    if dwell is not None:
        if kind != "loiter" or not isinstance(target.get("loiterTriggers"), list):
            raise ZoneEditError("invalid_dwell", "Only a loiter zone has a dwell time to change")
        own["loiterTriggers"] = _loiter_triggers(dwell, target["loiterTriggers"])
    merge_id = target.get("mergeId")
    # the zone itself, and those Protect shows as one with it
    together = [(kind, zone_id)]
    if merge_id is not None:
        for other_kind, key in ZONE_KINDS:
            for zone in camera.get(key) if isinstance(camera.get(key), list) else []:
                if isinstance(zone, dict) and zone.get("mergeId") == merge_id and (other_kind, zone.get("id")) != (kind, zone_id):
                    if "points" in fields:
                        raw_points(other_kind, fields["points"])  # must suit that kind too
                    together.append((other_kind, zone.get("id")))

    body: dict[str, list[Any]] = {}
    before: dict[str, list[Any]] = {}
    changed = []
    for one_kind, one_id in together:
        key = _KIND_KEY[one_kind]
        if key not in body:
            body[key] = copy.deepcopy(camera[key])
        zone = next(z for z in body[key] if isinstance(z, dict) and z.get("id") == one_id)
        # a name that only differs by the spaces Protect keeps around it is not a change
        new = {
            k: v
            for k, v in fields.items()
            if not (k == "name" and isinstance(zone.get("name"), str) and zone["name"].strip() == v)
        }
        if (one_kind, one_id) == (kind, zone_id):
            new.update(own)
        if all(k in zone and _same(zone[k], v) for k, v in new.items()):
            continue
        zone.update(new)
        changed.append([one_kind, one_id])
    body = {key: zones for key, zones in body.items() if zones != camera[key]}
    if not body:
        return None
    before = {key: copy.deepcopy(camera[key]) for key in body}
    return body, {"changed": changed, "lists": before}


def _object_types(types: Any, now: list[Any]) -> list[str]:
    """The object types asked for, checked: ones we know, or that the zone already has."""
    if not isinstance(types, list) or not types:
        raise ZoneEditError("invalid_types", "Give at least one object type")
    for object_type in types:
        if not isinstance(object_type, str) or (object_type not in OBJECT_TYPES and object_type not in now):
            raise ZoneEditError("invalid_types", f"Not an object type a zone can detect: {object_type!r}")
    return list(dict.fromkeys(types))


def _loiter_triggers(dwell: Any, now: list[Any]) -> list[dict[str, Any]]:
    """Protect's loiterTriggers for {object type: seconds}. A trigger the zone
    already has keeps whatever else Protect stores in it."""
    if not isinstance(dwell, dict) or not dwell:
        raise ZoneEditError("invalid_dwell", "Give at least one object type and its seconds")
    have = {t["objectType"]: t for t in now if isinstance(t, dict) and isinstance(t.get("objectType"), str)}
    triggers = []
    for object_type, seconds in dwell.items():
        if not isinstance(object_type, str) or (object_type not in OBJECT_TYPES and object_type not in have):
            raise ZoneEditError("invalid_dwell", f"Not an object type a loiter zone can detect: {object_type!r}")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            raise ZoneEditError("invalid_dwell", "The dwell time must be a number of seconds")
        if not MIN_DWELL <= seconds <= MAX_DWELL:
            raise ZoneEditError("invalid_dwell", f"The dwell time must be {MIN_DWELL} to {MAX_DWELL} seconds")
        trigger = copy.deepcopy(have.get(object_type, {"objectType": object_type}))
        trigger["loiterTriggerTime"] = round(seconds * 1000)
        triggers.append(trigger)
    return triggers


def zone_undo(camera: Any, snapshot: Any) -> dict[str, list[Any]] | None:
    """What to send Protect to put back the zones a change touched.

    camera is the camera as just read; snapshot is what zone_change returned
    with that change. Only the zones that change touched are put back as they
    were, into the lists as they are NOW, so anything else done since (in the
    Protect app, say) is kept. Returns {list key: whole new list}, or None if
    they already are as they were. Raises ZoneEditError if one of those zones
    no longer exists.
    """
    if not isinstance(camera, dict) or not isinstance(snapshot, dict):
        raise ZoneEditError("unexpected_answer", "Protect gave an unexpected answer for the camera")
    body: dict[str, list[Any]] = {}
    for kind, zone_id in snapshot.get("changed") or []:
        key = _KIND_KEY.get(kind)
        saved = next(
            (z for z in (snapshot.get("lists") or {}).get(key) or [] if isinstance(z, dict) and z.get("id") == zone_id),
            None,
        )
        if key is None or saved is None:
            raise ZoneEditError("bad_history", "The saved state of this zone can't be read")
        if _find(camera, kind, zone_id) is None:
            raise ZoneEditError("zone_not_found", "A zone that was changed no longer exists in Protect")
        if key not in body:
            body[key] = copy.deepcopy(camera[key])
        index = next(i for i, z in enumerate(body[key]) if isinstance(z, dict) and z.get("id") == zone_id)
        body[key][index] = copy.deepcopy(saved)
    body = {key: zones for key, zones in body.items() if zones != camera[key]}
    return body or None


def remember(history: list[Any], snapshot: dict[str, Any]) -> list[Any]:
    """history (oldest first) with this snapshot added, keeping the last KEEP_HISTORY."""
    return [*history, snapshot][-KEEP_HISTORY:]


def _find(camera: dict[str, Any], kind: str, zone_id: Any) -> dict[str, Any] | None:
    zones = camera.get(_KIND_KEY[kind])
    if not isinstance(zones, list) or isinstance(zone_id, bool):
        return None
    return next((z for z in zones if isinstance(z, dict) and z.get("id") == zone_id), None)


def _same(a: Any, b: Any) -> bool:
    """Equal, and not just because 1 == 1.0 == True."""
    return a == b and repr(a) == repr(b)


def zone_key(unique_id: str, tail: str = "") -> tuple[str, str, int] | None:
    """(camera mac, kind, zone id) from a zone entity's unique id
    "zone_<mac>_<kind>_<id><tail>", or None if it isn't one with this tail.

    With no tail an id that has one ("…_occupied") is not taken for the plain one.
    """
    if tail:
        if not unique_id.endswith(tail):
            return None
        unique_id = unique_id[: -len(tail)]
    parts = unique_id.split("_")
    if len(parts) == 4 and parts[0] == "zone" and parts[3].isdigit():
        return parts[1], parts[2], int(parts[3])
    return None


# ------------------------------------------------------------ live updates

type CameraLookup = Callable[[Any], tuple[str, str] | None]  # Protect camera id -> (mac, name)
type HitSender = Callable[[str, str, int, str, dict[str, Any]], None]  # mac, kind, zone id, type, attributes
type OccupancySender = Callable[[str, int, bool, list[str]], None]  # mac, zone id, occupied, what was seen
type ZoneTypes = Callable[[str, str, int], tuple[str, ...] | None]  # mac, kind, zone id -> parse_types


class ZoneTracker:
    """Follows Protect's detection events and reports

    - send_hit(mac, kind, zone_id, event_type, attrs) once per detection, zone
      and kind of thing seen;
    - send_occupancy(mac, zone_id, occupied, seen) when a detection zone
      becomes occupied or empty, or what is in it changes.
    """

    def __init__(
        self,
        camera_by_id: CameraLookup,
        send_hit: HitSender,
        send_occupancy: OccupancySender,
        clock: Callable[[], float] = time.monotonic,
        zone_types: ZoneTypes | None = None,
    ) -> None:
        self._camera_by_id = camera_by_id
        self._zone_types = zone_types
        self._send_hit = send_hit
        self._send_occupancy = send_occupancy
        self._clock = clock
        # detection event id -> what we know of it and which zone hits were sent
        self._events: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # (camera mac, zone id) -> the detections now in that zone:
        # {detection event id: [when last heard of, what it has seen]}
        self._occupants: dict[tuple[str, int], dict[str, list[Any]]] = {}

    def detection(self, event_id: str | None, data: dict[str, Any]) -> None:
        """One update of a Protect event (the first carries its type, later ones only what changed)."""
        if not event_id or not isinstance(data, dict):
            return
        ev = self._events.get(event_id)
        if ev is None:
            if data.get("type") not in ZONE_EVENT_TYPES:
                return
            ev = self._events[event_id] = {"camera": None, "types": [], "hits": {}, "since": {}, "sent": set()}
            while len(self._events) > REMEMBER_EVENTS:
                self._events.popitem(last=False)
        ev["camera"] = data.get("camera") or data.get("device") or ev["camera"]
        types = data.get("smartDetectTypes")
        for t in types if isinstance(types, list) else []:
            if isinstance(t, str) and t not in ev["types"]:
                ev["types"].append(t)

        meta = data.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
        inside: dict[int, bool] = {}  # zone id -> in it now, as far as this update says
        for key, kind in STATUS_KEYS:
            status = meta.get(key)
            if not isinstance(status, dict):
                continue
            for raw_id, val in status.items():
                zid = _int(raw_id)
                if zid is None:
                    continue  # not a zone id we understand: skip it, keep the rest
                state = val.get("status") if isinstance(val, dict) else val
                # "leave" is no hit: when a detection ends Protect says "leave"
                # for every zone, also those it never was in
                if state and state not in ("none", "leave"):
                    self._hit(ev, kind, zid, val.get("level") if isinstance(val, dict) else None)
                if kind == "zone":
                    inside[zid] = bool(state) and state not in ("none", "leave")
        labels = data.get("labels")
        labels = list(labels) if isinstance(labels, list) else []
        thumbs = meta.get("detectedThumbnails")
        for thumb in thumbs if isinstance(thumbs, list) else []:
            if isinstance(thumb, dict) and isinstance(thumb.get("labels"), list):
                labels += thumb["labels"]
        for label in labels:
            parts = str(label).split(":")
            if len(parts) == 2 and parts[0] == "smartDetectType" and parts[1] not in ev["types"]:
                ev["types"].append(parts[1])
            elif len(parts) == 3 and parts[0] in ("zone", "line", "loiter") and parts[2].isdigit():
                self._hit(ev, parts[0], int(parts[2]), None, keep_level=True)

        camera = self._camera_by_id(ev["camera"])
        if camera is None:
            return
        mac, camera_name = camera
        ended = bool(data.get("end"))
        if ended:  # the detection is over: it is in no zone any more
            inside = {zid: False for m, zid in self._occupants if m == mac and event_id in self._occupants[m, zid]}
        for zid, present in inside.items():
            self._set_occupant(mac, zid, event_id, self._seen_in(ev, mac, zid) if present else None)
        # what it is can arrive after where it is, in an update that names no
        # zone: the zones it is already in must show that too
        for (m, zid), occupants in list(self._occupants.items()):
            if m == mac and zid not in inside and event_id in occupants:
                seen = self._seen_in(ev, mac, zid)
                if occupants[event_id][1] != seen:
                    self._set_occupant(mac, zid, event_id, seen, heard=occupants[event_id][0])
        self._send_hits(event_id, ev, mac, camera_name, final=ended)

    def sweep(self) -> None:
        """Run this every few seconds: drops detections that went silent, and
        fires the zone hits that waited in vain for an object type."""
        now = self._clock()
        for (mac, zone_id), occupants in list(self._occupants.items()):
            for event_id in [e for e, (at, _) in occupants.items() if now - at > OCCUPANCY_STALE]:
                self._set_occupant(mac, zone_id, event_id, None)
        for event_id, ev in list(self._events.items()):
            if ev["types"] or not ev["hits"]:
                continue
            if (camera := self._camera_by_id(ev["camera"])) is not None:
                self._send_hits(event_id, ev, camera[0], camera[1], final=False)

    def occupancy(self, mac: str, zone_id: int) -> tuple[bool, list[str]]:
        """Is this detection zone occupied now, and what is in it."""
        occupants = self._occupants.get((mac, zone_id)) or {}
        return bool(occupants), self._seen(occupants)

    def clear_occupancy(self, only: str | None = None) -> None:
        """The stream dropped, or (only: its mac) one camera went offline or
        was removed: we no longer hear who leaves."""
        for (mac, zone_id), occupants in list(self._occupants.items()):
            if only is not None and mac != only:
                continue
            for event_id in list(occupants):
                self._set_occupant(mac, zone_id, event_id, None)

    # ------------------------------------------------------------ internals

    def _hit(self, ev: dict[str, Any], kind: str, zid: int, level: Any, keep_level: bool = False) -> None:
        if keep_level:
            ev["hits"].setdefault((kind, zid), None)
        else:
            ev["hits"][(kind, zid)] = level
        ev["since"].setdefault((kind, zid), self._clock())

    def _send_hits(self, event_id: str, ev: dict[str, Any], mac: str, camera_name: str, final: bool) -> None:
        now = self._clock()
        for (kind, zid), level in ev["hits"].items():
            if ev["types"]:
                found = [OBJECT_TYPES.get(t, DETECTED) for t in self._types_in(ev, mac, kind, zid)]
            elif final or now - ev["since"][(kind, zid)] >= UNTYPED_GRACE:
                found = [DETECTED]
            else:
                continue  # wait a moment for Protect to say what it saw
            for event_type in found:
                if (kind, zid, event_type) in ev["sent"]:
                    continue
                ev["sent"].add((kind, zid, event_type))
                self._send_hit(
                    mac,
                    kind,
                    zid,
                    event_type,
                    {"camera": camera_name, "detection_event_id": event_id, "confidence": level},
                )

    def _types_in(self, ev: dict[str, Any], mac: str, kind: str, zid: int) -> list[str]:
        """The object types of this detection that count for this zone.

        One detection can hold several things (a person on the path and a car
        passing behind), and Protect doesn't say which of them is in which
        zone: every type the detection has seen counts for every zone it is
        in. Except a type the zone isn't set to detect (zone_detects): that
        much Protect does say. (Unless that leaves nothing: Protect says the
        detection is in the zone, so our copy of its settings must be old.)
        """
        detects = self._zone_types(mac, kind, zid) if self._zone_types else None
        return [t for t in ev["types"] if zone_detects(detects, t)] or list(ev["types"])

    def _seen_in(self, ev: dict[str, Any], mac: str, zid: int) -> list[str]:
        """What a detection zone's occupancy says this detection is."""
        return sorted({OBJECT_TYPES.get(t, DETECTED) for t in self._types_in(ev, mac, "zone", zid)})

    def _set_occupant(
        self, mac: str, zone_id: int, event_id: str, seen: list[str] | None, heard: float | None = None
    ) -> None:
        """A detection is in a zone (seen: what it has seen so far) or has left it (None).

        heard: when the zone last named it, if that isn't now.
        """
        occupants = self._occupants.setdefault((mac, zone_id), {})
        before = (bool(occupants), self._seen(occupants))
        if seen is None:
            occupants.pop(event_id, None)
        else:
            occupants[event_id] = [self._clock() if heard is None else heard, seen]
        after = (bool(occupants), self._seen(occupants))
        if not occupants:
            self._occupants.pop((mac, zone_id), None)
        if after != before:
            self._send_occupancy(mac, zone_id, *after)

    @staticmethod
    def _seen(occupants: dict[str, list[Any]]) -> list[str]:
        return sorted({t for _, seen in occupants.values() for t in seen})
