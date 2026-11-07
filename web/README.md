# Status page (`web/`)

A static status page for a ShellBeacon station — the beacon's public
face on the web. The reference station's is live at
<https://www.cenzontle.us/kj5gby/>.

Every five minutes the generator reads the station's own sources of
truth and re-renders one self-contained HTML page (~15 KB, no external
assets, no JavaScript libraries, no trackers):

- **Status**, in the beacon's own vocabulary — *ALL STATION SYSTEMS
  NORMAL*, or what is actually down (*WEATHER SYSTEM IS DOWN*,
  *WEB CHECK FAILING*, *BEACON OVERDUE*).
- **Weather**: the latest patio-sensor reading from `wx-collect.sh`,
  its age, and a 24-hour temperature trace (the generator keeps the
  rolling history itself).
- **Telemetry as transmitted**: the last T# frame is decoded out of
  the beacon's systemd journal — what aprs.fi received is what the
  page shows, not a re-measurement.
- **A locator chart**: the station's Maidenhead square with the
  subsquare grid superimposed — the station's subsquare highlighted,
  the station pinned at its exact transmitted position. The base is a
  **real map**: run `make-basemap.py` once and it fetches OpenStreetMap
  tiles for your square, stitches them, and re-renders them as a muted
  Solarized night chart (one local JPEG, no tile calls or trackers
  when the page is viewed; map data © OpenStreetMap contributors).
  Without a basemap, the chart falls back to a drawn grid with
  optional local geography (towns, highways, rivers) from a
  map-features file — `map-features.example.json` is the reference
  station's San Antonio set.
- **JSON feeds** next to the page: `latest.json` (the weather file,
  copied) and `telemetry.json` (status, weather, and the decoded
  frame) for anyone who would rather consume the station as data.

The panel is Solarized in both modes and **dark by default**; the
corner button toggles, and the choice is remembered. A whisper of
JavaScript refreshes the numbers from `telemetry.json` once a minute;
with JavaScript off, the baked-in values are at most five minutes old.

## Install

```sh
cd web
sudo ./install.sh
# edit /usr/local/etc/shellbeacon-page.conf - callsign, place,
# lat/lon, comment, webroot, wxfile - then:
sudo systemctl start shellbeacon-page.service
```

`install.sh` places the generator in `/usr/local/sbin`, the units in
`/etc/systemd/system`, and the config in `/usr/local/etc` (an existing
config is never overwritten). Point your web server at the `webroot`
directory from the config; any static server will do — the files are
plain HTML and JSON.

## Files

| File | Purpose |
| --- | --- |
| `shellbeacon-page.py` | the generator (pure Python 3 stdlib) |
| `make-basemap.py` | one-time basemap builder (OpenStreetMap tiles → Solarized night chart; needs Pillow) |
| `shellbeacon-page.conf.example` | station configuration template |
| `map-features.example.json` | worked example: EL09 geography for the chart |
| `shellbeacon-page.service` / `.timer` | regenerate every five minutes |
| `install.sh` | root installer |

## Notes

- The config holds **no secrets** — your APRS passcode stays in
  `shellbeacon.conf`, which the generator never reads.
- The locator is computed from `lat`/`lon` unless `grid` is set.
- Staleness thresholds default to the beacon's own: a weather reading
  counts as fresh for 40 minutes (the fold-in threshold), a beacon
  frame for 45 (two cycles plus slack).
- The reference station serves the page from the same Raspberry Pi
  Zero W that runs the beacon, under its callsign as the path:
  `/kj5gby/`.
