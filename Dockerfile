# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

ARG APP_VERSION=1.0.0
ARG BUILD_DATE=unknown

WORKDIR /app

COPY app/ ./app/
COPY tests/ ./tests/
COPY verify/ ./verify/

# Bake build provenance into the image; the verify service cross-checks this
# file against what the running app container reports via /version.
RUN printf '{"name":"bwf-clip-api","version":"%s","buildDate":"%s","builder":"dockerfile"}\n' \
        "$APP_VERSION" "$BUILD_DATE" > /app/build-info.json \
    && python -m compileall -q app tests verify \
    && useradd --system --uid 10001 --no-create-home appuser

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
    CMD python -c "import os,sys,urllib.request;u='http://127.0.0.1:%s/healthz'%os.environ.get('PORT','8080');sys.exit(0 if urllib.request.urlopen(u,timeout=2).status==200 else 1)"

CMD ["python", "-m", "app.server"]
