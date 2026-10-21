# The agent plane for Codex workers (spec D02: agents run in their own sandbox and may be root
# there; the control plane holds keys, the ledger and verdicts).
#
# One container per worker task. It sees only the request's work/ folder (mounted at
# /workspace), reaches the toolkit over MCP at the host's tool gateway, and reaches the model
# through the host's model gateway, which holds the real API key. No vendor key is in the image
# or the container.
FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.162.1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git ripgrep jq python3 python3-venv python3-pip python3-dev build-essential curl \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @openai/codex@${CODEX_VERSION} \
 && npm cache clean --force

RUN useradd --create-home --uid 1001 agent && mkdir -p /home/agent/.codex && chown -R agent /home/agent/.codex
USER agent
ENV CODEX_HOME=/home/agent/.codex
WORKDIR /workspace
ENTRYPOINT ["codex"]
