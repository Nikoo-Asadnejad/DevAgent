# Common sandbox: OpenHands agent server, Codex ACP, and native .NET dependencies.
# Build:  docker build -f docker/sandbox.Dockerfile -t devagent-sandbox:latest .
# Pin AGENT_SERVER_TAG to the openhands-sdk version in uv.lock for reproducible runs.
ARG AGENT_SERVER_TAG=latest-python
FROM ghcr.io/openhands/agent-server:${AGENT_SERVER_TAG}

USER root

# The agent-server image may already include Codex ACP. Install it only when absent.
RUN set -eu; \
    if ! command -v codex-acp >/dev/null 2>&1; then \
      if ! command -v npm >/dev/null 2>&1; then \
        apt-get update && apt-get install -y --no-install-recommends nodejs npm && rm -rf /var/lib/apt/lists/*; \
      fi; \
      npm install -g @agentclientprotocol/codex-acp; \
      npm cache clean --force; \
    fi; \
    codex-acp --version

# SDKs and runtimes are installed into cached derived images after inspecting each repository.
# ICU belongs in this common image so globalization works for every selected .NET version.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates libicu-dev \
    && rm -rf /var/lib/apt/lists/*
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
