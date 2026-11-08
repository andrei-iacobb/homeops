# TrueNAS API Exporter - pulls metrics from TrueNAS REST API
# Based on https://github.com/alexlmiller/truenas-grafana
FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

WORKDIR /app
COPY app/resources/truenas-exporter.py ./truenas-exporter.py
EXPOSE 9814

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
