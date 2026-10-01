#!/usr/bin/env python3
"""LIGHT Spot-Katalog.

Baut aus den OSM-Komplettdaten (Geofabrik, einmal die Woche) einen weltweiten
Katalog fotogener Orte (Aussichtspunkte, Gipfel, Felsen, Burgen, Leuchttuerme,
Straende) und legt ihn als 5-Grad-Kacheln ab. Die App laedt nur die Kacheln um
den Suchort, statt bei jeder Suche die ehrenamtlichen Overpass-Server zu fragen.

Bis 01.10.2026 kam der Katalog Zelle fuer Zelle aus Overpass. Die Server waren
so ausgelastet (504, Timeouts, 429), dass am Tag nur rund 40 von 1129
Landzellen durchkamen. Die Komplettdaten bringen die ganze Welt in einem Lauf.

Fuer Aussichtspunkte, Aussichtstuerme und Gipfel steht die Standpunkthoehe
gleich mit drin, aus Mapterhorn, und der Standpunkt sitzt auf dem hoechsten
Punkt im Umkreis von 100 m (die Karte setzt Aussichtspunkte gern an den Hang).
Das kostet einmal hier statt bei jedem Nutzer, und taeglich nur ein Budget:
die taeglichen Laeufe tragen fehlende Hoehen nach.

Aufrufe:
  build_catalog.py --site-relations spots.opl spots.geojsonseq
  build_catalog.py --split-osm spots.geojsonseq --out osm/europe
  build_catalog.py --assemble-osm osm --previous prev --out release
  build_catalog.py --band 0 --bands 6 --previous prev --out out
  build_catalog.py --assemble bands --previous prev --out release
  build_catalog.py --status release
  build_catalog.py --selftest

Kachelformat: JSON, roh-deflate-komprimiert (wbits=-15), damit iOS es mit
NSData.decompressed(using: .zlib) ohne Zusatzbibliothek oeffnet.
"""

import argparse
import datetime
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

CELL_DEGREES = 5
LAT_MIN = -60
LAT_MAX = 80
ROWS = (LAT_MAX - LAT_MIN) // CELL_DEGREES
COLS = 360 // CELL_DEGREES
FORMAT_VERSION = 1
MAX_RELEASE_ASSETS = 1000

USER_AGENT = "LIGHT-SpotCatalog/1.0 (+https://github.com/stheil777/light-spot-catalog)"

MAPTERHORN_URL = "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp"
MAPTERHORN_ZOOMS = (13, 12)
MAPTERHORN_TILE = 512
EDGE_RADIUS_METERS = 100.0
EDGE_STEP_METERS = 10.0
EDGE_MIN_GAIN_METERS = 10.0

# Grobe Hoehen von AWS Open Data (von Amazon gesponsert, kein Budget noetig).
# Sie entscheiden, ob ein Spot ueberhaupt am Hang liegt: im Flachen ist die
# grobe Hoehe schon richtig, nur an Kanten braucht es Mapterhorn.
TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TERRARIUM_ZOOM = 12
TERRARIUM_TILE = 256
FLAT_RELIEF_METERS = 20.0

# Rheintal: wo die App benutzt wird, kommt zuerst dran.
HOME = (50.17, 7.70)

KEEP_TAGS = {
    "tourism", "man_made", "natural", "tower:type", "historic",
    "access", "name", "alt_name", "wikidata", "wikipedia", "direction",
    "addr:city", "is_in", "ele",
    "name:de", "name:en", "name:es", "name:fr", "name:it", "name:pt", "name:nl",
}
BLOCKED_ACCESS = {"private", "no", "customers"}



# Gleiche Auswahl wie in osm.yml (osmium tags-filter) und wie frueher die
# Overpass-Abfrage. osmium filtert grob, is_spot genau.
def is_spot(tags):
    natural = tags.get("natural")
    return (tags.get("tourism") == "viewpoint"
            or tags.get("man_made") == "lighthouse"
            or natural == "beach"
            or (tags.get("man_made") == "tower" and tags.get("tower:type") == "observation")
            or (natural in ("peak", "cliff", "rock") and "name" in tags)
            or (tags.get("historic") in ("castle", "ruins") and "name" in tags))


def log(*parts):
    print(*parts, flush=True)


# ---------------------------------------------------------------- Zellen

def cell_name(row, col):
    return f"c{row:02d}_{col:02d}"


def cell_bbox(row, col):
    south = LAT_MIN + row * CELL_DEGREES
    west = -180 + col * CELL_DEGREES
    return south, west, south + CELL_DEGREES, west + CELL_DEGREES


def cell_of(lat, lon):
    # % COLS: Laenge 180 gehoert zur Zelle bei -180.
    return int((lat - LAT_MIN) // CELL_DEGREES), int((lon + 180) // CELL_DEGREES) % COLS


def all_cells():
    return [cell_name(row, col) for row in range(ROWS) for col in range(COLS)]


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def home_distance(name):
    south, west, north, east = cell_bbox(int(name[1:3]), int(name[4:6]))
    return distance_km(HOME[0], HOME[1], (south + north) / 2, (west + east) / 2)


# ---------------------------------------------------------------- OSM

def osm_object(feature_id):
    """osmium-ID -> (typ, id). Flaechen heissen a<2*weg> bzw. a<2*relation+1>."""
    kind, number = feature_id[0], int(feature_id[1:])
    if kind == "a":
        return ("way" if number % 2 == 0 else "relation"), number // 2
    return {"n": "node", "w": "way", "r": "relation"}[kind], number


def bbox_centre(geometry):
    """Mitte des Rahmens, wie Overpass mit `out center`.

    ponytail: Flaechen ueber die Datumsgrenze landen falsch (Mitte bei 0 Grad);
    betrifft eine Handvoll Inseln im Pazifik.
    """
    lats, lons = [], []
    stack = [geometry["coordinates"]]
    while stack:
        item = stack.pop()
        if isinstance(item[0], (int, float)):
            lons.append(item[0])
            lats.append(item[1])
        else:
            stack.extend(item)
    return (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2


def compact(kind, number, lat, lon, tags):
    tags = {k: v for k, v in tags.items() if k in KEEP_TAGS and isinstance(v, str)}
    if tags.get("access") in BLOCKED_ACCESS:
        return None
    return {"t": kind, "i": number, "a": round(lat, 6), "o": round(lon, 6), "g": tags}


def split_osm(path, out_dir):
    """osmium-Export einer Region -> je Zelle eine JSON-Liste von Spots."""
    cells = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feature = json.loads(line)
            tags = feature.get("properties") or {}
            if not is_spot(tags) or not feature.get("geometry"):
                continue
            lat, lon = bbox_centre(feature["geometry"])
            if not (LAT_MIN <= lat < LAT_MAX):
                continue
            spot = compact(*osm_object(feature["id"]), lat, lon, tags)
            if spot is None:
                continue
            # Ein geschlossener Weg kommt als Linie und als Flaeche: einmal behalten.
            cells.setdefault(cell_name(*cell_of(lat, lon)), {})[spot_key(spot)] = spot
    os.makedirs(out_dir, exist_ok=True)
    for name, spots in cells.items():
        with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as handle:
            json.dump(list(spots.values()), handle, ensure_ascii=False, separators=(",", ":"))
    log(f"{path}: {sum(len(s) for s in cells.values())} Spots in {len(cells)} Zellen")


def opl_fields(line):
    kind, *fields = line.rstrip("\n").split(" ")
    return kind[0], int(kind[1:]), {field[0]: field[1:] for field in fields if field}


def opl_unescape(text):
    return re.sub(r"%([0-9a-fA-F]+)%", lambda m: chr(int(m.group(1), 16)), text)


def site_relations(opl_path, geo_path):
    """Relationen, die keine Flaeche sind (Burgen als type=site), als Punkte
    an den Export anhaengen.

    osmium export kennt Relationen nur als Multipolygon. Am Rheintal fehlten
    so am 01.10.2026 Schloss Buerresheim und Feste Kaiser Franz. Mitte des
    Rahmens aus den Mitgliedern, wie Overpass mit `out center`. Erwartet OPL
    aus `osmium add-locations-to-ways -n`.
    """
    wanted = {}
    with open(opl_path, encoding="utf-8") as handle:
        for line in handle:
            if not line.startswith("r"):
                continue
            _, number, fields = opl_fields(line)
            tags = dict(opl_unescape(pair).split("=", 1) for pair in fields.get("T", "").split(",")
                        if "=" in pair)
            if tags.get("type") == "multipolygon" or not is_spot(tags):
                continue
            members = [m.split("@")[0] for m in fields.get("M", "").split(",") if m[:1] in ("n", "w")]
            wanted[number] = (tags, set(members))
    needed = set().union(*(members for _, members in wanted.values())) if wanted else set()
    points = {}
    with open(opl_path, encoding="utf-8") as handle:
        for line in handle:
            if line[:1] not in ("n", "w"):
                continue
            kind, number, fields = opl_fields(line)
            if f"{kind}{number}" not in needed:
                continue
            raw = [fields] if kind == "n" else [
                {"x": node.split("x")[1].split("y")[0], "y": node.split("y")[1]}
                for node in fields.get("N", "").split(",") if "x" in node and "y" in node]
            points[f"{kind}{number}"] = [[float(p["x"]), float(p["y"])] for p in raw
                                         if p.get("x") and p.get("y")]
    added = 0
    with open(geo_path, "a", encoding="utf-8") as out:
        for number, (tags, members) in wanted.items():
            coordinates = [c for m in members for c in points.get(m, [])]
            if coordinates:
                out.write(json.dumps({"type": "Feature", "id": f"r{number}", "properties": tags,
                                      "geometry": {"type": "MultiPoint", "coordinates": coordinates}},
                                     ensure_ascii=False) + "\n")
                added += 1
    log(f"{opl_path}: {added} von {len(wanted)} Relationen ohne Flaeche als Punkt")


def same_place(a, b):
    return abs(a["a"] - b["a"]) < 1e-4 and abs(a["o"] - b["o"]) < 1e-4


def assemble_osm(osm_dir, previous_dir, out_dir):
    """Alle Regionen zu einem Katalog. Jede Zelle der Welt bekommt einen Stand."""
    parts = {}
    for root, _, files in os.walk(osm_dir):
        for file in files:
            if file.endswith(".json"):
                parts.setdefault(file[:-5], []).append(os.path.join(root, file))
    if not parts:
        sys.exit("Keine OSM-Daten")
    os.makedirs(out_dir, exist_ok=True)
    counts = dict.fromkeys(all_cells(), 0)
    reused = 0
    for name, paths in sorted(parts.items()):
        spots = {}
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                for spot in json.load(handle):
                    spots[spot_key(spot)] = spot  # Regionen ueberlappen am Rand
        # Hoehen aus dem letzten Katalog, solange der Ort nicht gewandert ist.
        former = {spot_key(s): s for s in (previous_spots(previous_dir, name) or {}).get("spots", [])}
        for key, spot in spots.items():
            old = former.get(key)
            if old and "h" in old and same_place(old, spot):
                spot["h"] = old["h"]
                if "s" in old:
                    spot["s"] = old["s"]
                reused += 1
        write_cell_file(os.path.join(out_dir, f"{name}.bin"),
                        {"v": FORMAT_VERSION, "cell": name, "spots": list(spots.values())})
        counts[name] = len(spots)
    log(f"Hoehen uebernommen: {reused}")
    write_manifest(out_dir, counts, datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))


def write_manifest(out_dir, counts, osm_date):
    # Ein Release traegt hoechstens 1000 Dateien. Darueber fallen die Zellen
    # mit den wenigsten Spots aus dem Manifest; dort fragt die App Overpass.
    with_spots = sorted((n for n, c in counts.items() if c), key=lambda n: -counts[n])
    for name in with_spots[MAX_RELEASE_ASSETS - 1:]:
        log(f"  {name}: {counts[name]} Spots, kein Platz im Release")
        del counts[name]
        os.remove(os.path.join(out_dir, f"{name}.bin"))
    manifest = {
        "version": FORMAT_VERSION,
        "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cellDegrees": CELL_DEGREES,
        "latMin": LAT_MIN,
        "latMax": LAT_MAX,
        "osm": osm_date,
        "cells": dict(sorted(counts.items())),
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, separators=(",", ":"))
    log(f"Katalog: {len(counts)} Zellen, {sum(1 for c in counts.values() if c)} mit Spots, "
        f"{sum(counts.values())} Spots gesamt")


# ---------------------------------------------------------------- Mapterhorn

class Terrain:
    """Hoehen aus Kacheln im Terrarium-Format, mit Plattencache und Budget."""

    def __init__(self, cache_dir, budget, url=MAPTERHORN_URL, tile_size=MAPTERHORN_TILE,
                 zooms=MAPTERHORN_ZOOMS, suffix="webp"):
        from PIL import Image  # nur hier noetig
        self.image = Image
        self.cache_dir = cache_dir
        self.budget = budget
        self.url = url
        self.tile_size = tile_size
        self.zooms = zooms
        self.suffix = suffix
        self.downloads = 0
        self.memory = {}
        self.missing = set()
        os.makedirs(cache_dir, exist_ok=True)

    @property
    def exhausted(self):
        return self.downloads >= self.budget

    def _tile(self, z, x, y):
        key = (z, x, y)
        if key in self.memory:
            return self.memory[key]
        if key in self.missing:
            return None
        path = os.path.join(self.cache_dir, f"{self.suffix}_{z}_{x}_{y}")
        data = None
        if os.path.exists(path):
            with open(path, "rb") as handle:
                data = handle.read()
        else:
            if self.exhausted:
                return None
            request = urllib.request.Request(
                self.url.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT})
            self.downloads += 1
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = response.read()
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    self.missing.add(key)
                elif error.code in (403, 429, 503):
                    # Nicht weiter anklopfen: fuer heute Schluss.
                    log(f"    {self.suffix}-Kacheln: HTTP {error.code}, Pause bis morgen")
                    self.downloads = self.budget
                return None
            except (urllib.error.URLError, TimeoutError, OSError):
                return None
            with open(path, "wb") as handle:
                handle.write(data)
        try:
            image = self.image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            return None
        if len(self.memory) > 256:
            self.memory.clear()
        self.memory[key] = image
        return image

    def _position(self, lat, lon, z):
        n = 2 ** z
        xf = (lon + 180) / 360 * n
        lat_r = math.radians(max(min(lat, 85.0), -85.0))
        yf = (1 - math.asinh(math.tan(lat_r)) / math.pi) / 2 * n
        x, y = int(xf), int(yf)
        px = min(int((xf - x) * self.tile_size), self.tile_size - 1)
        py = min(int((yf - y) * self.tile_size), self.tile_size - 1)
        return x, y, px, py

    def _height(self, image, px, py):
        r, g, b = image.getpixel((px, py))
        return r * 256 + g + b / 256 - 32768

    def zoom_for(self, lat, lon):
        for z in self.zooms:
            x, y, _, _ = self._position(lat, lon, z)
            if self._tile(z, x, y) is not None:
                return z
        return None

    def height(self, lat, lon, z):
        x, y, px, py = self._position(lat, lon, z)
        image = self._tile(z, x, y)
        return None if image is None else self._height(image, px, py)

    def relief(self, lat, lon):
        """(hoehe am punkt, hoehenunterschied im 100-m-kreis) oder None."""
        z = self.zoom_for(lat, lon)
        if z is None:
            return None
        centre = self.height(lat, lon, z)
        if centre is None:
            return None
        m_lat = 111_320.0
        m_lon = max(m_lat * math.cos(math.radians(lat)), 1.0)
        low = high = centre
        for bearing in range(0, 360, 30):
            for distance in (35.0, 70.0, EDGE_RADIUS_METERS):
                c_lat = lat + distance * math.cos(math.radians(bearing)) / m_lat
                c_lon = lon + distance * math.sin(math.radians(bearing)) / m_lon
                h = self.height(c_lat, c_lon, z)
                if h is not None:
                    low, high = min(low, h), max(high, h)
        return centre, high - low

    def standpoint(self, lat, lon, snap_to_edge):
        """(lat, lon, hoehe) oder None, wenn keine Daten erreichbar sind."""
        z = self.zoom_for(lat, lon)
        if z is None:
            return None
        centre = self.height(lat, lon, z)
        if centre is None:
            return None
        best = (lat, lon, centre)
        if snap_to_edge:
            m_lat = 111_320.0
            m_lon = max(m_lat * math.cos(math.radians(lat)), 1.0)
            steps = int(EDGE_RADIUS_METERS // EDGE_STEP_METERS)
            for i in range(-steps, steps + 1):
                for j in range(-steps, steps + 1):
                    north, east = i * EDGE_STEP_METERS, j * EDGE_STEP_METERS
                    if north * north + east * east > EDGE_RADIUS_METERS ** 2:
                        continue
                    c_lat, c_lon = lat + north / m_lat, lon + east / m_lon
                    h = self.height(c_lat, c_lon, z)
                    if h is not None and h > best[2]:
                        best = (c_lat, c_lon, h)
            if best[2] - centre < EDGE_MIN_GAIN_METERS:
                best = (lat, lon, centre)
        return best


# ---------------------------------------------------------------- Spots

def needs_height(tags):
    return (tags.get("tourism") == "viewpoint"
            or tags.get("tower:type") == "observation"
            or tags.get("natural") in ("peak", "cliff", "rock"))


def snaps_to_edge(tags):
    # Deckungsgleich mit EdgeStandpointPolicy in der App: VIEWPOINT, MOUNTAIN.
    return (tags.get("tourism") == "viewpoint"
            or tags.get("tower:type") == "observation"
            or tags.get("natural") == "peak")


def spot_key(spot):
    return f"{spot['t']}/{spot['i']}"


def read_cell_file(path):
    with open(path, "rb") as handle:
        return json.loads(zlib.decompress(handle.read(), -15))


def write_cell_file(path, payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    with open(path, "wb") as handle:
        handle.write(compressor.compress(raw) + compressor.flush())


def resolve_height(spot, terrain, coarse, totals):
    """Traegt Hoehe (und ggf. Standpunkt an der Kante) in einen Spot ein."""
    if coarse.exhausted:
        # Ohne Vorpruefung wuerde auch flaches Land Mapterhorn-Budget kosten.
        # Morgen ist wieder Budget da.
        return
    rough = coarse.relief(spot["a"], spot["o"])
    if rough is not None and rough[1] < FLAT_RELIEF_METERS:
        # Flach: die grobe Hoehe ist hier schon richtig, keine Kante zum Rauf-
        # ruecken. Kostet Mapterhorn nichts.
        spot["h"] = round(rough[0], 1)
        totals["heights_flat"] += 1
        return
    if terrain.exhausted:
        return
    stand = terrain.standpoint(spot["a"], spot["o"], snaps_to_edge(spot["g"]))
    if stand is None:
        return
    s_lat, s_lon, h = stand
    spot["h"] = round(h, 1)
    if (s_lat, s_lon) != (spot["a"], spot["o"]):
        spot["s"] = [round(s_lat, 6), round(s_lon, 6)]
    totals["heights_new"] += 1


def previous_spots(previous_dir, name):
    path = os.path.join(previous_dir or "", f"{name}.bin")
    if not previous_dir or not os.path.exists(path):
        return None
    try:
        return read_cell_file(path)
    except Exception:
        return None


def previous_manifest(previous_dir):
    path = os.path.join(previous_dir or "", "manifest.json")
    if not previous_dir or not os.path.exists(path):
        return {"cells": {}}
    with open(path) as handle:
        return json.load(handle)


def build_band(band, bands, previous_dir, out_dir, height_budget,
               coarse_budget=40000, time_budget_minutes=290):
    """Ein Band traegt fehlende Standpunkthoehen nach, Rheintal zuerst.

    Mapterhorn wird von Freiwilligen betrieben: je Band und Tag nur
    `height_budget` Kacheln. Geaendert wird nur, was eine Hoehe bekommt; der
    Rest kommt beim Zusammenbauen aus dem letzten Katalog.
    """
    os.makedirs(out_dir, exist_ok=True)
    cache = os.path.join(out_dir, "..", ".terrain-cache")
    terrain = Terrain(cache, height_budget)
    coarse = Terrain(cache, coarse_budget, url=TERRARIUM_URL, tile_size=TERRARIUM_TILE,
                     zooms=(TERRARIUM_ZOOM,), suffix="png")
    old = previous_manifest(previous_dir)
    names = sorted((n for n, c in old["cells"].items() if c), key=home_distance)[band::bands]
    started = time.time()
    deadline = started + time_budget_minutes * 60
    cells_out = {}
    totals = {"heights_new": 0, "heights_flat": 0, "heights_missing": 0}

    for name in names:
        if time.time() > deadline or coarse.exhausted:
            log(f"  Zeit oder Budget um bei {name}: Rest morgen")
            break
        payload = previous_spots(previous_dir, name)
        if payload is None:
            continue
        before = totals["heights_new"] + totals["heights_flat"]
        for spot in payload.get("spots", []):
            if needs_height(spot["g"]) and "h" not in spot and time.time() <= deadline:
                resolve_height(spot, terrain, coarse, totals)
                if "h" not in spot:
                    totals["heights_missing"] += 1
        if totals["heights_new"] + totals["heights_flat"] > before:
            write_cell_file(os.path.join(out_dir, f"{name}.bin"), payload)
            cells_out[name] = len(payload["spots"])
            # Nach jeder Zelle sichern: wird das Band abgebrochen, laedt der
            # Workflow den Zwischenstand trotzdem hoch.
            save_band_manifest(out_dir, band, cells_out)

    save_band_manifest(out_dir, band, cells_out)
    log(f"Band {band}: {len(cells_out)} Zellen geaendert, {totals} · Mapterhorn "
        f"{terrain.downloads} Kacheln · AWS {coarse.downloads} Kacheln · "
        f"{int(time.time() - started)} s")


def save_band_manifest(out_dir, band, cells):
    path = os.path.join(out_dir, f"manifest-band{band}.json")
    with open(path + ".tmp", "w") as handle:
        json.dump({"cells": cells}, handle)
    os.replace(path + ".tmp", path)


def assemble(bands_dir, previous_dir, out_dir):
    """Hoehen-Lauf: geaenderte Zellen aus den Baendern, der Rest wie gehabt."""
    old = previous_manifest(previous_dir)
    if not old["cells"]:
        sys.exit("Kein Katalog zum Ergaenzen; erst osm.yml laufen lassen")
    os.makedirs(out_dir, exist_ok=True)
    counts = dict(old["cells"])
    for entry in os.listdir(bands_dir):
        if entry.startswith("manifest-band") and entry.endswith(".json"):
            with open(os.path.join(bands_dir, entry)) as handle:
                counts.update(json.load(handle)["cells"])
    for name, count in list(counts.items()):
        if not count:
            continue
        for source in (os.path.join(bands_dir, f"{name}.bin"),
                       os.path.join(previous_dir, f"{name}.bin")):
            if os.path.exists(source):
                shutil.copyfile(source, os.path.join(out_dir, f"{name}.bin"))
                break
        else:
            # Ohne Datei raus aus dem Manifest: die App fragt dort Overpass.
            del counts[name]
    write_manifest(out_dir, counts, old.get("osm", ""))


def status(release_dir, failed_bands=""):
    """Kurzer Stand fuer das Issue 'Katalog-Status', in Markdown."""
    with open(os.path.join(release_dir, "manifest.json")) as handle:
        manifest = json.load(handle)
    cells = manifest["cells"]
    with_spots = [n for n, c in cells.items() if c]
    need = have = 0
    loreley = False
    home = cell_name(*cell_of(*HOME))
    for name in with_spots:
        spots = read_cell_file(os.path.join(release_dir, f"{name}.bin"))["spots"]
        for spot in spots:
            if needs_height(spot["g"]):
                need += 1
                have += "h" in spot
        if name == home:
            loreley = any("lorele" in s["g"].get("name", "").lower() for s in spots)
    missing = [n for n in all_cells() if n not in cells]

    lines = [
        f"**{manifest['built'][:10]}** · OSM-Stand {manifest.get('osm') or 'alt (Overpass)'} · "
        f"{len(with_spots)} Kacheln mit Spots · {sum(cells.values())} Spots",
        f"- Standpunkthoehen: {have} von {need} ({round(100 * have / max(need, 1))} %)",
        f"- Rheintal ({home}): "
        + (f"{cells[home]} Spots · Loreley: {'ja' if loreley else 'nein'}" if cells.get(home) else "nicht drin"),
    ]
    if missing:
        lines.append(f"- Nicht im Katalog (App fragt dort Overpass): {len(missing)} Zellen")
    if failed_bands:
        lines.append(f"- Ausgefallen: {failed_bands} (deren Hoehen kommen morgen)")
    print("\n".join(lines))


def selftest():
    """Kleiner Durchlauf ohne Netz: Auswahl, IDs, Zusammenbau, Hoehen, Grenze."""
    global MAX_RELEASE_ASSETS
    features = [
        {"id": "n1", "geometry": {"type": "Point", "coordinates": [7.73, 50.14]},
         "properties": {"tourism": "viewpoint", "name": "Loreley"}},
        {"id": "n2", "geometry": {"type": "Point", "coordinates": [7.5, 50.1]},
         "properties": {"natural": "peak"}},  # ohne Namen: raus
        {"id": "n3", "geometry": {"type": "Point", "coordinates": [7.6, 50.2]},
         "properties": {"tourism": "viewpoint", "access": "private"}},  # privat: raus
        {"id": "w2", "geometry": {"type": "LineString", "coordinates": [[7.0, 50.0], [7.2, 50.4]]},
         "properties": {"natural": "beach"}},
        {"id": "a4", "geometry": {"type": "MultiPolygon", "coordinates": [[[[7.0, 50.0], [7.2, 50.4], [7.0, 50.4], [7.0, 50.0]]]]},
         "properties": {"natural": "beach"}},  # derselbe Weg 2 als Flaeche
        {"id": "a7", "geometry": {"type": "MultiPolygon", "coordinates": [[[[-16.9, 32.6], [-16.8, 32.7], [-16.9, 32.7], [-16.9, 32.6]]]]},
         "properties": {"historic": "castle", "name": "Fortaleza"}},
        {"id": "n4", "geometry": {"type": "Point", "coordinates": [180.0, 10.0]},
         "properties": {"man_made": "lighthouse"}},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        geo = os.path.join(tmp, "x.geojsonseq")
        with open(geo, "w") as handle:
            handle.write("".join("\x1e" + json.dumps(f) + "\n" for f in features))
        opl = os.path.join(tmp, "x.opl")
        with open(opl, "w") as handle:
            handle.write("n9 v1 dV c0 t i0 u T x7.6 y50.3\n"
                         "w8 v1 dV c0 t i0 u Tbuilding=yes Nn10x7.5y50.25,n11x7.7y50.35\n"
                         "r5 v1 dV c0 t i0 u Thistoric=castle,name=Feste%20%Franz,type=site Mw8@,n9@label,r6@\n"
                         "r6 v1 dV c0 t i0 u Thistoric=castle,name=X,type=multipolygon Mw8@outer\n")
        site_relations(opl, geo)
        split_osm(geo, os.path.join(tmp, "osm", "a"))
        split_osm(geo, os.path.join(tmp, "osm", "b"))  # Ueberlappung
        prev = os.path.join(tmp, "prev")
        os.makedirs(prev)
        write_cell_file(os.path.join(prev, "c22_37.bin"), {"spots": [
            {"t": "node", "i": 1, "a": 50.14001, "o": 7.73, "g": {}, "h": 192.0, "s": [50.1401, 7.7301]}]})
        assemble_osm(os.path.join(tmp, "osm"), prev, os.path.join(tmp, "rel"))
        with open(os.path.join(tmp, "rel", "manifest.json")) as handle:
            manifest = json.load(handle)
        assert len(manifest["cells"]) == ROWS * COLS
        assert manifest["cells"]["c22_37"] == 3, manifest["cells"]["c22_37"]  # Loreley, Strand, Feste
        spots = {spot_key(s): s for s in read_cell_file(os.path.join(tmp, "rel", "c22_37.bin"))["spots"]}
        assert set(spots) == {"node/1", "way/2", "relation/5"}, set(spots)
        assert spots["relation/5"]["g"]["name"] == "Feste Franz"
        assert (spots["relation/5"]["a"], spots["relation/5"]["o"]) == (50.3, 7.6)
        assert spots["node/1"]["h"] == 192.0 and spots["node/1"]["s"] == [50.1401, 7.7301]
        assert spots["way/2"]["a"] == 50.2 and spots["way/2"]["o"] == 7.1
        assert manifest["cells"][cell_name(*cell_of(32.65, -16.85))] == 1
        assert "relation/3" in {spot_key(s) for s in read_cell_file(
            os.path.join(tmp, "rel", cell_name(*cell_of(32.65, -16.85)) + ".bin"))["spots"]}
        assert manifest["cells"][cell_name(*cell_of(10.0, -180.0))] == 1  # Datumsgrenze

        # Hoehen-Lauf ohne Baender: alles bleibt, wie es war.
        os.makedirs(os.path.join(tmp, "bands"))
        assemble(os.path.join(tmp, "bands"), os.path.join(tmp, "rel"), os.path.join(tmp, "rel2"))
        with open(os.path.join(tmp, "rel2", "manifest.json")) as handle:
            assert json.load(handle)["cells"] == manifest["cells"]

        # Zu viele Zellen fuers Release: die kleinsten fliegen raus.
        MAX_RELEASE_ASSETS = 3
        assemble(os.path.join(tmp, "bands"), os.path.join(tmp, "rel"), os.path.join(tmp, "rel3"))
        with open(os.path.join(tmp, "rel3", "manifest.json")) as handle:
            cells = json.load(handle)["cells"]
        assert sum(1 for c in cells.values() if c) == 2 and cells["c22_37"] == 3
        assert len([f for f in os.listdir(os.path.join(tmp, "rel3")) if f.endswith(".bin")]) == 2
    log("Selbsttest ok")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--band", type=int)
    parser.add_argument("--bands", type=int, default=6)
    parser.add_argument("--previous")
    parser.add_argument("--out")
    parser.add_argument("--assemble")
    parser.add_argument("--split-osm", help="osmium-Export (geojsonseq) einer Region")
    parser.add_argument("--assemble-osm", help="Ordner mit allen Regionen")
    parser.add_argument("--site-relations", nargs=2, metavar=("OPL", "GEOJSONSEQ"),
                        help="Relationen ohne Flaeche an den Export anhaengen")
    parser.add_argument("--status", help="Release-Ordner: Stand als Markdown ausgeben")
    parser.add_argument("--failed-bands", default="")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--height-budget", type=int, default=2000,
                        help="Mapterhorn-Kacheln je Band und Lauf")
    parser.add_argument("--time-budget", type=int, default=290,
                        help="Minuten; danach nur noch speichern")
    args = parser.parse_args()

    if args.selftest:
        selftest()
    elif args.site_relations:
        site_relations(*args.site_relations)
    elif args.split_osm:
        split_osm(args.split_osm, args.out)
    elif args.assemble_osm:
        assemble_osm(args.assemble_osm, args.previous, args.out)
    elif args.status:
        status(args.status, args.failed_bands)
    elif args.assemble:
        assemble(args.assemble, args.previous, args.out)
    else:
        build_band(args.band, args.bands, args.previous, args.out, args.height_budget,
                   time_budget_minutes=args.time_budget)


if __name__ == "__main__":
    main()
