# Protect Zones

A Home Assistant custom integration that turns the zones you drew in UniFi Protect into entities. It is a companion to Home Assistant's own UniFi Protect integration and uses its connection.

For every camera it adds:

- An **event** for each detection zone, line and loiter zone. It fires when Protect detects something there, and the event type is what was seen: `person`, `vehicle`, `animal`, `package`, `license_plate`, `face`, or `detected` when Protect doesn't say.
- An **occupancy binary sensor** for each detection zone, on while something is in it.
- An **"At home position" binary sensor** for each PTZ camera that has zones. Zones are drawn for the home position, so they don't match the picture while the camera looks elsewhere.

Everything is pushed live from Protect, so the entities work as automation triggers.

It only reads from Protect, with one exception: an administrator can edit a zone through a websocket command (see [Editing zones](#editing-zones)).

> This integration uses the API of the Protect app, which Ubiquiti doesn't document or promise to keep. A UniFi Protect or Home Assistant update can break it until it is updated to match. It is not affiliated with Ubiquiti.

## Requirements

- Home Assistant 2026.3 or newer.
- The [UniFi Protect integration](https://www.home-assistant.io/integrations/unifiprotect), set up and working. Protect Zones holds no credentials of its own; it uses that integration's login.
- Cameras with smart detections, and at least one smart detection zone, line or loiter zone drawn in Protect. Plain motion zones are not covered.
- To edit zones: the Protect user that the UniFi Protect integration logs in with must be allowed to change camera settings.

## Installation

### HACS

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/OnticEntia/protect-zones` with type **Integration**.
3. Find **Protect Zones** in HACS, download it, and restart Home Assistant.

### Manual

Copy the `custom_components/protect_zones` folder from this repository into the `custom_components` folder of your Home Assistant configuration, then restart Home Assistant.

## Configuration

Go to **Settings > Devices & services > Add integration** and choose **Protect Zones**. There is nothing to fill in, and it can be set up once.

Each camera with zones becomes a device named "Protect Zones <camera name>". Its entities follow Protect: the zone list is read every five minutes, so a zone that is added, renamed or removed in Protect shows up, is renamed or disappears in Home Assistant within that time.

## Entities

| Entity | For | Name | Notes |
| --- | --- | --- | --- |
| Event | every detection zone, line and loiter zone | the zone's name, with the kind added for lines and loiter zones | Attributes: `zone`, `kind` (`zone`, `line` or `loiter`), `camera`, `detection_event_id`, `confidence` |
| Binary sensor (occupancy) | every detection zone | "<zone> occupied" | Attributes: `zone`, `camera`, `detected` (what the detections in the zone have seen) |
| Binary sensor | every PTZ camera with zones | "At home position" | Attributes: `pan`, `tilt`, `zoom` in the camera's own steps. Asks Protect for the position every 5 seconds |

The entities are unavailable while the live connection to Protect is down or the zone list could not be read for a while.

### What the zones can and can't tell you

- A zone only reports the object types it is set to detect in Protect. If a zone should react to people, enable "person" for it in Protect.
- Protect reports one detection for everything it sees at the same time. When a person and a passing car are in the picture together, Protect doesn't say which of them is in which zone, so each zone the detection touches reports every type that zone is set to detect. Narrow the object types per zone in Protect to keep this apart.
- A loiter zone fires once the object has stayed for the zone's dwell time. Loiter zones have an event but no occupancy sensor.
- Lines are handled the same way as zones but have seen less use.

## Websocket commands

These are for dashboards and cards. `mac` is the camera's MAC address as Protect gives it (upper case, no separators), and points are `[x, y]` pairs from 0 to 1 measured from the top left of the picture.

### Zone shapes

For any logged-in user, read-only:

- `protect_zones/overlay` returns the zones of all cameras once.
- `protect_zones/overlay/subscribe` sends them at once and again whenever the zone list or a zone's occupancy changes.

```json
{
  "editable": true,
  "cameras": {
    "<Protect camera id>": {
      "mac": "AABBCCDDEEFF",
      "name": "Front Door",
      "aligned": null,
      "zones": [
        {
          "id": 1,
          "kind": "zone",
          "name": "Porch",
          "color": "#00ff00",
          "types": ["person"],
          "dwell": null,
          "points": [[0.1, 0.1], [0.9, 0.1], [0.5, 0.9]],
          "occupied": false,
          "detected": null
        }
      ]
    }
  }
}
```

`aligned` is `true` or `false` for a PTZ camera (is it at its home position) and `null` otherwise. `editable` says whether the user asking is an administrator.

### Editing zones

For administrators only. This is the only thing that writes to Protect.

- `protect_zones/zone/update` with `mac`, `kind` (`zone`, `line` or `loiter`), `zone_id`, and any of:
  - `points`: the new outline
  - `name`, `color` (`#rrggbb`)
  - `types`: what a detection zone detects, for example `["person", "face"]`
  - `dwell`: what a loiter zone detects and after how many seconds, for example `{"person": 5}`
- `protect_zones/zone/undo` with `mac` takes back the last change made to that camera's zones through this integration.

The zone is named by `zone_id`, not `id`; `id` is the websocket message's own number. The state before each change is kept, the last 10 per camera, in `.storage/protect_zones.zone_history`. Creating and deleting zones is not supported; do that in Protect.

## Related integrations

Protect Zones opens one connection to Protect's live updates and shares it. Two other companions use that connection and need Protect Zones installed and set up:

- [Protect Rules](https://github.com/OnticEntia/protect-rules)
- [Protect Detections](https://github.com/OnticEntia/protect-detections)

Install Protect Zones first. Each works without the other two being configured beyond that.

## Troubleshooting

- **"Set up the UniFi Protect integration first"** when adding it: the UniFi Protect integration isn't loaded. Set it up, or fix it, and try again.
- **No entities for a camera**: the camera has no smart detection zones, lines or loiter zones in Protect. New zones appear within five minutes, or at once after reloading the integration.
- **A zone never fires for people (or vehicles)**: check the zone's object types in Protect.
- **A repair issue about Protect live updates**: a Home Assistant or UniFi Protect update changed something this integration relies on. Check this repository for an update and [open an issue](https://github.com/OnticEntia/protect-zones/issues) if there is none. The message clears by itself once updates work again.
- **Entities unavailable**: the live connection to Protect is down. It reconnects by itself; check that the UniFi Protect integration is working.
- **Seeing what Protect sends**: enable debug logging for the integration (Settings > Devices & services > Protect Zones > Enable debug logging). Every detection update is then logged in short form. Diagnostics can be downloaded from the same place; they contain camera names, MAC addresses and zone outlines, but no credentials.

## Development

`custom_components/protect_zones/zones.py` holds the reading of Protect's data and has no Home Assistant imports. The tests use only the standard library:

```bash
python3 -B -m unittest discover -s tests
```

`tests/test_stream.py` also pins down what the related integrations rely on in `stream.py`.

## Licence

[MIT](LICENSE)
