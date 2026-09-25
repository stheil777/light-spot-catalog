# LIGHT Spot-Katalog

Weltweiter Katalog fotogener Orte für die App LIGHT: Aussichtspunkte,
Aussichtstürme, Gipfel, Felsen, Burgen, Leuchttürme und Strände.

Eine GitHub Action läuft jeden Tag. Sie holt je Lauf rund 420 Zellen neu
aus OpenStreetMap (nie gesehene und Europa zuerst, dann die ältesten) und
trägt fehlende Standpunkthöhen nach. Die Budgets bleiben deutlich unter den
Bitten der Betreiber; meldet ein Server „zu viel“, hört der Lauf für den Tag
auf. Jeder Lauf wird als Release veröffentlicht. Die App lädt nur die 5°-Kacheln rund um den Suchort:

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

Actions → „Spot-Katalog bauen“ → Run workflow.
