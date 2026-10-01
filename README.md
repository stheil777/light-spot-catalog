# LIGHT Spot-Katalog

Weltweiter Katalog fotogener Orte für die App LIGHT: Aussichtspunkte,
Aussichtstürme, Gipfel, Felsen, Burgen, Leuchttürme und Strände.

Einmal die Woche (`osm.yml`) kommt die ganze Welt aus den
OSM-Komplettdaten von [Geofabrik](https://download.geofabrik.de/): je Kontinent
herunterladen, mit osmium auf die Spot-Arten filtern, in 5-Grad-Zellen
schneiden. Danach kennt der Katalog jede Zelle zwischen 60° Süd und 80° Nord.
Jeden Tag (`build.yml`) werden fehlende Standpunkthöhen nachgetragen,
Rheintal zuerst, in festen Budgets. Jeder Lauf wird als Release veröffentlicht,
der Stand steht danach in Issue #1. Die App lädt nur die 5°-Kacheln rund um
den Suchort:

```
https://github.com/stheil777/light-spot-catalog/releases/latest/download/manifest.json
https://github.com/stheil777/light-spot-catalog/releases/latest/download/c22_37.bin
```

Kacheln sind JSON, roh-deflate-komprimiert. Für Aussichtspunkte und Gipfel
enthalten sie die Standpunkthöhe (`h`) und, wo der Kartenpunkt am Hang
liegt, den Standpunkt oben an der Kante (`s`).

## Daten und Lizenzen

- Orte: © OpenStreetMap-Mitwirkende, [ODbL](https://opendatacommons.org/licenses/odbl/).
  Der Katalog ist eine abgeleitete Datenbank und steht unter derselben Lizenz.
- Höhen: [Mapterhorn](https://mapterhorn.com/attribution) und die dort genannten Quellen.

## Von Hand neu bauen

Actions → „Spot-Katalog aus OSM“ (Orte) oder „Spot-Katalog bauen“ (Höhen) → Run workflow.
Test ohne Netz: `python build_catalog.py --selftest`.
