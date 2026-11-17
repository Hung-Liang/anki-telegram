#!/usr/bin/env bash
# Import a JSON word list into the running bot's collection.
# Usage: scripts/import.sh <file.json>
set -euo pipefail

[ $# -eq 1 ] || { echo "Usage: $0 <file.json>" >&2; exit 2; }
[ -f "$1" ] || { echo "ERROR: file not found: $1" >&2; exit 2; }

file=$(realpath "$1")
cd "$(dirname "$0")/.."
exec docker compose exec -T bot python -m ankibot.importer < "$file"
