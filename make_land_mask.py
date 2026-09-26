#!/usr/bin/env python3
"""Welche Teilzellen des Katalogs ueberhaupt Land enthalten.

Einmal von Hand erzeugt und ins Repo gelegt, nicht bei jedem Lauf: die
Kuesten wandern nicht. Grundlage sind die groben Terrarium-Kacheln von AWS
Open Data (Zoom 4, ~10 km je Pixel am Aequator). Dort steht das Meer mit
negativer Hoehe (Bathymetrie), Land darueber.

Am 25.09.2026 hat der erste Lauf 40 Minuten mit Abfragen ueber dem Atlantik
verbracht, bekam "zu viel" und hoerte vor dem Rheintal auf. Meer hat keine
Aussichtspunkte; Inseln wie Madeira oder die Azoren haben Landpixel und
bleiben drin.

Aufruf: make_land_mask.py > land_cells.json
"""

import io
import json
import math
import sys
import urllib.request

from PIL import Image

from build_catalog import CELL_DEGREES, COLS, LAT_MIN, ROWS, SUB_PER_SIDE, USER_AGENT, cell_name

ZOOM = 4
TILE = 256
URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
# Knapp ueber null, damit Rauschen im Kuestenpixel nicht als Land zaehlt.
LAND_METERS = 1.0

tiles = {}


def tile(x, y):
    if (x, y) not in tiles:
        request = urllib.request.Request(URL.format(z=ZOOM, x=x, y=y), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            image = Image.open(io.BytesIO(response.read())).convert("RGB")
        tiles[(x, y)] = image.load()
    return tiles[(x, y)]


def global_pixel(lat, lon):
    n = 2 ** ZOOM * TILE
    lat = max(min(lat, 85.0), -85.0)
    x = (lon + 180) / 360 * n
    y = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def has_land(south, west, north, east):
    x0, y0 = global_pixel(north, west)
    x1, y1 = global_pixel(south, east)
    limit = 2 ** ZOOM * TILE - 1
    for py in range(max(int(y0), 0), min(int(math.ceil(y1)), limit) + 1):
        for px in range(max(int(x0), 0), min(int(math.ceil(x1)), limit) + 1):
            r, g, b = tile(px // TILE, py // TILE)[px % TILE, py % TILE]
            if r * 256 + g + b / 256 - 32768 >= LAND_METERS:
                return True
    return False


def main():
    step = CELL_DEGREES / SUB_PER_SIDE
    land = {}
    for row in range(ROWS):
        for col in range(COLS):
            south = LAT_MIN + row * CELL_DEGREES
            west = -180 + col * CELL_DEGREES
            subs = []
            for i in range(SUB_PER_SIDE):
                for j in range(SUB_PER_SIDE):
                    s = south + i * step
                    w = west + j * step
                    if has_land(s, w, s + step, w + step):
                        subs.append(i * SUB_PER_SIDE + j)
            if subs:
                land[cell_name(row, col)] = subs
        print(f"Zeile {row + 1}/{ROWS}", file=sys.stderr)
    json.dump({"subPerSide": SUB_PER_SIDE, "zoom": ZOOM, "cells": land}, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
