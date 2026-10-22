FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

RUN useradd --create-home appuser && mkdir -p /srv/data && chown appuser /srv/data
USER appuser

ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
