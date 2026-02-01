# TrueNAS API Exporter - pulls metrics from TrueNAS REST API
# Based on https://github.com/alexlmiller/truenas-grafana
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN curl -sL https://raw.githubusercontent.com/alexlmiller/truenas-grafana/main/exporter/truenas-exporter.py \
    -o truenas-exporter.py && chmod +x truenas-exporter.py
EXPOSE 9814

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
