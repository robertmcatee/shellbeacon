#!/usr/bin/env python3
# make-basemap.py - build the real-map bases for a ShellBeacon page.
#
# Fetches OpenStreetMap tiles ONCE per view, stitches and crops them
# to the view's exact bounds, and re-renders each as TWO JPEGs in the
# page's Solarized scheme: a night chart (for the dark theme) and a
# light chart (for the light theme). The status page then serves real
# maps as local files - no tile calls, no trackers at page-view time -
# and the generator superimposes the Maidenhead lines, highlights and
# pin over them.
#
# Views:
#   subsquare  the station's 6-char cell (5' x 2.5'), zoom 14
#   square     the station's 4-char square (2 x 1 deg), zoom 9
#   range      station-centred, 0.64 deg of latitude, zoom 10 -
#              sized so 5/10/20-mile range rings fit the frame
#   hamlet     the station's home ground, a fixed box on the roads
#              that bound daily life, zoom 14
#
# Usage:  make-basemap.py VIEW LAT LON OUTBASE
#   writes OUTBASE.jpg (dark) and OUTBASE-light.jpg (light)
# Example: make-basemap.py subsquare 29.6585 -98.4710 /usr/local/etc/map-base
#
# Map data (c) OpenStreetMap contributors - keep the attribution the
# page prints in each chart's corner. Tiles per view run from eight
# (square) to about thirty (subsquare); be polite, run it once.

import math
import sys
import urllib.request
from io import BytesIO

from PIL import Image

UA = {"User-Agent": "shellbeacon-page make-basemap (station status page; "
                    "https://github.com/robertmcatee/shellbeacon-telemetry)"}
RANGE_LAT_SPAN = 0.64          # keep in step with shellbeacon-page.py
# The hamlet box: fixed bounds on the station's home ground (the
# roads that bound it), NOT derived from the station position.
# Keep in step with shellbeacon-page.py.
HAMLET_SOUTH, HAMLET_NORTH = 29.596, 29.718
HAMLET_CENTRE_LON = -98.4848
HAMLET_ASPECT = 600.0 / 460.0  # the generator's hamlet frame

# The club view: station-centred, the 5-mile service ring's frame.
CLUB_LAT_SPAN = 0.16           # keep in step with shellbeacon-page.py
CLUB_ASPECT = 600.0 / 480.0    # the generator's club frame


def tile_frac(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    phi = math.radians(lat)
    y = (1.0 - math.log(math.tan(phi) + 1 / math.cos(phi)) / math.pi) / 2.0 * n
    return x, y


def merc(lat):
    phi = math.radians(lat)
    return math.log(math.tan(phi) + 1 / math.cos(phi))


def bounds(view, lat, lon):
    """(west, south, east, north, zoom) for a view of the station."""
    if view == "subsquare":
        sq_w = math.floor(lon / 2.0) * 2.0
        sq_s = math.floor(lat)
        west = sq_w + int((lon - sq_w) / 2.0 * 24) * (2.0 / 24)
        south = sq_s + int((lat - sq_s) * 24) * (1.0 / 24)
        return west, south, west + 2.0 / 24, south + 1.0 / 24, 14
    if view == "square":
        west = math.floor(lon / 2.0) * 2.0
        south = math.floor(lat)
        return west, south, west + 2.0, south + 1.0, 9
    if view == "range":
        south = lat - RANGE_LAT_SPAN / 2.0
        north = lat + RANGE_LAT_SPAN / 2.0
        # longitude span from the Mercator aspect of the 600x340 frame
        dlon = (merc(north) - merc(south)) * (600.0 / 340.0) / (2 * math.pi) * 360.0
        return lon - dlon / 2.0, south, lon + dlon / 2.0, north, 10
    if view == "club":
        south = lat - CLUB_LAT_SPAN / 2.0
        north = lat + CLUB_LAT_SPAN / 2.0
        dlon = (merc(north) - merc(south)) * CLUB_ASPECT \
            / (2 * math.pi) * 360.0
        return lon - dlon / 2.0, south, lon + dlon / 2.0, north, 14
    if view == "hamlet":
        dlon = (merc(HAMLET_NORTH) - merc(HAMLET_SOUTH)) * HAMLET_ASPECT \
            / (2 * math.pi) * 360.0
        return (HAMLET_CENTRE_LON - dlon / 2.0, HAMLET_SOUTH,
                HAMLET_CENTRE_LON + dlon / 2.0, HAMLET_NORTH, 14)
    raise SystemExit("view must be subsquare, square, range, hamlet or club")


def fetch_crop(west, south, east, north, zoom):
    fx0, fy0 = tile_frac(north, west, zoom)
    fx1, fy1 = tile_frac(south, east, zoom)
    tx0, ty0, tx1, ty1 = int(fx0), int(fy0), int(fx1), int(fy1)
    sheet = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            url = "https://tile.openstreetmap.org/%d/%d/%d.png" % (zoom, tx, ty)
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=30) as resp:
                tile = Image.open(BytesIO(resp.read())).convert("RGB")
            sheet.paste(tile, ((tx - tx0) * 256, (ty - ty0) * 256))
    return sheet.crop((int((fx0 - tx0) * 256), int((fy0 - ty0) * 256),
                       int((fx1 - tx0) * 256), int((fy1 - ty0) * 256)))


def render(base, dark_mode):
    """Solarized duotone. OSM's luminance band is stretched first
    (labels sit ~90-120, land ~242) so names and roads separate; the
    night chart inverts it (pale land -> deep teal, dark features ->
    bright ink), the light chart keeps features dark on pale land."""
    raw = base.convert("L")
    gray = raw.point(lambda v: max(0, min(255, int((v - 60) * 255 / 185))))
    if dark_mode:
        dark, mid, light = (8, 46, 58), (52, 88, 98), (190, 203, 190)
    else:
        dark, mid, light = (13, 43, 54), (110, 132, 130), (226, 230, 214)
    lut = []
    for i in range(256):
        v = i / 255.0
        if dark_mode:
            v = 1.0 - v
        if v < 0.5:
            t, a, b = v * 2, dark, mid
        else:
            t, a, b = (v - 0.5) * 2, mid, light
        lut.append(tuple(int(a[c] + (b[c] - a[c]) * t) for c in range(3)))
    return Image.merge("RGB", [gray.point([c[0] for c in lut]),
                               gray.point([c[1] for c in lut]),
                               gray.point([c[2] for c in lut])])


def main():
    if len(sys.argv) != 5:
        print(__doc__)
        return 2
    view, lat, lon, outbase = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
    west, south, east, north, zoom = bounds(view, lat, lon)
    base = fetch_crop(west, south, east, north, zoom)
    print("%s: cropped %dx%d px at z%d" % (view, base.size[0], base.size[1], zoom))
    render(base, True).save(outbase + ".jpg", quality=82)
    render(base, False).save(outbase + "-light.jpg", quality=82)
    print("wrote %s.jpg and %s-light.jpg" % (outbase, outbase))
    return 0


if __name__ == "__main__":
    sys.exit(main())
