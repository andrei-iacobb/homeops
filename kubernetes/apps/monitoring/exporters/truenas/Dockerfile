# TrueNAS API Exporter - pulls metrics from TrueNAS REST API
# Based on https://github.com/alexlmiller/truenas-grafana
FROM python:3.15.2-slim@sha256:c4158543ecb725b99e57d129dfd43d761d4c43e4577b5e23326aabb223cf7f4e

WORKDIR /app
COPY app/resources/truenas-exporter.py ./truenas-exporter.py
EXPOSE 9814

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
