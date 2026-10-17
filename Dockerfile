# Build: docker build -t ledgerlens .
# One image, three entry points:
#   API      (default)  uvicorn app.api.main:app
#   worker              python -m app.worker
#   migrate             alembic upgrade head
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt ./
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PORT=8080

WORKDIR /srv
COPY --from=build /opt/venv /opt/venv
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY tools ./tools

# Run unprivileged, with nothing writable in the application tree.
RUN useradd --create-home --uid 10001 artha
USER artha

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/api/healthz', timeout=2)" || exit 1

# State lives in PostgreSQL, so the API scales with workers and instances alike.
CMD exec uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-2} \
    --timeout-keep-alive 75 --proxy-headers --forwarded-allow-ips="*"
