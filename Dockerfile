# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    TZ=UTC
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} app \
    && useradd -m -u ${APP_UID} -g ${APP_GID} -s /bin/bash app \
    && mkdir -p /data \
    && chown app:app /data
RUN python -m venv /opt/venv
WORKDIR /app
# Install dependencies against a stub package so this layer is cached until pyproject.toml changes.
COPY pyproject.toml README.md ./
RUN mkdir -p src/ankibot && touch src/ankibot/__init__.py \
    && pip install . \
    && pip uninstall -y ankibot \
    && rm -rf src build

FROM base AS dev
RUN apt-get update \
    && apt-get install -y --no-install-recommends git make \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p src/ankibot && touch src/ankibot/__init__.py \
    && pip install ".[dev]" \
    && pip uninstall -y ankibot \
    && rm -rf src build \
    && chown -R app:app /opt/venv
USER app

FROM base AS prod
COPY src ./src
# The only script needed in production (building /data/tatoeba.db); the rest of scripts/ is dev-only.
COPY scripts/build_tatoeba.py ./scripts/build_tatoeba.py
RUN pip install --no-deps . && rm -rf build src/*.egg-info
USER app
ENV HEARTBEAT_FILE=/tmp/ankibot-heartbeat
VOLUME ["/data"]
# Healthy = the event loop is alive and Telegram's getMe succeeded within the last 3 minutes.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "ankibot.healthcheck"]
STOPSIGNAL SIGTERM
# Exec form: python is PID 1 and receives SIGTERM directly (it installs its own handler).
CMD ["python", "-m", "ankibot"]
