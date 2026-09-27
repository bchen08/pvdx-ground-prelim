# The cloud telemetry service (pvdx-serve): SatNOGS ingest, decode, InfluxDB + Redis output, HTTP API.
# Build and run with `docker compose --profile cloud up -d --build`; state lives in the /data volume.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pvdx_ground ./pvdx_ground
RUN pip install --no-cache-dir .

ENV STATE_DB=/data/state.db \
    API_HOST=0.0.0.0 \
    API_PORT=8080 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

CMD ["pvdx-serve"]
