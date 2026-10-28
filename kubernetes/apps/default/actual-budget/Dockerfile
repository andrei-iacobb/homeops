# Build for linux/amd64. Preserve the deployed Actual 26.10.0 application.
FROM node:24.22.1-bookworm-slim@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c AS patched-node
FROM ghcr.io/actualbudget/actual-server:26.10.0@sha256:24645e971da6bb1a1f953d6859e307a0b29be1e8b4130a35b3220c209b5b860c

# This directory contains only Node tooling; replace it rather than merging npm files.
RUN rm -rf /usr/local
COPY --from=patched-node /usr/local/ /usr/local/
ENV NODE_VERSION=24.22.1
