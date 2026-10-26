#!/usr/bin/env bash
# Deploy or update: prepare data/, bootstrap the collection if missing, (re)start the bot, build the example database if missing.
# Safe to re-run after `git pull`; existing collection.anki2 and tatoeba.db are left alone.
# Usage: scripts/deploy.sh [--skip-bootstrap] [--skip-tatoeba]
set -euo pipefail

cd "$(dirname "$0")/.."

skip_bootstrap=0
skip_tatoeba=0
for arg in "$@"; do
  case "$arg" in
    --skip-bootstrap) skip_bootstrap=1 ;;
    --skip-tatoeba) skip_tatoeba=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }

[[ -f .env ]] || die ".env not found; copy it from .env.example and fill it in"

endpoint=$(grep -E '^ANKI_SYNC_ENDPOINT=' .env | tail -n1 | cut -d= -f2- || true)
[[ -n "$endpoint" ]] || die "ANKI_SYNC_ENDPOINT is empty in .env"
[[ "$endpoint" != *anki-sync:8080* ]] || die "ANKI_SYNC_ENDPOINT points at the devcontainer local-sync server: $endpoint"

tz=$(grep -E '^TZ=' .env | tail -n1 | cut -d= -f2- || true)
if [[ -z "$tz" || "$tz" == "UTC" ]]; then
  echo "WARNING: TZ is '${tz:-unset}'; /stats counts 'today' in UTC"
fi

echo "==> Preparing data/ (owned by uid/gid 1000)"
mkdir -p data
if [[ "$(stat -c '%u:%g' data)" != "1000:1000" ]]; then
  sudo chown 1000:1000 data
fi
df -h data

echo "==> Building image"
docker compose build

if [[ $skip_bootstrap -eq 0 ]]; then
  if [[ -f data/collection.anki2 ]]; then
    echo "==> data/collection.anki2 exists; skipping bootstrap"
  else
    echo "==> Bootstrap (dry run)"
    docker compose run --rm bot python -m ankibot.bootstrap
    read -r -p "Apply bootstrap? [y/N] " answer
    [[ "$answer" =~ ^[Yy]$ ]] || die "bootstrap aborted"
    docker compose run --rm bot python -m ankibot.bootstrap --yes
  fi
fi

echo "==> Starting bot"
docker compose up -d

if [[ $skip_tatoeba -eq 0 ]]; then
  if [[ -f data/tatoeba.db ]]; then
    echo "==> data/tatoeba.db exists; skipping Tatoeba build"
  else
    echo "==> Building Tatoeba example database"
    docker compose run --rm bot python scripts/build_tatoeba.py
  fi
fi

docker compose ps
echo "Done. Follow logs with: docker compose logs -f bot"
