# Per-run sandbox: OpenHands agent server + Codex ACP CLI + the toolchains your repos need to build/test.
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

# --- repo toolchains -------------------------------------------------------------------------------------------------
# Add what your repos' bootstrap/build/test commands need. Example: .NET 8 SDK for `dotnet restore/build/test`.
ARG INSTALL_DOTNET=true
RUN if [ "$INSTALL_DOTNET" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends wget ca-certificates \
      && wget -q https://dot.net/v1/dotnet-install.sh -O /tmp/dotnet-install.sh \
      && bash /tmp/dotnet-install.sh --channel 8.0 --install-dir /usr/share/dotnet \
      && ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet \
      && rm -rf /tmp/dotnet-install.sh /var/lib/apt/lists/*; \
    fi
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
