# TrueNAS API Exporter - pulls metrics from TrueNAS REST API
# Based on https://github.com/alexlmiller/truenas-grafana
FROM python:3.15.2-slim@sha256:e97b88e416c3c01eb427b21fd8cd3ebf006e6ee38afcfcc3cae1c54ee2ab0a75

WORKDIR /app
COPY app/resources/truenas-exporter.py ./truenas-exporter.py
EXPOSE 9814

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
