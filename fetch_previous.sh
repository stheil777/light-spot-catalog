#!/usr/bin/env bash
# Holt den letzten Katalog nach previous/, ueber die oeffentlichen
# Download-Links statt die API: 6 Baender x 1000 Dateien sprengten am
# 03.10.2026 das API-Limit (403), und vier Baender rechneten ohne Katalog.
set -euo pipefail
BASE="https://github.com/$GITHUB_REPOSITORY/releases/latest/download"
mkdir -p previous
if ! curl -fsSL --retry 5 --retry-all-errors -o previous/manifest.json "$BASE/manifest.json"; then
  echo "Noch kein Katalog veroeffentlicht"
  rm -f previous/manifest.json
  exit 0
fi
python3 -c "import json; [print(n) for n, c in json.load(open('previous/manifest.json'))['cells'].items() if c]" \
  > previous/names.txt
# Vier gleichzeitig: auch der Download-Server sagt sonst "429 zu viele".
# Fehlt am Ende eine Datei, scheitert der Schritt laut statt ohne Katalog
# weiterzurechnen.
xargs -P 4 -I{} curl -fsSL --retry 8 --retry-all-errors --retry-delay 10 \
  -o "previous/{}.bin" "$BASE/{}.bin" < previous/names.txt || true
missing=$(while read -r n; do [ -s "previous/$n.bin" ] || echo "$n"; done < previous/names.txt)
rm previous/names.txt
if [ -n "$missing" ]; then
  echo "Fehlen: $missing"
  exit 1
fi
echo "$(ls previous | wc -l) Dateien geholt"
