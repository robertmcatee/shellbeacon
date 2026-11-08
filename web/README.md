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
- **Locator charts, five views** with a range switch: **Subsquare**
  (street level, the next Maidenhead level's 10 × 10 grid over it, the
  station's extended-square cell highlighted), **Hamlet** (the
  station's home ground — the neighbourhood inside the roads that
  bound daily life, the boundary drawn dashed over the streets it
  follows, and the practical places marked: school, library, shops,
  park, fire station), **Club** (the station's 5-mile service
  ring - the footprint of a hyper-local club - the ring drawn dashed
  with its communities and meeting places marked), **Square** (the whole
  square for context, subsquare highlighted), and **Range** (a
  station-centred chart with true 5/10/20-mile rings). The bases are
  **real maps that follow the page theme**: run `make-basemap.py` once
  per view and it fetches OpenStreetMap tiles, stitches them, and
  re-renders each view twice — a Solarized night chart for the dark
  theme and a light chart for the light theme (local JPEGs, no tile
  calls or trackers when the page is viewed; map data © OpenStreetMap
  contributors). Without basemaps, the chart falls back to a drawn map
  of the square with optional local geography (towns, highways,
  rivers) from a map-features file — `map-features.example.json` is
  the reference station's San Antonio set.
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
| `make-basemap.py` | one-time basemap builder: per view, OpenStreetMap tiles → Solarized dark + light charts (needs Pillow) |
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
