# Build: docker build -f deploy/Dockerfile -t artha .
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first so application edits do not invalidate the layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run unprivileged; Cloud Run gives the container a read-only-friendly filesystem anyway.
RUN useradd --create-home --uid 10001 artha && chown -R artha:artha /srv
USER artha

EXPOSE 8080
ENV PORT=8080

# One worker per instance keeps the in-memory store coherent; scale with instances, not workers.
CMD exec uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75
