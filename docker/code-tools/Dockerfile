# Third-party language servers only. Project scripts/plugins are never installed
# or executed by Workbench on the host. Keep JS application code in Remotion.
FROM node:24.19.0-bookworm-slim@sha256:a9f5f7c91a432850b2a8a7797adf5eadb6c733ceed61167806cee7ea7fbc29df
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/vwb/lsp
COPY package.json package-lock.json ./
RUN npm ci --omit=optional --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force \
    && useradd --uid 1001 --create-home agent
ENV PATH="/opt/vwb/lsp/node_modules/.bin:${PATH}"
USER 1001:1001
WORKDIR /workspace
