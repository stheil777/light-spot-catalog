#!/usr/bin/env bash
# Veroeffentlicht release/ als neuen Katalog, setzt den Stand ins Issue 1 und
# behaelt die zwei neuesten Kataloge. Aufruf: publish.sh "<ausgefallene Jobs>"
set -euo pipefail
TAG="catalog-$(date -u +%Y%m%d-%H%M)"
gh release create "$TAG" release/* \
  --repo "$GITHUB_REPOSITORY" \
  --title "Spot-Katalog $(date -u +%Y-%m-%d)" \
  --notes "Daten © OpenStreetMap-Mitwirkende, ODbL. Standpunkthoehen: Mapterhorn (https://mapterhorn.com/attribution)." \
  --latest
# Nach Tag sortieren (catalog-JJJJMMTT-HHMM): createdAt ist das Commit-Datum
# und bei allen Releases gleich; bis 01.10.2026 flog so der neueste raus.
gh release list --repo "$GITHUB_REPOSITORY" --limit 100 --json tagName \
  --jq 'sort_by(.tagName) | reverse | .[2:] | .[].tagName' \
  | while read -r old; do
      gh release delete "$old" --repo "$GITHUB_REPOSITORY" --yes --cleanup-tag
    done
python build_catalog.py --status release --failed-bands "${1:-}" > status.md
cat status.md
gh issue comment 1 --repo "$GITHUB_REPOSITORY" --body-file status.md
