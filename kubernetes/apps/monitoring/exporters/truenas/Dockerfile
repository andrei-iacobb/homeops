# TrueNAS API Exporter - pulls metrics from TrueNAS REST API
# Based on https://github.com/alexlmiller/truenas-grafana
FROM python:3.15.2-slim@sha256:8c13a3f37a6a5b71498228e902c4b9f51c1f7900d49fabf71f9a650c6120e0d0

WORKDIR /app
COPY app/resources/truenas-exporter.py ./truenas-exporter.py
EXPOSE 9814

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
