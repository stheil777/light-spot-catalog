#!/usr/bin/env bash
# Veroeffentlicht release/ als neuen Katalog, setzt den Stand ins Issue 1 und
# behaelt die zwei neuesten Kataloge. Aufruf: publish.sh "<ausgefallene Jobs>"
set -euo pipefail
R=(--repo "$GITHUB_REPOSITORY")
# Kacheln wie im letzten Katalog: kein neuer Release. Seit alle Hoehen da sind
# (06.10.2026), kam sonst taeglich eine Kopie dazu, die GitHub nicht mehr
# loeschen kann. Das Manifest zaehlt nicht mit, es traegt die Bauzeit.
if [ -s previous/manifest.json ] && diff -rq -x manifest.json release previous > /dev/null; then
  python build_catalog.py --status release --failed-bands "${1:-}" > status.md
  echo "- Kacheln unveraendert, kein neuer Katalog veroeffentlicht." >> status.md
  cat status.md
  gh issue comment 1 "${R[@]}" --body-file status.md
  exit 0
fi
TAG="catalog-$(date -u +%Y%m%d-%H%M)"
# Erst als Entwurf: die App sieht den Katalog erst, wenn alles oben ist.
gh release create "$TAG" "${R[@]}" --draft \
  --title "Spot-Katalog $(date -u +%Y-%m-%d)" \
  --notes "Daten © OpenStreetMap-Mitwirkende, ODbL. Standpunkthoehen: Mapterhorn (https://mapterhorn.com/attribution)."
# Je 50 Dateien, dann eine Minute Pause. Rund 1000 Dateien auf einmal
# loesten am 02.10.2026 GitHubs "secondary rate limit" (403) aus.
upload() {
  for try in 1 2 3 4 5; do
    gh release upload "$TAG" "$@" "${R[@]}" --clobber && return 0
    echo "Upload fehlgeschlagen (Versuch $try), warte 5 Minuten"; sleep 300
  done
  return 1
}
FILES=(release/*.bin)
for ((i = 0; i < ${#FILES[@]}; i += 50)); do
  upload "${FILES[@]:i:50}"
  echo "$((i + 50 < ${#FILES[@]} ? i + 50 : ${#FILES[@]})) von ${#FILES[@]} Kacheln oben"
  sleep 60
done
upload release/manifest.json
gh release edit "$TAG" "${R[@]}" --draft=false --latest
python build_catalog.py --status release --failed-bands "${1:-}" > status.md
# Ein Release mit 1000 Dateien loeschen lehnt GitHub seit 13.10.2026 mit
# HTTP 502 ab (auch mit Wiederholung, 15.10. gemessen). Darum nur Warnung, der
# Katalog ist zu diesem Zeitpunkt schon live und die App liest nur "Latest".
: > left.txt
remove() {
  gh release delete "$1" "${R[@]}" --yes --cleanup-tag && return 0
  echo "::warning::Alter Katalog $1 nicht geloescht"
  echo "$1" >> left.txt
}
# Nach Tag sortieren (catalog-JJJJMMTT-HHMM): createdAt ist das Commit-Datum
# und bei allen Releases gleich; bis 01.10.2026 flog so der neueste raus.
# Entwuerfe (abgebrochene Laeufe) haben keinen Wert und gehen mit.
gh release list "${R[@]}" --limit 100 --json tagName,isDraft \
  --jq '(map(select(.isDraft)) | .[].tagName), (map(select(.isDraft | not)) | sort_by(.tagName) | reverse | .[2:] | .[].tagName)' \
  | while read -r old; do remove "$old"; done
LEFT=$(wc -l < left.txt | tr -d ' ')
[ "$LEFT" -gt 0 ] && echo "- Alte Kataloge liegen geblieben: $LEFT (GitHub-Fehler beim Loeschen)." >> status.md
cat status.md
gh issue comment 1 "${R[@]}" --body-file status.md
