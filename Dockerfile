# Jarvis phone receptionist: one small container (FastAPI + GPT-Live sideband + Telegram).
#
#   docker build -t dimidium .
#
# Audio never passes through this container: Telnyx talks SIP to OpenAI directly.

FROM python:3.12-slim-bookworm AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .


FROM python:3.12-slim-bookworm

# setpriv (util-linux, already in the base image) drops root in the entrypoint.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home app \
    && mkdir -p /srv/data \
    && chown app:app /srv/data

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/app \
    PORT=8000 \
    DB_PATH=/srv/data/dimidium.db

COPY --from=build /opt/venv /opt/venv
WORKDIR /srv
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# For `python scripts/doctor.py` inside the container.
COPY scripts ./scripts

# SQLite database. Mount a volume here so call logs survive redeploys.
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
