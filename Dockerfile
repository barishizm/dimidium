# Jarvis phone receptionist: one small container (FastAPI + Pipecat + Telegram long polling).
#
#   docker build -t dimidium .                              # Google speech (default)
#   docker build --build-arg EXTRAS=google,azure -t dimidium .
#
# EXTRAS are the optional speech SDKs from pyproject.toml. Deepgram and ElevenLabs need none.

FROM python:3.12-slim-bookworm AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app
ARG EXTRAS=google
RUN pip install ".[${EXTRAS}]"


FROM python:3.12-slim-bookworm

ARG EXTRAS=google
# setpriv (util-linux, already in the base image) drops root in the entrypoint.
# The Azure Speech SDK loads libasound at import time, even without an audio device.
RUN case ",${EXTRAS}," in \
      *,azure,*) apt-get update \
        && apt-get install -y --no-install-recommends libasound2 \
        && rm -rf /var/lib/apt/lists/* ;; \
    esac \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home app \
    && mkdir -p /srv/data \
    && chown app:app /srv/data

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/app \
    PORT=8000 \
    DB_PATH=/srv/data/dimidium.db \
    PHRASE_CACHE_DIR=/srv/data/phrase_cache

COPY --from=build /opt/venv /opt/venv
WORKDIR /srv
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# For `python scripts/doctor.py` inside the container.
COPY scripts ./scripts

# SQLite database, phrase cache. Mount a volume here so call logs survive redeploys.
VOLUME ["/srv/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4)"

# The entrypoint starts as root only to hand a freshly mounted volume (root-owned on Fly.io
# and Render) to uid 10001, then runs the app as that user. `docker run --user 10001` skips it.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "app.serve"]
# SIGTERM reaches python directly (exec in the entrypoint): active calls hear a goodbye first.
STOPSIGNAL SIGTERM
