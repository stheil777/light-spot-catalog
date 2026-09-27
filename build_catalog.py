#!/usr/bin/env python3
"""LIGHT Spot-Katalog.

Baut jeden Tag ein Stueck aus OpenStreetMap einen weltweiten Katalog fotogener
Orte (Aussichtspunkte, Gipfel, Felsen, Burgen, Leuchttuerme, Straende) und
legt ihn als 5-Grad-Kacheln ab. Die App laedt nur die Kacheln um den
Suchort, statt bei jeder Suche die ehrenamtlichen Overpass-Server zu fragen.

Fuer Aussichtspunkte, Aussichtstuerme und Gipfel steht die Standpunkthoehe
gleich mit drin, aus Mapterhorn, und der Standpunkt sitzt auf dem hoechsten
Punkt im Umkreis von 100 m (die Karte setzt Aussichtspunkte gern an den Hang).
Das kostet einmal hier statt bei jedem Nutzer.

Aufrufe:
  build_catalog.py --band 0 --bands 6 --previous prev --out out
  build_catalog.py --assemble bands --previous prev --out release

Kachelformat: JSON, roh-deflate-komprimiert (wbits=-15), damit iOS es mit
NSData.decompressed(using: .zlib) ohne Zusatzbibliothek oeffnet.
"""

import argparse
import datetime
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

CELL_DEGREES = 5
LAT_MIN = -60
LAT_MAX = 80
ROWS = (LAT_MAX - LAT_MIN) // CELL_DEGREES
COLS = 360 // CELL_DEGREES
FORMAT_VERSION = 1
MAX_RELEASE_ASSETS = 990

USER_AGENT = "LIGHT-SpotCatalog/1.0 (+https://github.com/stheil777/light-spot-catalog)"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_PAUSE_SECONDS = 1.5
# Ist ein Server ausgelastet (504), wird einmal gewartet und neu gefragt,
# statt die Zelle sofort aufzugeben.
OVERPASS_RETRY_PAUSE_SECONDS = 60
MAX_SUBDIVISION_DEPTH = 2
# "Zu viel" (429): eine Minute warten. Nach so vielen Absagen im ganzen Lauf
# ist fuer heute Schluss.
THROTTLE_PAUSE_SECONDS = 60
MAX_THROTTLE_STRIKES = 20
throttle_strikes = 0

# Abgefragt wird je Teilzelle von 1,25 Grad, nicht die ganze 5-Grad-Zelle:
# die ganze Zelle lief am 25.09.2026 fast immer in einen Timeout, bevor
# zerlegt wurde, und das kostete je Zelle zwei Server mal vier Minuten.
SUB_PER_SIDE = 4
# Welche Teilzellen Land haben (make_land_mask.py). Meer wird nie gefragt.
LAND_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "land_cells.json")

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

# Von innen nach aussen: zuerst das Rheintal und Madeira, wo die App benutzt
# wird, dann in Ringen nach Entfernung vom Rheintal. Vorher lief ein
# Kasten ab 30 Grad West von links nach rechts, und der Atlantik kam vor
# Deutschland dran.
HOME = (50.17, 7.70)
FIRST = [(50.17, 7.70), (32.75, -16.95)]  # Rheintal, Madeira

KEEP_TAGS = {
    "tourism", "man_made", "natural", "tower:type", "historic",
    "access", "name", "alt_name", "wikidata", "wikipedia", "direction",
    "addr:city", "is_in", "ele",
    "name:de", "name:en", "name:es", "name:fr", "name:it", "name:pt", "name:nl",
}
BLOCKED_ACCESS = {"private", "no", "customers"}


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
    return int((lat - LAT_MIN) // CELL_DEGREES), int((lon + 180) // CELL_DEGREES)


def land_cells():
    with open(LAND_FILE) as handle:
        return json.load(handle)["cells"]


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def global_order():
    """Alle Landzellen, die wichtigsten zuerst."""
    first = [cell_of(lat, lon) for lat, lon in FIRST]
    cells = []
    for name in land_cells():
        row, col = int(name[1:3]), int(name[4:6])
        south, west, north, east = cell_bbox(row, col)
        centre = distance_km(HOME[0], HOME[1], (south + north) / 2, (west + east) / 2)
        rank = first.index((row, col)) if (row, col) in first else len(first)
        cells.append(((rank, centre), (row, col)))
    return [cell for _, cell in sorted(cells)]


def band_cells(band, bands):
    # Reihum aus der Gesamtreihenfolge: jedes Band faengt nahe am Rheintal an.
    return [cell for index, cell in enumerate(global_order()) if index % bands == band]


def sub_bboxes(row, col, subs):
    south, west, _, _ = cell_bbox(row, col)
    step = CELL_DEGREES / SUB_PER_SIDE
    for sub in subs:
        i, j = divmod(sub, SUB_PER_SIDE)
        s, w = south + i * step, west + j * step
        yield s, w, s + step, w + step


# ---------------------------------------------------------------- Overpass

def overpass_query(south, west, north, east):
    bbox = f"{south},{west},{north},{east}"
    # Ohne eigenes maxsize: wer 1 GB Speicher reserviert, bekommt vom Server
    # bei Last sofort 504. Am 26.09.2026 nachgemessen, dieselbe Abfrage am
    # Rheintal: mit 1 GB 504 nach 11 s, ohne 200 nach 2 s. Daran scheiterte
    # der ganze erste Lauf, nicht an ueberlasteten Servern.
    return f"""[out:json][timeout:90];
(
  nwr["tourism"="viewpoint"]({bbox});
  nwr["man_made"="lighthouse"]({bbox});
  nwr["natural"="beach"]({bbox});
  nwr["man_made"="tower"]["tower:type"="observation"]({bbox});
  nwr["natural"~"^(peak|cliff|rock)$"]["name"]({bbox});
  nwr["historic"~"^(castle|ruins)$"]["name"]({bbox});
);
out center tags qt;"""


class OverpassFailure(Exception):
    pass


class OverpassThrottled(Exception):
    """Der Server sagt "zu viel". Dann fuer heute Schluss, nicht nachbohren."""


class OutOfTime(Exception):
    """Das Zeitbudget ist um, auch mitten in einer Zelle.

    Am 27.09.2026 hing Band 2 ueber zwei Stunden in einer einzigen Zelle
    (Marokko): Timeout, zerlegen, wieder Timeout. Das Budget wurde nur
    zwischen den Zellen geprueft, die harte Job-Grenze griff zuerst, und
    alles, was das Band in fuenf Stunden gebaut hatte, war verloren.
    Deshalb prueft jetzt jeder einzelne Abruf die Uhr.
    """


# Wird vom Lauf gesetzt; None heisst ohne Grenze (Tests, Fixture).
overpass_deadline = None


def fetch_overpass(south, west, north, east):
    body = urllib.parse.urlencode({"data": overpass_query(south, west, north, east)}).encode()
    last_error = None
    # Zwei Runden ueber beide Server, dazwischen eine Minute Pause.
    for attempt, endpoint in enumerate(OVERPASS_ENDPOINTS * 2):
        if overpass_deadline is not None and time.time() > overpass_deadline:
            raise OutOfTime()
        if attempt == len(OVERPASS_ENDPOINTS):
            time.sleep(OVERPASS_RETRY_PAUSE_SECONDS)
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"User-Agent": USER_AGENT,
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read())
            remark = payload.get("remark") or ""
            if "error" in remark.lower():
                raise OverpassFailure(remark)
            time.sleep(OVERPASS_PAUSE_SECONDS)
            return payload.get("elements", [])
        except urllib.error.HTTPError as error:
            if error.code == 429:
                # Die Abfrage-Plaetze fuer diese Adresse sind belegt. Warten
                # und neu fragen; erst nach mehreren Absagen fuer heute Schluss.
                global throttle_strikes
                throttle_strikes += 1
                if throttle_strikes > MAX_THROTTLE_STRIKES:
                    raise OverpassThrottled(endpoint)
                log(f"    overpass {endpoint.split('/')[2]}: 429, warte {THROTTLE_PAUSE_SECONDS} s")
                time.sleep(THROTTLE_PAUSE_SECONDS)
                last_error = error
                continue
            last_error = error
            log(f"    overpass {endpoint.split('/')[2]} {south},{west}: HTTP {error.code}")
            time.sleep(15)
        except (urllib.error.URLError, OverpassFailure, TimeoutError, ValueError, OSError) as error:
            last_error = error
            log(f"    overpass {endpoint.split('/')[2]} {south},{west}: {str(error)[:120]}")
            time.sleep(15)
    raise OverpassFailure(str(last_error))


def elements_in(south, west, north, east, depth=0):
    """Eine Zelle; ist sie zu dicht fuer den Server, in vier Teile zerlegen."""
    try:
        return fetch_overpass(south, west, north, east)
    except OverpassFailure:
        if depth >= MAX_SUBDIVISION_DEPTH:
            raise
    mid_lat = (south + north) / 2
    mid_lon = (west + east) / 2
    log(f"    zerlege {south},{west} (Tiefe {depth + 1})")
    result = []
    for s, w, n, e in (
        (south, west, mid_lat, mid_lon), (south, mid_lon, mid_lat, east),
        (mid_lat, west, north, mid_lon), (mid_lat, mid_lon, north, east),
    ):
        result.extend(elements_in(s, w, n, e, depth + 1))
    return result


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


def compact(element):
    tags = {k: v for k, v in (element.get("tags") or {}).items() if k in KEEP_TAGS}
    if tags.get("access") in BLOCKED_ACCESS:
        return None
    if "lat" in element:
        lat, lon = element["lat"], element["lon"]
    elif "center" in element:
        lat, lon = element["center"]["lat"], element["center"]["lon"]
    else:
        return None
    return {"t": element["type"], "i": element["id"],
            "a": round(lat, 6), "o": round(lon, 6), "g": tags}


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


def previous_spots(previous_dir, name):
    path = os.path.join(previous_dir or "", f"{name}.bin")
    if not previous_dir or not os.path.exists(path):
        return None
    try:
        return read_cell_file(path)
    except Exception:
        return None


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


def previous_manifest(previous_dir):
    path = os.path.join(previous_dir or "", "manifest.json")
    if not previous_dir or not os.path.exists(path):
        return {"cells": {}, "crawled": {}}
    with open(path) as handle:
        manifest = json.load(handle)
    manifest.setdefault("crawled", {})
    return manifest


def build_band(band, bands, previous_dir, out_dir, height_budget, crawl_cells,
               fixture=None, coarse_budget=40000, time_budget_minutes=290):
    """Ein Band: die aeltesten Zellen neu aus OSM, ueberall fehlende Hoehen.

    Overpass bittet um hoechstens 10 000 Abfragen und rund 1 GB am Tag. Die
    ganze Welt an einem Tag waere darueber. Deshalb holt jeder Lauf nur
    `crawl_cells` Zellen je Band neu, nie gesehene und Europa zuerst, danach
    die mit dem aeltesten Stand. Nach etwa fuenf Tagen ist die Welt einmal
    durch, danach frischt sich jede Zelle im gleichen Takt auf.
    """
    os.makedirs(out_dir, exist_ok=True)
    cache = os.path.join(out_dir, "..", ".terrain-cache")
    terrain = Terrain(cache, height_budget)
    coarse = Terrain(cache, coarse_budget, url=TERRARIUM_URL, tile_size=TERRARIUM_TILE,
                     zooms=(TERRARIUM_ZOOM,), suffix="png")
    old = previous_manifest(previous_dir)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    cells_out, crawled_out = {}, {}
    started = time.time()
    totals = {"crawled": 0, "kept": 0, "spots": 0, "heights_new": 0, "heights_flat": 0,
              "heights_reused": 0, "heights_missing": 0, "failed": 0}

    land = land_cells()
    order = band_cells(band, bands)
    rank = {cell: index for index, cell in enumerate(order)}
    # Nie gesehen zuerst (Rheintal vorn, band_cells sortiert schon so), dann
    # nach Alter des letzten Standes.
    order.sort(key=lambda cell: (
        cell_name(*cell) in old["crawled"],
        old["crawled"].get(cell_name(*cell), ""),
        rank[cell],
    ))
    to_crawl = set(order[:crawl_cells])
    throttled = False
    # Rechtzeitig aufhoeren und speichern. Am 25.09.2026 wurde Band 0 an der
    # Zeitgrenze des Laufs abgebrochen, und alles Gefundene war verloren.
    deadline = started + time_budget_minutes * 60
    global overpass_deadline
    overpass_deadline = deadline
    out_of_time = False

    # Crawl-Zellen zuerst, damit ein Abbruch wegen "zu viel" nur sie trifft.
    for row, col in sorted(order, key=lambda cell: (cell not in to_crawl, order.index(cell))):
        name = cell_name(row, col)
        south, west, north, east = cell_bbox(row, col)
        previous = previous_spots(previous_dir, name)
        if not out_of_time and time.time() > deadline:
            out_of_time = True
            log(f"  Zeitbudget ({time_budget_minutes} min) erreicht: Rest behaelt den alten Stand")
        crawl = (row, col) in to_crawl and not throttled and not out_of_time

        elements = None
        if crawl:
            try:
                if fixture is not None:
                    elements = [e for e in fixture
                                if south <= (e.get("lat") or e.get("center", {}).get("lat", 999)) < north
                                and west <= (e.get("lon") or e.get("center", {}).get("lon", 999)) < east]
                else:
                    # Erst zuweisen, wenn alle Teile da sind. Am 26.09.2026
                    # kam mitten in der Zelle "zu viel", und die halbe Zelle
                    # wurde als ganze gespeichert: ohne Loreley.
                    collected = []
                    for sub in sub_bboxes(row, col, land[name]):
                        collected.extend(elements_in(*sub))
                    elements = collected
            except OutOfTime:
                out_of_time = True
                log(f"  {name}: Zeitbudget ({time_budget_minutes} min) mitten in der Zelle erreicht, "
                    "Rest behaelt den alten Stand")
            except OverpassThrottled as error:
                throttled = True
                log(f"  {name}: Overpass sagt 'zu viel' ({error}), OSM-Abruf fuer heute beendet")
            except OverpassFailure as error:
                totals["failed"] += 1
                log(f"  {name}: Overpass aus ({str(error)[:100]})")

        if elements is None:
            # Nicht dran oder fehlgeschlagen: Stand der Vorwoche behalten und
            # nur Hoehen nachtragen. Ohne Vorwoche bleibt die Zelle aussen
            # vor, dann fragt die App dort selbst Overpass.
            if previous is None:
                if name in old["cells"] and old["cells"][name] == 0:
                    cells_out[name] = 0
                    crawled_out[name] = old["crawled"].get(name, "")
                continue
            for spot in previous.get("spots", []):
                if needs_height(spot["g"]) and "h" not in spot and not out_of_time:
                    resolve_height(spot, terrain, coarse, totals)
                    if "h" not in spot:
                        totals["heights_missing"] += 1
            write_cell_file(os.path.join(out_dir, f"{name}.bin"), previous)
            cells_out[name] = len(previous.get("spots", []))
            crawled_out[name] = old["crawled"].get(name, "")
            totals["kept"] += 1
            totals["spots"] += cells_out[name]
            continue

        reuse = {spot_key(spot): spot for spot in (previous or {}).get("spots", [])}
        spots, seen = [], set()
        for element in elements:
            spot = compact(element)
            if spot is None or spot_key(spot) in seen:
                continue
            seen.add(spot_key(spot))
            if needs_height(spot["g"]) and time.time() <= deadline:
                former = reuse.get(spot_key(spot))
                if former and former["a"] == spot["a"] and former["o"] == spot["o"] and "h" in former:
                    spot["h"] = former["h"]
                    if "s" in former:
                        spot["s"] = former["s"]
                    totals["heights_reused"] += 1
                else:
                    resolve_height(spot, terrain, coarse, totals)
                    if "h" not in spot:
                        totals["heights_missing"] += 1
            spots.append(spot)

        cells_out[name] = len(spots)
        crawled_out[name] = today
        totals["crawled"] += 1
        totals["spots"] += len(spots)
        if spots:
            write_cell_file(os.path.join(out_dir, f"{name}.bin"),
                            {"v": FORMAT_VERSION, "cell": name, "spots": spots})
            log(f"  {name}: {len(spots)} Spots (neu aus OSM)")

    with open(os.path.join(out_dir, f"manifest-band{band}.json"), "w") as handle:
        json.dump({"cells": cells_out, "crawled": crawled_out}, handle)
    log(f"Band {band}: {totals} · Mapterhorn {terrain.downloads} Kacheln · "
        f"AWS {coarse.downloads} Kacheln · {int(time.time() - started)} s")


def assemble(bands_dir, previous_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    cells, crawled = {}, {}
    for entry in sorted(os.listdir(bands_dir)):
        if entry.startswith("manifest-band") and entry.endswith(".json"):
            with open(os.path.join(bands_dir, entry)) as handle:
                band = json.load(handle)
            cells.update(band.get("cells", {}))
            crawled.update(band.get("crawled", {}))

    # Ein komplett ausgefallenes Band: die Vorwoche springt ein.
    previous_manifest = None
    if previous_dir and os.path.exists(os.path.join(previous_dir, "manifest.json")):
        with open(os.path.join(previous_dir, "manifest.json")) as handle:
            previous_manifest = json.load(handle)
    if previous_manifest:
        for name, count in previous_manifest.get("cells", {}).items():
            if name in cells:
                continue
            source = os.path.join(previous_dir, f"{name}.bin")
            if count == 0 or os.path.exists(source):
                cells[name] = count
                crawled[name] = previous_manifest.get("crawled", {}).get(name, "")
                if count:
                    with open(source, "rb") as src, open(os.path.join(bands_dir, f"{name}.bin"), "wb") as dst:
                        dst.write(src.read())

    # Meer: bekannt und leer. Sonst hielte die App eine Suche an der Kueste
    # fuer "Katalog weiss es nicht" und fragte wieder selbst Overpass.
    land = land_cells()
    for row in range(ROWS):
        for col in range(COLS):
            name = cell_name(row, col)
            if name not in land:
                cells.setdefault(name, 0)

    assets = [name for name, count in cells.items() if count > 0]
    if len(assets) + 1 > MAX_RELEASE_ASSETS:
        sys.exit(f"{len(assets)} Kacheln: mehr als ein Release tragen kann")
    if not cells:
        sys.exit("Kein einziges Band hat geliefert")

    for name in assets:
        with open(os.path.join(bands_dir, f"{name}.bin"), "rb") as src, \
                open(os.path.join(out_dir, f"{name}.bin"), "wb") as dst:
            dst.write(src.read())

    manifest = {
        "version": FORMAT_VERSION,
        "built": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cellDegrees": CELL_DEGREES,
        "latMin": LAT_MIN,
        "latMax": LAT_MAX,
        "cells": dict(sorted(cells.items())),
        "crawled": dict(sorted(crawled.items())),
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as handle:
        json.dump(manifest, handle, separators=(",", ":"))
    log(f"Katalog: {len(cells)} Zellen, {len(assets)} mit Spots, "
        f"{sum(cells.values())} Spots gesamt")


def status(release_dir, failed_bands=""):
    """Kurzer Stand fuer das Issue 'Katalog-Status', in Markdown."""
    with open(os.path.join(release_dir, "manifest.json")) as handle:
        manifest = json.load(handle)
    land = land_cells()
    cells = manifest["cells"]
    done = sum(1 for name in land if name in cells)
    with_spots = sum(1 for count in cells.values() if count > 0)
    total = sum(cells.values())

    home = cell_name(*cell_of(*HOME))
    loreley = False
    path = os.path.join(release_dir, f"{home}.bin")
    if os.path.exists(path):
        with open(path, "rb") as handle:
            spots = json.loads(zlib.decompress(handle.read(), -15))["spots"]
        loreley = any("lorele" in spot["g"].get("name", "").lower() for spot in spots)

    lines = [
        f"**{manifest['built'][:10]}** · {done} von {len(land)} Landzellen "
        f"({round(100 * done / len(land))} %) · {with_spots} Kacheln mit Spots · {total} Spots",
        f"- Rheintal ({home}): "
        + (f"{cells[home]} Spots · Loreley: {'ja' if loreley else 'nein'}" if home in cells else "noch nicht drin"),
    ]
    if failed_bands:
        lines.append(f"- Ausgefallen: {failed_bands} (deren Zellen behalten den alten Stand)")
    if done == len(land):
        lines.append("- Die Welt ist einmal komplett drin. Ab jetzt wird nur noch aufgefrischt.")
    print("\n".join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--band", type=int)
    parser.add_argument("--bands", type=int, default=6)
    parser.add_argument("--previous")
    parser.add_argument("--out")
    parser.add_argument("--assemble")
    parser.add_argument("--status", help="Release-Ordner: Stand als Markdown ausgeben")
    parser.add_argument("--failed-bands", default="")
    parser.add_argument("--height-budget", type=int, default=2000,
                        help="Hoehenkacheln pro Lauf; schont Mapterhorn")
    parser.add_argument("--fixture", help="Overpass-JSON statt Netz (Test)")
    parser.add_argument("--crawl-cells", type=int, default=70,
                        help="Zellen je Band und Lauf neu aus OSM; schont Overpass")
    parser.add_argument("--time-budget", type=int, default=290,
                        help="Minuten; danach nur noch speichern")
    args = parser.parse_args()

    if args.status:
        status(args.status, args.failed_bands)
    elif args.assemble:
        assemble(args.assemble, args.previous, args.out)
    else:
        fixture = None
        if args.fixture:
            with open(args.fixture) as handle:
                fixture = json.load(handle)["elements"]
        build_band(args.band, args.bands, args.previous, args.out, args.height_budget,
                   args.crawl_cells, fixture, time_budget_minutes=args.time_budget)


if __name__ == "__main__":
    main()
