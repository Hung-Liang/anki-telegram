"""Static checks of the deployment files (docker itself is not available in the devcontainer)."""

import os
import re
import subprocess
import sys
from pathlib import Path

from ankibot.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def stage(name):
    text = (ROOT / "Dockerfile").read_text()
    parts = re.split(r"^FROM ", text, flags=re.M)
    return next(p for p in parts if re.match(rf"\S+ AS {name}\b", p))


def test_base_creates_non_root_user_owning_data_and_installs_tzdata():
    base = stage("base")
    assert "python:3.12-slim" in base
    assert "tzdata" in base
    assert re.search(r"useradd .*-u \$\{APP_UID\}", base)
    assert "mkdir -p /data" in base and "chown app:app /data" in base


def test_prod_runs_as_app_with_healthcheck():
    prod = stage("prod")
    assert "COPY src ./src" in prod
    assert "USER app" in prod
    assert 'CMD ["python", "-m", "ankibot"]' in prod
    assert re.search(r"HEALTHCHECK .*\n?.*ankibot\.healthcheck", prod)
    assert "ENV HEARTBEAT_FILE=/tmp/ankibot-heartbeat" in prod
    # No test tooling in prod.
    assert ".[dev]" not in prod


def test_prod_image_contains_the_tatoeba_script():
    assert "COPY scripts/build_tatoeba.py ./scripts/build_tatoeba.py" in stage("prod")


def test_compose_service():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert re.search(r"^\s+target: prod$", compose, re.M)
    assert "env_file: .env" in compose
    assert "- ./data:/data" in compose
    assert "restart: unless-stopped" in compose
    assert "TZ" not in compose, "TZ comes from .env"
    grace = int(re.search(r"stop_grace_period: (\d+)s", compose).group(1))
    timeout = Settings.model_fields["shutdown_sync_timeout_seconds"].default
    assert grace > timeout + 10
    assert compose.count("\n  ") and re.findall(r"^  (\w+):$", compose, re.M) == ["bot"], "a single service"


def test_secrets_are_not_shipped():
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert ".env" in ignored and ".env.dev" in ignored and "data" in ignored
    gitignored = (ROOT / ".gitignore").read_text().split()
    assert ".env" in gitignored and "data/" in gitignored


def test_heartbeat_default_matches_between_settings_and_image():
    assert str(Settings.model_fields["heartbeat_file"].default) == "/tmp/ankibot-heartbeat"


def test_healthcheck_runs_without_bot_settings(tmp_path):
    env = {"PATH": os.environ["PATH"], "HEARTBEAT_FILE": str(tmp_path / "hb")}
    result = subprocess.run([sys.executable, "-m", "ankibot.healthcheck"], env=env, timeout=30)
    assert result.returncode == 1
    (tmp_path / "hb").write_text("x")
    result = subprocess.run([sys.executable, "-m", "ankibot.healthcheck"], env=env, timeout=30)
    assert result.returncode == 0
