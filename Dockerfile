# syntax=docker/dockerfile:1

# stmt needs Node.js 24.12 or newer with built-in SQLite.
FROM node:24-bookworm-slim AS node

FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node

COPY stmt-1.0.0.tar.gz /tmp/
RUN tar -xzf /tmp/stmt-1.0.0.tar.gz -C /opt \
    && rm /tmp/stmt-1.0.0.tar.gz \
    && /opt/stmt-1.0.0/bin/stmt contract > /dev/null

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /store /data \
    && chown app:app /store /data

ENV STMT_BIN=/opt/stmt-1.0.0/bin/stmt \
    STMT_DB=/store/rules.sqlite \
    SREDITOR_CHANNELS_DB=/data/channels.sqlite \
    SREDITOR_ASSETS_DIR=/data/assets \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]
CMD ["sreditor", "serve", "--root", "/app", "--host", "0.0.0.0", "--port", "8000"]
