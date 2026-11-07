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
import os
import re
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
    "map_features": "",                   # optional JSON: towns/highways/rivers
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


def locator_svg():
    """The station's Maidenhead square as a chart: optional geography
    (towns, highways, rivers from the map-features file) under the
    subsquare grid, the station's own subsquare highlighted, and the
    station pinned at its exact position."""
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
        geo.append('<polyline points="%s" class="hwy"/>' % pts(hwy["points"]))
        if hwy.get("label") and hwy.get("label_at"):
            anchor = ' text-anchor="end"' if hwy.get("label_anchor") == "end" else ""
            geo.append('<text x="%.1f" y="%.1f" class="lab hwylab"%s>%s</text>'
                       % (X(hwy["label_at"][1]), Y(hwy["label_at"][0]),
                          anchor, hwy["label"]))
    for town in feats.get("towns", []):
        big = town.get("big")
        geo.append('<circle cx="%.1f" cy="%.1f" r="%s" class="town"/>'
                   % (X(town["lon"]), Y(town["lat"]), "4" if big else "2.6"))
        geo.append('<text x="%.1f" y="%.1f" class="lab townlab%s" text-anchor="%s">%s</text>'
                   % (X(town["lon"]) + town.get("dx", 9),
                      Y(town["lat"]) + town.get("dy", 4),
                      " big2" if big else "", town.get("anchor", "start"),
                      town["name"]))

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
    return """<svg class="map" viewBox="0 0 {W} {H}" role="img" aria-label="Map of Maidenhead square {grid4}, station in subsquare {sub}">
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
</svg>""".format(W=W, H=H, gx=gx, gy=gy, gw=gw, gh=gh, geo="\n".join(geo),
                 lines="\n".join(lines),
                 cx=cx, cy=cy, cw=cw, ch=ch, px=px, py=py,
                 gx2=gx + 10, gy2=gy + 24, tx=cx + cw + 6, ty=cy + ch - 3,
                 top=gy - 10, bot=gy + gh + 20, bot2=gy + gh + 38, rx=gx + gw,
                 grid4=GRID[:4], sub=GRID[4:],
                 nlat=int(south + 1), slat=int(south),
                 wlon=int(-west), elon=int(-(west + 2)))


PAGE = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
svg.map .river {{ fill:none; stroke:var(--accent2); stroke-width:1.2; opacity:.8; }}
svg.map .town {{ fill:var(--head); }}
svg.map .townlab {{ font-size:11.5px; }}
svg.map .townlab.big2 {{ font-size:13px; fill:var(--head); letter-spacing:.14em; }}
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
  {map}
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
        map=locator_svg(),
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
    print("%s page generated: %s" % (CALL, status))


if __name__ == "__main__":
    sys.exit(main())
