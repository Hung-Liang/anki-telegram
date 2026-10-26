PYTHON ?= python
SPIKE_USER ?= user
SPIKE_PASSWORD ?= pass
DEV_COMPOSE = docker compose -f .devcontainer/docker-compose.dev.yml

.PHONY: test test-integration lint format run verify-sync verify-sync-readonly build-tatoeba bootstrap local-sync-up local-sync-reset

test:
	$(PYTHON) -m pytest -m "not integration"

test-integration:
	$(PYTHON) -m pytest -m integration

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

run:
	$(PYTHON) -m ankibot

# Full read/write verification against ANKI_SYNC_ENDPOINT; needs an empty account on the local-sync server.
verify-sync:
	$(PYTHON) scripts/spike_sync.py local --user $(SPIKE_USER) --password $(SPIKE_PASSWORD)

# Read-only against ANKI_SYNC_ENDPOINT (login, full download, no-op sync); uses ANKI_USER / ANKI_PASSWORD.
verify-sync-readonly:
	$(PYTHON) scripts/spike_sync.py readonly

build-tatoeba:
	$(PYTHON) scripts/build_tatoeba.py

bootstrap:
	$(PYTHON) -m ankibot.bootstrap

# Run these two on the host, not inside the devcontainer.
local-sync-up:
	$(DEV_COMPOSE) --profile local-sync up -d anki-sync

local-sync-reset:
	$(DEV_COMPOSE) --profile local-sync rm -sf anki-sync
	docker volume rm -f anki-telegram-dev_sync-data
	$(DEV_COMPOSE) --profile local-sync up -d anki-sync
