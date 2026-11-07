#!/usr/bin/env python3
# make-basemap.py - build a real map base for a ShellBeacon status page.
#
# Fetches OpenStreetMap tiles ONCE for the station's Maidenhead
# SUBSQUARE (the 6-character cell, 5' x 2.5'), stitches and crops them
# to the cell's exact bounds, and re-renders the result as a muted
# Solarized-dark duotone JPEG, so the status page can serve a real
# street-level map as a single local file - no tile calls, no
# trackers at page-view time. The page generator superimposes the
# next Maidenhead level's grid, the extended-square highlight and
# the station pin over it.
#
# Usage:  make-basemap.py LAT LON OUT.jpg
# Example: make-basemap.py 29.6585 -98.4710 /usr/local/etc/map-base.jpg
#
# Map data (c) OpenStreetMap contributors - keep the attribution the
# page prints in the chart's corner. Tiles are fetched at zoom 14 (a
# subsquare is about thirty tiles); be polite, run it once per station.

import math
import sys
import urllib.request
from io import BytesIO

from PIL import Image

ZOOM = 14
UA = {"User-Agent": "shellbeacon-page make-basemap (station status page; "
                    "https://github.com/robertmcatee/shellbeacon-telemetry)"}


def tile_frac(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    phi = math.radians(lat)
    y = (1.0 - math.log(math.tan(phi) + 1 / math.cos(phi)) / math.pi) / 2.0 * n
    return x, y


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    lat, lon, out = float(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
    sq_west = math.floor(lon / 2.0) * 2.0       # the 4-char square first
    sq_south = math.floor(lat)
    sub_x = int((lon - sq_west) / 2.0 * 24)     # then the 6-char subsquare
    sub_y = int((lat - sq_south) * 24)
    west = sq_west + sub_x * (2.0 / 24)
    south = sq_south + sub_y * (1.0 / 24)
    east, north = west + 2.0 / 24, south + 1.0 / 24

    fx0, fy0 = tile_frac(north, west, ZOOM)   # top-left, fractional tiles
    fx1, fy1 = tile_frac(south, east, ZOOM)   # bottom-right
    tx0, ty0, tx1, ty1 = int(fx0), int(fy0), int(fx1), int(fy1)
    sheet = Image.new("RGB", ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            url = "https://tile.openstreetmap.org/%d/%d/%d.png" % (ZOOM, tx, ty)
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=30) as resp:
                tile = Image.open(BytesIO(resp.read())).convert("RGB")
            sheet.paste(tile, ((tx - tx0) * 256, (ty - ty0) * 256))
            print("tile", url)
    left = int((fx0 - tx0) * 256)
    top = int((fy0 - ty0) * 256)
    right = int((fx1 - tx0) * 256)
    bottom = int((fy1 - ty0) * 256)
    base = sheet.crop((left, top, right, bottom))
    print("cropped subsquare: %dx%d px" % base.size)

    # Solarized-dark duotone: stretch the luminance band OSM actually
    # uses (labels sit ~90-120, land ~242) across the full range first,
    # so town names and roads separate, then map base03 -> base01 ->
    # base1. Roads and towns read as an instrument chart, not a cartoon.
    raw = base.convert("L")
    gray = raw.point(lambda v: max(0, min(255, int((v - 60) * 255 / 185))))
    dark, mid, light = (8, 46, 58), (52, 88, 98), (190, 203, 190)
    # Invert for a night chart: OSM's dark features (labels, road
    # casings) become the bright ink, the pale land becomes deep teal,
    # so the base sits naturally in the dark instrument panel.
    lut = []
    for i in range(256):
        v = 1.0 - i / 255.0
        if v < 0.5:
            t, a, b = v * 2, dark, mid
        else:
            t, a, b = (v - 0.5) * 2, mid, light
        lut.append(tuple(int(a[c] + (b[c] - a[c]) * t) for c in range(3)))
    duo = Image.merge("RGB", [gray.point([c[0] for c in lut]),
                              gray.point([c[1] for c in lut]),
                              gray.point([c[2] for c in lut])])
    duo.save(out, quality=82)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
