# Build for linux/amd64. Preserve the deployed Actual 26.10.0 application.
ARG NODE_VERSION=24.22.1
FROM node:${NODE_VERSION}-bookworm-slim@sha256:be8204d4bd327363357f532187b752ff62759c31bedf98e9e18fc08c9cbd605c AS patched-node
# The unchanged application's native modules were built for Node 24.
RUN node -e "if (process.versions.modules !== '137') throw new Error('Actual 26.10.0 requires Node ABI 137')"
FROM ghcr.io/actualbudget/actual-server:26.11.0@sha256:06cf186569cf58d93e12659ff1a517b65b5b0ebee7050a3991d4bd8a1d398da6
ARG NODE_VERSION

# This directory contains only Node tooling; replace it rather than merging npm files.
RUN rm -rf /usr/local
COPY --from=patched-node /usr/local/ /usr/local/
ENV NODE_VERSION=${NODE_VERSION}
