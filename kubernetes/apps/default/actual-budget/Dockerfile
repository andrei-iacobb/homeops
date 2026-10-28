# Build for linux/amd64. Preserve the deployed Actual 26.10.0 application.
FROM node:26.12.1-bookworm-slim@sha256:88e480776c6149ff1d098cd304f44bf18057fb5d284a3329b78162d29821e28c AS patched-node
FROM ghcr.io/actualbudget/actual-server:26.10.0@sha256:24645e971da6bb1a1f953d6859e307a0b29be1e8b4130a35b3220c209b5b860c

# This directory contains only Node tooling; replace it rather than merging npm files.
RUN rm -rf /usr/local
COPY --from=patched-node /usr/local/ /usr/local/
ENV NODE_VERSION=24.22.1
