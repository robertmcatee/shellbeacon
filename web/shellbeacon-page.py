#!/usr/bin/env python3
# shellbeacon-page.py - the ShellBeacon status page generator.
#
# Renders a static instrument-panel web page for a ShellBeacon station
# and refreshes it on a timer. The page shows:
#   - station status, in the beacon's own vocabulary
#     ("ALL STATION SYSTEMS NORMAL", "WEATHER SYSTEM IS DOWN", ...)
#   - the latest weather reading from wx-collect.sh (~/wx/latest.json),
#     with a 24-hour temperature trace
#   - the last APRS telemetry frame the beacon actually transmitted,
#     decoded (CPU temp, load, free memory, disk used, uptime, web bit)
#   - a drawn Maidenhead locator chart of the station's square, with
#     optional local geography (towns, highways, rivers) from a
#     map-features file, and the station pinned at its exact position
#
# Outputs, in the configured web root:
#   index.html      the panel (Solarized, dark by default, no external
#                   assets, no trackers, ~15 KB)
#   latest.json     byte copy of the weather collector's latest file
#   telemetry.json  the decoded telemetry + status as JSON, for machines
#
# Inputs: the weather file, the beacon's systemd journal (the last T#
# frame and last position frame are the source of truth for what was
# transmitted), and the rf.enabled / voice.enabled flag files.
#
# Station settings live in shellbeacon-page.conf (shell-style
# KEY="value", like shellbeacon.conf) - see the .example file. Search
# order: $SHELLBEACON_PAGE_CONF, /usr/local/etc/shellbeacon-page.conf,
# ./shellbeacon-page.conf, ~/.shellbeacon-page.conf.
#
# Pure Python 3 stdlib. Run it from shellbeacon-page.timer (every five
# minutes) or by hand.
#
# Copyright (C) 2026 Robert McAtee, KJ5GBY. Licensed under the GNU
# General Public License v3.0 or later - see LICENSE in this repo,
# or <https://www.gnu.org/licenses/>.

import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

DEFAULTS = {
    "callsign": "N0CALL",
    "place": "",                          # e.g. "San Antonio, Texas"
    "grid": "",                           # computed from lat/lon if empty
    "lat": "0.0",
    "lon": "0.0",
    "comment": "",
    "about": "on a Raspberry Pi.",
    "repo_url": "",                       # link for the word "ShellBeacon"
    "footer_extra": "",                   # raw HTML appended to the footer
    "webroot": "/var/www/html/station",
    "statedir": "/var/lib/shellbeacon-page",
    "wxfile": os.path.expanduser("~/wx/latest.json"),
    "beacon_dir": "/var/aprs-beacon",
    "beacon_unit": "aprs-beacon",
    "map_features": "",             # optional JSON: towns/highways/rivers
    "map_base": "",                 # optional real basemap JPEG (make-basemap.py)
    "map_square": "",               # optional square-view basemap JPEG
    "map_range": "",                # optional range-view basemap JPEG
    "map_hamlet": "",               # optional hamlet-view basemap JPEG
    "hamlet_file": "",              # optional JSON: hamlet boundary + POIs
    "rf_freq": "144.390",
    "voice_freq": "146.430",
    "beacon_interval_min": "20",
    "wx_stale_min": "40",                 # the beacon's fold-in threshold
    "beacon_stale_min": "45",             # ~two cycles plus slack
}


def load_conf():
    conf = dict(DEFAULTS)
    candidates = [
        os.environ.get("SHELLBEACON_PAGE_CONF", ""),
        "/usr/local/etc/shellbeacon-page.conf",
        os.path.join(os.getcwd(), "shellbeacon-page.conf"),
        os.path.expanduser("~/.shellbeacon-page.conf"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            with open(path) as fh:
                for line in fh:
                    m = re.match(r'\s*([a-z_]+)\s*=\s*"?(.*?)"?\s*$', line)
                    if m and not line.lstrip().startswith("#"):
                        conf[m.group(1)] = m.group(2)
            break
    return conf


CONF = load_conf()
CALL = CONF["callsign"]
LAT, LON = float(CONF["lat"]), float(CONF["lon"])
WEBROOT = CONF["webroot"]
STATEDIR = CONF["statedir"]
BEACON_DIR = CONF["beacon_dir"]
WX_STALE_MIN = int(CONF["wx_stale_min"])
BEACON_STALE_MIN = int(CONF["beacon_stale_min"])


def maidenhead(lat, lon):
    """Full 6-character Maidenhead locator for a position."""
    adj_lon, adj_lat = lon + 180.0, lat + 90.0
    return "%s%s%d%d%s%s" % (
        chr(65 + int(adj_lon // 20)), chr(65 + int(adj_lat // 10)),
        int((adj_lon % 20) // 2), int(adj_lat % 10),
        chr(97 + int(((adj_lon % 2) / 2) * 24)),
        chr(97 + int((adj_lat % 1) * 24)))


GRID = CONF["grid"] or maidenhead(LAT, LON)


def read_wx():
    try:
        with open(CONF["wxfile"], "rb") as fh:
            raw = fh.read()
        data = json.loads(raw)
        when = datetime.strptime(data["time"], "%Y-%m-%d %H:%M:%S")
        return raw, data, when.replace(tzinfo=datetime.now().astimezone().tzinfo)
    except Exception:
        return None, None, None


def read_journal():
    """Last telemetry frame and last position frame from the beacon journal."""
    tele = None
    last_pos = None
    try:
        out = subprocess.run(
            ["journalctl", "-u", CONF["beacon_unit"], "-n", "600",
             "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None, None
    for line in out.splitlines():
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{4})", line)
        if not m:
            continue
        try:
            stamp = datetime.strptime(m.group(1) + m.group(2),
                                      "%Y-%m-%dT%H:%M:%S%z").astimezone()
        except ValueError:
            continue
        t = re.search(r"T#(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d{8})", line)
        if t:
            tele = {
                "seq": int(t.group(1)),
                "cpu_temp_f": int(t.group(2)),
                "load": int(t.group(3)) / 10.0,
                "free_mem_pct": int(t.group(4)),
                "disk_used_pct": int(t.group(5)),
                "uptime_days": int(t.group(6)),
                "web_up": t.group(7)[0] == "1",
                "when": stamp,
            }
        if "TCPIP:!" in line:
            last_pos = stamp
    return tele, last_pos


def update_history(wx_when, temp_f):
    """Append the reading to the 24 h log; return the trimmed series."""
    path = os.path.join(STATEDIR, "history.csv")
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) == 2:
                    rows.append((parts[0], float(parts[1])))
    except FileNotFoundError:
        pass
    stamp = wx_when.strftime("%Y-%m-%dT%H:%M:%S")
    if wx_when and (not rows or rows[-1][0] != stamp):
        rows.append((stamp, temp_f))
    cutoff = (wx_when - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = [r for r in rows if r[0] >= cutoff]
    os.makedirs(STATEDIR, exist_ok=True)
    with open(path, "w") as fh:
        for s, v in rows:
            fh.write("%s,%.1f\n" % (s, v))
    return rows


def sparkline(rows):
    if len(rows) < 2:
        return '<p class="dim">Collecting — the 24-hour trace fills in as readings arrive.</p>'
    vals = [v for _, v in rows]
    lo, hi = min(vals), max(vals)
    if hi - lo < 2:
        hi, lo = hi + 1, lo - 1
    w, h, pad = 640, 72, 6
    pts = []
    for i, v in enumerate(vals):
        x = pad + (w - 2 * pad) * i / (len(vals) - 1)
        y = h - pad - (h - 2 * pad) * (v - lo) / (hi - lo)
        pts.append("%.1f,%.1f" % (x, y))
    return (
        '<svg class="spark" viewBox="0 0 %d %d" role="img" aria-label="Temperature, last 24 hours">'
        '<polyline points="%s" fill="none" stroke="var(--accent)" stroke-width="1.5"/></svg>'
        '<p class="dim">24 h — low %.0f&deg;F &middot; high %.0f&deg;F</p>'
        % (w, h, " ".join(pts), lo, hi))


def load_features():
    path = CONF["map_features"]
    if not path:
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def load_hamlet():
    path = CONF["hamlet_file"]
    if not path:
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def light_variant(path):
    root, ext = os.path.splitext(path)
    return root + "-light" + ext


def basemap_pair(conf_key):
    """(dark, light) source paths if both files exist, else None."""
    dark = CONF.get(conf_key) or ""
    if dark and os.path.exists(dark) and os.path.exists(light_variant(dark)):
        return dark, light_variant(dark)
    return None


def chart_images(name, gx, gy, gw, gh):
    """The theme-following basemap pair for one chart: CSS shows the
    night chart in the dark theme and the light chart in the light."""
    return ('<image href="%s.jpg" x="%d" y="%d" width="%d" height="%d" '
            'preserveAspectRatio="none" class="imgdark"/>\n'
            '<image href="%s-light.jpg" x="%d" y="%d" width="%d" height="%d" '
            'preserveAspectRatio="none" class="imglight"/>'
            % (name, gx, gy, gw, gh, name, gx, gy, gw, gh))


def subsquare_svg():
    """Basemap mode: the chart zooms to the station's own subsquare -
    the real map (map-base.jpg, built by make-basemap.py for exactly
    this cell) with the next Maidenhead level's 10 x 10 grid over it,
    the station's extended-square cell highlighted, and the pin at the
    exact transmitted position. Grid Y uses Mercator, like the map."""
    west = math.floor(LON / 2.0) * 2.0
    south = math.floor(LAT)
    sub_x = int((LON - west) / 2.0 * 24)
    sub_y = int((LAT - south) * 24)
    cw_deg, ch_deg = 2.0 / 24, 1.0 / 24        # subsquare: 5' x 2.5'
    cell_w = west + sub_x * cw_deg
    cell_s = south + sub_y * ch_deg
    cell_e, cell_n = cell_w + cw_deg, cell_s + ch_deg
    W, H = 696, 424
    gx, gy, gw, gh = 48, 36, 600, 340

    def X(lon):
        return gx + gw * (lon - cell_w) / cw_deg

    def merc(lat):
        phi = math.radians(lat)
        return math.log(math.tan(phi) + 1 / math.cos(phi))

    def Y(lat):
        top, bot = merc(cell_n), merc(cell_s)
        return gy + gh * (top - merc(lat)) / (top - bot)

    def dm(value, pos, neg):
        d = int(abs(value))
        m = (abs(value) - d) * 60
        return "%d°%.1f′%s" % (d, m, pos if value >= 0 else neg)

    lines = []
    for i in range(11):                        # the 10 x 10 extended grid
        x = X(cell_w + cw_deg * i / 10.0)
        lines.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="sqr"/>' % (x, gy, x, gy + gh))
        y = Y(cell_s + ch_deg * i / 10.0)
        lines.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="sqr"/>' % (gx, y, gx + gw, y))

    ex = int((LON - cell_w) / (cw_deg / 10.0))  # the station's 8-char cell
    ey = int((LAT - cell_s) / (ch_deg / 10.0))
    ex_w = cell_w + ex * cw_deg / 10.0
    ey_s = cell_s + ey * ch_deg / 10.0
    cx, cy = X(ex_w), Y(ey_s + ch_deg / 10.0)
    cw2, ch2 = X(ex_w + cw_deg / 10.0) - cx, Y(ey_s) - cy
    px, py = X(LON), Y(LAT)
    ext = "%d%d" % (ex, ey)
    return """<svg class="map hasbase" viewBox="0 0 {W} {H}" role="img" aria-label="Map of Maidenhead subsquare {grid}, station in extended square {ext}">
<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" class="frame"/>
{images}
{lines}
<rect x="{cx:.1f}" y="{cy:.1f}" width="{cw2:.1f}" height="{ch2:.1f}" class="cell"/>
<text x="{gx2}" y="{gy2}" class="lab big">{grid}</text>
<text x="{tx:.1f}" y="{ty:.1f}" class="lab celllab">{ext}</text>
<circle cx="{px:.1f}" cy="{py:.1f}" r="10" class="pinring"/>
<circle cx="{px:.1f}" cy="{py:.1f}" r="4" class="pin"/>
<text x="{gx}" y="{top}" class="lab">{nlat}</text>
<text x="{gx}" y="{bot}" class="lab">{slat}</text>
<text x="{gx}" y="{bot2}" class="lab">{wlon}</text>
<text x="{rx}" y="{bot2}" class="lab end">{elon}</text>
<text x="{ax}" y="{ay}" class="lab attr">© OpenStreetMap contributors</text>
</svg>""".format(W=W, H=H, gx=gx, gy=gy, gw=gw, gh=gh,
                 lines="\n".join(lines), images=chart_images("map-base", gx, gy, gw, gh),
                 cx=cx, cy=cy, cw2=cw2, ch2=ch2, px=px, py=py,
                 gx2=gx + 10, gy2=gy + 24, tx=cx + cw2 + 6, ty=cy + ch2 - 3,
                 top=gy - 10, bot=gy + gh + 20, bot2=gy + gh + 38, rx=gx + gw,
                 ax=gx + 6, ay=gy + gh - 8,
                 grid=GRID, ext=ext,
                 nlat=dm(cell_n, "N", "S"), slat=dm(cell_s, "N", "S"),
                 wlon=dm(cell_w, "E", "W"), elon=dm(cell_e, "E", "W"))


RANGE_LAT_SPAN = 0.64          # keep in step with make-basemap.py


def _dm(value, pos, neg):
    d = int(abs(value))
    m = (abs(value) - d) * 60
    return "%d°%.1f′%s" % (d, m, pos if value >= 0 else neg)


def square_svg():
    """Basemap mode, square view: the whole Maidenhead square as a
    real map, subsquare hairlines over it (Mercator Y), the station's
    subsquare highlighted, pin at the exact position."""
    west = math.floor(LON / 2.0) * 2.0
    south = math.floor(LAT)
    W, H = 696, 424
    gx, gy, gw, gh = 48, 36, 600, 340

    def X(lon):
        return gx + gw * (lon - west) / 2.0

    def merc(lat):
        phi = math.radians(lat)
        return math.log(math.tan(phi) + 1 / math.cos(phi))

    def Y(lat):
        top, bot = merc(south + 1), merc(south)
        return gy + gh * (top - merc(lat)) / (top - bot)

    lines = []
    for i in range(25):
        x = gx + gw * i / 24
        lines.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="sub"/>' % (x, gy, x, gy + gh))
        y = Y(south + i / 24.0)
        lines.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="sub"/>' % (gx, y, gx + gw, y))
    sub_x = int((LON - west) / 2.0 * 24)
    sub_y = int((LAT - south) * 24)
    cx = gx + gw * sub_x / 24
    cy = Y(south + (sub_y + 1) / 24.0)
    cw, ch = gw / 24, Y(south + sub_y / 24.0) - cy
    px, py = X(LON), Y(LAT)
    return """<svg class="map hasbase" viewBox="0 0 {W} {H}" role="img" aria-label="Map of Maidenhead square {grid4}, station in subsquare {sub}">
<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" class="frame"/>
{images}
{lines}
<rect x="{cx:.1f}" y="{cy:.1f}" width="{cw:.1f}" height="{ch:.1f}" class="cell"/>
<text x="{gx2}" y="{gy2}" class="lab big">{grid4}</text>
<text x="{tx:.1f}" y="{ty:.1f}" class="lab celllab">{sub}</text>
<circle cx="{px:.1f}" cy="{py:.1f}" r="10" class="pinring"/>
<circle cx="{px:.1f}" cy="{py:.1f}" r="4" class="pin"/>
<text x="{gx}" y="{top}" class="lab">{nlat}&deg;N</text>
<text x="{gx}" y="{bot}" class="lab">{slat}&deg;N</text>
<text x="{gx}" y="{bot2}" class="lab">{wlon}&deg;W</text>
<text x="{rx}" y="{bot2}" class="lab end">{elon}&deg;W</text>
<text x="{ax}" y="{ay}" class="lab attr">© OpenStreetMap contributors</text>
</svg>""".format(W=W, H=H, gx=gx, gy=gy, gw=gw, gh=gh,
                 images=chart_images("map-square", gx, gy, gw, gh),
                 lines="\n".join(lines),
                 cx=cx, cy=cy, cw=cw, ch=ch, px=px, py=py,
                 gx2=gx + 10, gy2=gy + 24, tx=cx + cw + 6, ty=cy + ch - 3,
                 top=gy - 10, bot=gy + gh + 20, bot2=gy + gh + 38, rx=gx + gw,
                 ax=gx + 6, ay=gy + gh - 8,
                 grid4=GRID[:4], sub=GRID[4:],
                 nlat=int(south + 1), slat=int(south),
                 wlon=int(-west), elon=int(-(west + 2)))


def range_svg():
    """Basemap mode, range view: a station-centred chart with true
    5/10/20-mile range rings - the operator's view of who can hear
    the station. Bounds match make-basemap.py's range view."""
    south = LAT - RANGE_LAT_SPAN / 2.0
    north = LAT + RANGE_LAT_SPAN / 2.0

    def merc(lat):
        phi = math.radians(lat)
        return math.log(math.tan(phi) + 1 / math.cos(phi))

    dlon = (merc(north) - merc(south)) * (600.0 / 340.0) / (2 * math.pi) * 360.0
    west, east = LON - dlon / 2.0, LON + dlon / 2.0
    W, H = 696, 424
    gx, gy, gw, gh = 48, 36, 600, 340

    def X(lon):
        return gx + gw * (lon - west) / (east - west)

    def Y(lat):
        top, bot = merc(north), merc(south)
        return gy + gh * (top - merc(lat)) / (top - bot)

    def ring(miles):
        pts = []
        delta = miles / 3958.8
        phi, lam = math.radians(LAT), math.radians(LON)
        for step in range(73):
            theta = math.radians(step * 5)
            p2 = math.asin(math.sin(phi) * math.cos(delta) +
                           math.cos(phi) * math.sin(delta) * math.cos(theta))
            l2 = lam + math.atan2(math.sin(theta) * math.sin(delta) * math.cos(phi),
                                  math.cos(delta) - math.sin(phi) * math.sin(p2))
            pts.append("%.1f,%.1f" % (X(math.degrees(l2)), Y(math.degrees(p2))))
        return " ".join(pts)

    rings, labels = [], []
    for miles in (5, 10, 20):
        rings.append('<polyline points="%s" class="ring"/>' % ring(miles))
        top_lat = LAT + math.degrees(miles / 3958.8)
        labels.append('<text x="%.1f" y="%.1f" class="lab ringlab">%d MI</text>'
                      % (X(LON) + 7, Y(top_lat) + 4, miles))
    px, py = X(LON), Y(LAT)
    return """<svg class="map hasbase" viewBox="0 0 {W} {H}" role="img" aria-label="Range chart centred on the station, rings at 5, 10 and 20 miles">
<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" class="frame"/>
{images}
{rings}
{labels}
<text x="{gx2}" y="{gy2}" class="lab big">RANGE</text>
<circle cx="{px:.1f}" cy="{py:.1f}" r="10" class="pinring"/>
<circle cx="{px:.1f}" cy="{py:.1f}" r="4" class="pin"/>
<text x="{gx}" y="{top}" class="lab">{nlat}</text>
<text x="{gx}" y="{bot}" class="lab">{slat}</text>
<text x="{gx}" y="{bot2}" class="lab">{wlon}</text>
<text x="{rx}" y="{bot2}" class="lab end">{elon}</text>
<text x="{ax}" y="{ay}" class="lab attr">© OpenStreetMap contributors</text>
</svg>""".format(W=W, H=H, gx=gx, gy=gy, gw=gw, gh=gh,
                 images=chart_images("map-range", gx, gy, gw, gh),
                 rings="\n".join(rings), labels="\n".join(labels),
                 px=px, py=py, gx2=gx + 10, gy2=gy + 24,
                 top=gy - 10, bot=gy + gh + 20, bot2=gy + gh + 38, rx=gx + gw,
                 ax=gx + 6, ay=gy + gh - 8,
                 nlat=_dm(north, "N", "S"), slat=_dm(south, "N", "S"),
                 wlon=_dm(west, "E", "W"), elon=_dm(east, "E", "W"))


# The hamlet box: fixed bounds on the station's home ground (the
# roads that bound it), NOT derived from the station position.
# Keep in step with make-basemap.py.
HAMLET_SOUTH, HAMLET_NORTH = 29.596, 29.718
HAMLET_CENTRE_LON = -98.4848
HAMLET_ASPECT = 600.0 / 460.0  # this view's frame, below


def hamlet_svg():
    """Basemap mode, hamlet view: the station's home ground - the
    ground inside the roads that bound daily life - as a real map,
    the boundary drawn as a dashed line over the streets it follows,
    and the practical places marked: school, library, shops, park,
    the services a household actually walks or drives to. Bounds
    match make-basemap.py's hamlet view."""
    def merc(lat):
        phi = math.radians(lat)
        return math.log(math.tan(phi) + 1 / math.cos(phi))

    dlon = (merc(HAMLET_NORTH) - merc(HAMLET_SOUTH)) * HAMLET_ASPECT \
        / (2 * math.pi) * 360.0
    west, east = HAMLET_CENTRE_LON - dlon / 2.0, HAMLET_CENTRE_LON + dlon / 2.0
    W, H = 696, 556
    gx, gy, gw, gh = 48, 36, 600, 460

    def X(lon):
        return gx + gw * (lon - west) / (east - west)

    def Y(lat):
        top, bot = merc(HAMLET_NORTH), merc(HAMLET_SOUTH)
        return gy + gh * (top - merc(lat)) / (top - bot)

    data = load_hamlet()
    poly = ""
    boundary = data.get("boundary") or []
    if len(boundary) > 2:
        pts = " ".join("%.1f,%.1f" % (X(lon), Y(lat)) for lat, lon in boundary)
        poly = '<polygon points="%s" class="hamlet"/>' % pts
    marks = []
    for poi in data.get("pois") or []:
        try:
            px2, py2 = X(float(poi["lon"])), Y(float(poi["lat"]))
            name = str(poi["name"])
        except Exception:
            continue
        if not (gx <= px2 <= gx + gw and gy <= py2 <= gy + gh):
            continue
        if poi.get("side") == "below":
            marks.append('<circle cx="%.1f" cy="%.1f" r="3.2" class="poi"/>'
                         '<text x="%.1f" y="%.1f" class="lab poilab mid">%s</text>'
                         % (px2, py2, px2, py2 + 15, name))
        elif px2 > gx + gw - 170 or poi.get("side") == "left":
            marks.append('<text x="%.1f" y="%.1f" class="lab poilab end">%s</text>'
                         '<circle cx="%.1f" cy="%.1f" r="3.2" class="poi"/>'
                         % (px2 - 8, py2 + 3.5, name, px2, py2))
        else:
            marks.append('<circle cx="%.1f" cy="%.1f" r="3.2" class="poi"/>'
                         '<text x="%.1f" y="%.1f" class="lab poilab">%s</text>'
                         % (px2, py2, px2 + 8, py2 + 3.5, name))
    px, py = X(LON), Y(LAT)
    return """<svg class="map hasbase" viewBox="0 0 {W} {H}" role="img" aria-label="Map of the station's home ground inside its bounding roads, with practical places marked">
<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" class="frame"/>
{images}
{poly}
{marks}
<text x="{gx2}" y="{gy2}" class="lab big">HAMLET</text>
<circle cx="{px:.1f}" cy="{py:.1f}" r="10" class="pinring"/>
<circle cx="{px:.1f}" cy="{py:.1f}" r="4" class="pin"/>
<text x="{gx}" y="{top}" class="lab">{nlat}</text>
<text x="{gx}" y="{bot}" class="lab">{slat}</text>
<text x="{gx}" y="{bot2}" class="lab">{wlon}</text>
<text x="{rx}" y="{bot2}" class="lab end">{elon}</text>
<text x="{ax}" y="{ay}" class="lab attr">© OpenStreetMap contributors</text>
</svg>""".format(W=W, H=H, gx=gx, gy=gy, gw=gw, gh=gh,
                 images=chart_images("map-hamlet", gx, gy, gw, gh),
                 poly=poly, marks="\n".join(marks),
                 px=px, py=py, gx2=gx + 10, gy2=gy + 24,
                 top=gy - 10, bot=gy + gh + 20, bot2=gy + gh + 38, rx=gx + gw,
                 ax=gx + 6, ay=gy + gh - 8,
                 nlat=_dm(HAMLET_NORTH, "N", "S"), slat=_dm(HAMLET_SOUTH, "N", "S"),
                 wlon=_dm(west, "E", "W"), elon=_dm(east, "E", "W"))


def locator_svg():
    """The station's Maidenhead square as a chart: optional geography
    (towns, highways, rivers from the map-features file) under the
    subsquare grid, the station's own subsquare highlighted, and the
    station pinned at its exact position. With a basemap configured,
    the chart zooms to the station's subsquare instead - see
    subsquare_svg()."""
    if basemap_pair("map_base"):
        return subsquare_svg()
    import math
    west = math.floor(LON / 2.0) * 2.0         # square: 2 deg x 1 deg
    south = math.floor(LAT)
    W, H = 696, 424
    gx, gy, gw, gh = 48, 36, 600, 340

    def X(lon):
        return gx + gw * (lon - west) / 2.0

    def Y(lat):
        return gy + gh * (1 - (lat - south))

    def pts(coords):
        return " ".join("%.1f,%.1f" % (X(lon), Y(lat)) for lat, lon in coords)

    geo = []
    feats = load_features()
    for river in feats.get("rivers", []):
        geo.append('<polyline points="%s" class="river"/>' % pts(river["points"]))
        if river.get("label") and river.get("label_at"):
            geo.append('<text x="%.1f" y="%.1f" class="lab riverlab">%s</text>'
                       % (X(river["label_at"][1]), Y(river["label_at"][0]),
                          river["label"]))
    for hwy in feats.get("highways", []):
        cls = "hwy loop" if hwy.get("style") == "loop" else "hwy"
        geo.append('<polyline points="%s" class="%s"/>' % (pts(hwy["points"]), cls))
        if hwy.get("label") and hwy.get("label_at"):
            anchor = ' text-anchor="end"' if hwy.get("label_anchor") == "end" else ""
            geo.append('<text x="%.1f" y="%.1f" class="lab hwylab"%s>%s</text>'
                       % (X(hwy["label_at"][1]), Y(hwy["label_at"][0]),
                          anchor, hwy["label"]))
    for town in feats.get("towns", []):
        big = town.get("big")
        small = town.get("small")
        geo.append('<circle cx="%.1f" cy="%.1f" r="%s" class="town"/>'
                   % (X(town["lon"]), Y(town["lat"]),
                      "4" if big else "2" if small else "2.6"))
        geo.append('<text x="%.1f" y="%.1f" class="lab townlab%s" text-anchor="%s">%s</text>'
                   % (X(town["lon"]) + town.get("dx", 9),
                      Y(town["lat"]) + town.get("dy", 4),
                      " big2" if big else " small" if small else "",
                      town.get("anchor", "start"), town["name"]))

    lines = []
    for i in range(25):                        # subsquare hairlines
        x = gx + gw * i / 24
        lines.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="sub"/>' % (x, gy, x, gy + gh))
        y = gy + gh * i / 24
        lines.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="sub"/>' % (gx, y, gx + gw, y))
    for i in range(11):                        # square lines (10 x 10)
        x = gx + gw * i / 10
        lines.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="sqr"/>' % (x, gy, x, gy + gh))
        y = gy + gh * i / 10
        lines.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="sqr"/>' % (gx, y, gx + gw, y))

    fx = (LON - west) / 2.0                    # pin fractions in the square
    fy = LAT - south
    px, py = gx + gw * fx, gy + gh * (1 - fy)
    sub_x, sub_y = int(fx * 24), int(fy * 24)  # the station's subsquare cell
    cx, cy = gx + gw * sub_x / 24, gy + gh * (1 - (sub_y + 1) / 24)
    cw, ch = gw / 24, gh / 24
    attr = ""
    return """<svg class="map{base_cls}" viewBox="0 0 {W} {H}" role="img" aria-label="Map of Maidenhead square {grid4}, station in subsquare {sub}">
<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" class="frame"/>
{geo}
{lines}
<rect x="{cx:.1f}" y="{cy:.1f}" width="{cw:.1f}" height="{ch:.1f}" class="cell"/>
<text x="{gx2}" y="{gy2}" class="lab big">{grid4}</text>
<text x="{tx:.1f}" y="{ty:.1f}" class="lab celllab">{sub}</text>
<circle cx="{px:.1f}" cy="{py:.1f}" r="10" class="pinring"/>
<circle cx="{px:.1f}" cy="{py:.1f}" r="4" class="pin"/>
<text x="{gx}" y="{top}" class="lab">{nlat}&deg;N</text>
<text x="{gx}" y="{bot}" class="lab">{slat}&deg;N</text>
<text x="{gx}" y="{bot2}" class="lab">{wlon}&deg;W</text>
<text x="{rx}" y="{bot2}" class="lab end">{elon}&deg;W</text>
{attr}
</svg>""".format(W=W, H=H, gx=gx, gy=gy, gw=gw, gh=gh, geo="\n".join(geo),
                 lines="\n".join(lines),
                 cx=cx, cy=cy, cw=cw, ch=ch, px=px, py=py,
                 gx2=gx + 10, gy2=gy + 24, tx=cx + cw + 6, ty=cy + ch - 3,
                 top=gy - 10, bot=gy + gh + 20, bot2=gy + gh + 38, rx=gx + gw,
                 grid4=GRID[:4], sub=GRID[4:], attr=attr, base_cls="",
                 nlat=int(south + 1), slat=int(south),
                 wlon=int(-west), elon=int(-(west + 2)))


PAGE = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>document.documentElement.classList.add('js');</script>
<title>{call} — {place} · {grid}</title>
<style>
:root, html[data-theme="dark"] {{
  --base03:#002b36; --base02:#073642; --base01:#586e75; --base00:#657b83;
  --base0:#839496; --base1:#93a1a1; --accent:#268bd2; --accent2:#2aa198;
  --hot:#cb4b16; --good:#859900; --bad:#dc322f;
  --bg:var(--base03); --panel:var(--base02); --ink:var(--base0);
  --head:var(--base1); --line:var(--base01);
}}
html[data-theme="light"] {{
  --base3:#fdf6e3; --base2:#eee8d5; --base1:#93a1a1; --base01:#586e75;
  --base00:#657b83; --base0:#839496;
  --accent:#268bd2; --accent2:#2aa198; --hot:#cb4b16; --good:#859900; --bad:#dc322f;
  --bg:var(--base3); --panel:var(--base2); --ink:var(--base00);
  --head:var(--base01); --line:var(--base1);
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font-family:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
  font-size:15px; line-height:1.5; }}
main {{ max-width:760px; margin:0 auto; padding:28px 18px 40px; }}
header.top {{ display:flex; justify-content:space-between; align-items:flex-start;
  border-bottom:2px solid var(--line); padding-bottom:12px; }}
h1 {{ margin:0; font-size:34px; letter-spacing:.12em; color:var(--head); font-weight:700; }}
.sub {{ color:var(--accent2); letter-spacing:.08em; margin-top:2px; }}
button#theme {{ background:none; border:1px solid var(--line); color:var(--ink);
  font:inherit; font-size:12px; letter-spacing:.1em; padding:5px 10px; cursor:pointer; }}
.status {{ margin:14px 0 4px; font-size:17px; letter-spacing:.14em; color:var(--good); }}
.status.bad {{ color:var(--bad); }}
section {{ border:1px solid var(--line); background:var(--panel);
  padding:14px 16px 10px; margin-top:18px; }}
h2 {{ margin:0 0 10px; font-size:12px; letter-spacing:.22em; color:var(--head);
  border-bottom:1px solid var(--line); padding-bottom:6px; font-weight:700; }}
.bigtemp {{ font-size:56px; color:var(--head); line-height:1; }}
.bigtemp small {{ font-size:20px; color:var(--ink); }}
table {{ width:100%; border-collapse:collapse; }}
td {{ padding:4px 0; border-bottom:1px dotted var(--line); }}
td.k {{ color:var(--ink); letter-spacing:.06em; }}
td.v {{ text-align:right; color:var(--head); }}
tr:last-child td {{ border-bottom:none; }}
.dim {{ color:var(--base01); font-size:13px; }}
html[data-theme="light"] .dim {{ color:var(--base0); }}
a {{ color:var(--accent); }}
svg.map {{ width:100%; height:auto; display:block; }}
svg.map .frame {{ fill:none; stroke:var(--line); stroke-width:1.5; }}
svg.map .sub {{ stroke:var(--line); stroke-width:.35; opacity:.45; }}
svg.map .sqr {{ stroke:var(--line); stroke-width:.6; opacity:.7; }}
svg.map .cell {{ fill:var(--accent); opacity:.28; stroke:var(--accent); stroke-width:1; }}
svg.map .pin {{ fill:var(--hot); }}
svg.map .pinring {{ fill:none; stroke:var(--hot); stroke-width:1.5; }}
svg.map .lab {{ fill:var(--ink); font-size:12px; letter-spacing:.08em; }}
svg.map .lab.big {{ font-size:17px; fill:var(--head); }}
svg.map .lab.end {{ text-anchor:end; }}
svg.map .celllab {{ fill:var(--accent); }}
svg.map .hwy {{ fill:none; stroke:var(--ink); stroke-width:1.6; opacity:.55; }}
svg.map .hwy.loop {{ stroke-dasharray:5 4; stroke-width:1.3; opacity:.5; }}
svg.map .river {{ fill:none; stroke:var(--accent2); stroke-width:1.2; opacity:.8; }}
svg.map .town {{ fill:var(--head); }}
svg.map .townlab {{ font-size:11.5px; }}
svg.map .townlab.big2 {{ font-size:13px; fill:var(--head); letter-spacing:.14em; }}
svg.map .townlab.small {{ font-size:10px; opacity:.72; }}
svg.map .attr {{ font-size:8.5px; opacity:.6; }}
svg.map.hasbase .lab.big {{ fill:#f2ecdc; }}
svg.map.hasbase .attr {{ fill:#c9d2c6; }}
html[data-theme="light"] svg.map.hasbase .lab.big {{ fill:#073642; }}
html[data-theme="light"] svg.map.hasbase .attr {{ fill:#073642; }}
html[data-theme="dark"] svg.map .imglight {{ display:none; }}
html[data-theme="light"] svg.map .imgdark {{ display:none; }}
svg.map .hamlet {{ fill:var(--accent2); fill-opacity:.07; stroke:var(--accent2); stroke-width:1.6; stroke-dasharray:7 4; opacity:.9; }}
svg.map .poi {{ fill:var(--head); stroke:var(--bg); stroke-width:1.2; }}
svg.map .poilab {{ font-size:10.5px; letter-spacing:.05em; fill:var(--ink); paint-order:stroke; stroke-width:3px; }}
svg.map .poilab.mid {{ text-anchor:middle; }}
html[data-theme="dark"] svg.map .poilab {{ stroke:#0a303c; }}
html[data-theme="light"] svg.map .poilab {{ stroke:#e9ecdb; }}
svg.map .ring {{ fill:none; stroke:var(--accent); stroke-width:1.2; stroke-dasharray:6 4; opacity:.85; }}
svg.map .ringlab {{ fill:var(--accent); font-size:11px; }}
.chartbtns {{ display:none; gap:6px; margin:0 0 8px; }}
.js .chartbtns {{ display:flex; }}
.chartbtns button {{ background:none; border:1px solid var(--line); color:var(--ink);
  font:inherit; font-size:11px; letter-spacing:.14em; padding:4px 10px; cursor:pointer; }}
.chartbtns button.on {{ color:var(--head); border-color:var(--head); }}
.js .chartview {{ display:none; }}
.js .chartview.on {{ display:block; }}
svg.map .hwylab {{ fill:var(--accent); font-size:11px; }}
svg.map .riverlab {{ fill:var(--accent2); font-size:11px; font-style:italic; }}
svg.spark {{ width:100%; height:auto; display:block; }}
footer {{ margin-top:22px; font-size:12.5px; color:var(--base01);
  border-top:1px solid var(--line); padding-top:10px; }}
html[data-theme="light"] footer {{ color:var(--base0); }}
</style>
</head>
<body>
<main>
<header class="top">
  <div>
    <h1>{call}</h1>
    <div class="sub">{place_upper} &middot; {grid}</div>
  </div>
  <button id="theme" type="button" aria-label="Toggle light and dark">LIGHT</button>
</header>

<p class="status{status_cls}" data-bind="status">{status}</p>
<p class="dim">Last beacon <span data-bind="last_beacon">{last_beacon}</span>
&middot; telemetry seq <span data-bind="seq">{seq}</span>
&middot; page generated <span data-bind="generated">{generated}</span></p>

<section>
  <h2>WEATHER — PATIO SENSOR</h2>
  <div class="bigtemp"><span data-bind="temp_f">{temp_f}</span>&deg;F
    <small><span data-bind="temp_c">{temp_c}</span>&deg;C</small></div>
  <p class="dim">Reading <span data-bind="wx_age">{wx_age}</span> old
  &middot; Acurite-606TX &middot; battery <span data-bind="battery">{battery}</span></p>
  {spark}
</section>

<section>
  <h2>STATION TELEMETRY — AS TRANSMITTED (APRS T#)</h2>
  <table>
    <tr><td class="k">CPU TEMP</td><td class="v"><span data-bind="cpu">{cpu}</span>&deg;F</td></tr>
    <tr><td class="k">LOAD (1 MIN)</td><td class="v" data-bind="load">{load}</td></tr>
    <tr><td class="k">FREE MEMORY</td><td class="v"><span data-bind="freemem">{freemem}</span>%</td></tr>
    <tr><td class="k">DISK USED</td><td class="v"><span data-bind="disk">{disk}</span>%</td></tr>
    <tr><td class="k">UPTIME</td><td class="v"><span data-bind="uptime">{uptime}</span> DAYS</td></tr>
    <tr><td class="k">WEB SERVER</td><td class="v" data-bind="webup">{webup}</td></tr>
    <tr><td class="k">RF {rf_freq}</td><td class="v">{rf}</td></tr>
    <tr><td class="k">VOICE {voice_freq}</td><td class="v">{voice}</td></tr>
  </table>
</section>

<section>
  <h2>LOCATION — MAIDENHEAD {grid}</h2>
  {charts}
  <p class="dim">{lat_fmt}&deg; N, {lon_fmt}&deg; W — the position the beacon transmits.
  Live track: <a href="https://aprs.fi/info/a/{call}">aprs.fi</a></p>
</section>

<section>
  <h2>ABOUT THE STATION</h2>
  <p>{shellbeacon_link} {about} APRS position, weather
  and telemetry every {interval} minutes on {rf_freq}&nbsp;MHz RF and APRS-IS;
  voice announcement on {voice_freq}&nbsp;MHz. Station comment:
  &ldquo;{comment}&rdquo; Data feeds:
  <a href="latest.json">latest.json</a> &middot;
  <a href="telemetry.json">telemetry.json</a></p>
</section>

<footer>{call}{footer_extra} &middot; Solarized instrument panel, generated on the Pi</footer>
</main>
<script>
(function () {{
  var root = document.documentElement, btn = document.getElementById('theme');
  try {{
    var saved = localStorage.getItem('shellbeacon-theme');
    if (saved === 'light' || saved === 'dark') root.setAttribute('data-theme', saved);
  }} catch (e) {{}}
  function label() {{ btn.textContent = root.getAttribute('data-theme') === 'dark' ? 'LIGHT' : 'DARK'; }}
  label();
  btn.addEventListener('click', function () {{
    var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try {{ localStorage.setItem('shellbeacon-theme', next); }} catch (e) {{}}
    label();
  }});
  var cbtns = document.querySelectorAll('.chartbtns button');
  function show(view) {{
    document.querySelectorAll('.chartview').forEach(function (el) {{
      el.classList.toggle('on', el.id === 'chart-' + view);
    }});
    cbtns.forEach(function (b) {{ b.classList.toggle('on', b.getAttribute('data-view') === view); }});
  }}
  if (cbtns.length) {{
    var initial = cbtns[0].getAttribute('data-view');
    try {{
      var savedView = localStorage.getItem('shellbeacon-chart');
      cbtns.forEach(function (b) {{ if (b.getAttribute('data-view') === savedView) initial = savedView; }});
    }} catch (e) {{}}
    show(initial);
    cbtns.forEach(function (b) {{
      b.addEventListener('click', function () {{
        show(b.getAttribute('data-view'));
        try {{ localStorage.setItem('shellbeacon-chart', b.getAttribute('data-view')); }} catch (e) {{}}
      }});
    }});
  }}
  function set(name, value) {{
    var el = document.querySelector('[data-bind="' + name + '"]');
    if (el && value !== null && value !== undefined) el.textContent = value;
  }}
  function refresh() {{
    fetch('telemetry.json', {{ cache: 'no-store' }}).then(function (r) {{ return r.json(); }})
      .then(function (d) {{
        if (d.wx) {{ set('temp_f', d.wx.temperature_f.toFixed(1)); set('temp_c', d.wx.temperature_c.toFixed(1)); }}
        if (d.telemetry) {{
          set('cpu', d.telemetry.cpu_temp_f); set('load', d.telemetry.load.toFixed(1));
          set('freemem', d.telemetry.free_mem_pct); set('disk', d.telemetry.disk_used_pct);
          set('uptime', d.telemetry.uptime_days); set('webup', d.telemetry.web_up ? 'UP' : 'DOWN');
          set('seq', '#' + d.telemetry.seq); set('last_beacon', d.telemetry.last_beacon);
        }}
        set('status', d.status); set('generated', d.generated);
      }}).catch(function () {{}});
  }}
  setInterval(refresh, 60000);
}})();
</script>
</body>
</html>
"""


def age_text(delta):
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "under a minute"
    if mins == 1:
        return "1 minute"
    if mins < 90:
        return "%d minutes" % mins
    return "%.1f hours" % (mins / 60.0)


def main():
    now = datetime.now().astimezone()
    wx_raw, wx, wx_when = read_wx()
    tele, last_pos = read_journal()

    temp_c = wx["temperature_C"] if wx else None
    temp_f = temp_c * 9 / 5 + 32 if temp_c is not None else None
    wx_age = (now - wx_when) if wx_when else None
    wx_fresh = wx_age is not None and wx_age.total_seconds() <= WX_STALE_MIN * 60

    rows = update_history(wx_when, temp_f) if (wx_when and temp_f is not None) else []

    beacon_when = (tele or {}).get("when") or last_pos
    beacon_age = (now - beacon_when) if beacon_when else None
    beacon_fresh = beacon_age is not None and beacon_age.total_seconds() <= BEACON_STALE_MIN * 60

    down = []
    if not wx_fresh:
        down.append("WEATHER SYSTEM IS DOWN")
    if tele and not tele["web_up"]:
        down.append("WEB CHECK FAILING")
    if not beacon_fresh:
        down.append("BEACON OVERDUE")
    status = "ALL STATION SYSTEMS NORMAL" if not down else " — ".join(down)

    rf = "ON" if os.path.exists(os.path.join(BEACON_DIR, "rf.enabled")) else "OFF"
    voice = "ON" if os.path.exists(os.path.join(BEACON_DIR, "voice.enabled")) else "OFF"

    def fmt(value, spec="%.1f"):
        return (spec % value) if value is not None else "—"

    if CONF["repo_url"]:
        shellbeacon_link = '<a href="%s">ShellBeacon</a>' % CONF["repo_url"]
    else:
        shellbeacon_link = "ShellBeacon"

    views = []
    if basemap_pair("map_base"):
        views.append(("subsquare", "SUBSQUARE", subsquare_svg()))
    if basemap_pair("map_hamlet"):
        views.append(("hamlet", "HAMLET", hamlet_svg()))
    if basemap_pair("map_square"):
        views.append(("square", "SQUARE", square_svg()))
    if basemap_pair("map_range"):
        views.append(("range", "RANGE", range_svg()))
    if not views:
        views.append(("square", "SQUARE", locator_svg()))
    charts = ""
    if len(views) > 1:
        charts += ('<div class="chartbtns" role="group" aria-label="Chart view">'
                   + "".join('<button type="button" data-view="%s">%s</button>'
                             % (key, label) for key, label, _ in views) + "</div>")
    charts += "".join('<div class="chartview" id="chart-%s">%s</div>'
                      % (key, svg) for key, _, svg in views)

    page = PAGE.format(
        call=CALL,
        place=CONF["place"],
        place_upper=CONF["place"].upper(),
        grid=GRID,
        status=status,
        status_cls="" if not down else " bad",
        last_beacon=beacon_when.strftime("%H:%M %Z") if beacon_when else "—",
        seq=("#%d" % tele["seq"]) if tele else "—",
        generated=now.strftime("%H:%M:%S %Z"),
        temp_f=fmt(temp_f), temp_c=fmt(temp_c),
        wx_age=age_text(wx_age) if wx_age else "—",
        battery=("OK" if wx and wx.get("battery_ok") else "LOW") if wx else "—",
        spark=sparkline(rows),
        cpu=tele["cpu_temp_f"] if tele else "—",
        load=fmt(tele["load"]) if tele else "—",
        freemem=tele["free_mem_pct"] if tele else "—",
        disk=tele["disk_used_pct"] if tele else "—",
        uptime=tele["uptime_days"] if tele else "—",
        webup=("UP" if tele["web_up"] else "DOWN") if tele else "—",
        rf=rf, voice=voice,
        rf_freq=CONF["rf_freq"], voice_freq=CONF["voice_freq"],
        charts=charts,
        lat_fmt="%.4f" % abs(LAT), lon_fmt="%.4f" % abs(LON),
        shellbeacon_link=shellbeacon_link,
        about=CONF["about"],
        interval=CONF["beacon_interval_min"],
        comment=CONF["comment"],
        footer_extra=CONF["footer_extra"],
    )

    feed = {
        "generated": now.strftime("%H:%M:%S %Z"),
        "generated_iso": now.isoformat(),
        "station": {"call": CALL, "grid": GRID, "lat": LAT, "lon": LON,
                    "place": CONF["place"], "comment": CONF["comment"],
                    "rf": rf == "ON", "voice": voice == "ON"},
        "status": status,
        "wx": ({"temperature_c": temp_c, "temperature_f": round(temp_f, 1),
                "time": wx["time"], "age_min": int(wx_age.total_seconds() // 60),
                "battery_ok": bool(wx.get("battery_ok")),
                "sensor": wx.get("model")} if wx else None),
        "telemetry": ({"seq": tele["seq"], "cpu_temp_f": tele["cpu_temp_f"],
                       "load": tele["load"], "free_mem_pct": tele["free_mem_pct"],
                       "disk_used_pct": tele["disk_used_pct"],
                       "uptime_days": tele["uptime_days"], "web_up": tele["web_up"],
                       "last_beacon": beacon_when.strftime("%H:%M %Z"),
                       "last_beacon_iso": beacon_when.isoformat()} if tele else None),
    }

    os.makedirs(WEBROOT, exist_ok=True)
    with open(os.path.join(WEBROOT, "index.html"), "w") as fh:
        fh.write(page)
    with open(os.path.join(WEBROOT, "telemetry.json"), "w") as fh:
        json.dump(feed, fh, indent=1)
    if wx_raw:
        with open(os.path.join(WEBROOT, "latest.json"), "wb") as fh:
            fh.write(wx_raw)
    for conf_key, name in (("map_base", "map-base"), ("map_hamlet", "map-hamlet"),
                           ("map_square", "map-square"),
                           ("map_range", "map-range")):
        pair = basemap_pair(conf_key)
        if pair:
            shutil.copyfile(pair[0], os.path.join(WEBROOT, name + ".jpg"))
            shutil.copyfile(pair[1], os.path.join(WEBROOT, name + "-light.jpg"))
    print("%s page generated: %s" % (CALL, status))


if __name__ == "__main__":
    sys.exit(main())
