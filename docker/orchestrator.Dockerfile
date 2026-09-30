# devagent orchestrator: FastAPI + worker. Talks to the host Docker daemon to start per-run sandboxes.
FROM python:3.13-slim-bookworm

ARG GITLEAKS_VERSION=8.28.0
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl gnupg \
 # Docker CLI only (the daemon is the host's, via the mounted socket); OpenHands DockerWorkspace shells out to it.
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
 && BUILD_ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" \
 && case "${BUILD_ARCH}" in amd64) GITLEAKS_ARCH=x64 ;; arm64) GITLEAKS_ARCH=arm64 ;; *) echo "unsupported architecture: ${BUILD_ARCH}" >&2; exit 1 ;; esac \
 && curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${GITLEAKS_ARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin gitleaks \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}" \
    DEVAGENT_CONFIG=/etc/devagent/agent.yaml \
    PYTHONUNBUFFERED=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    CUSTOM_TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

# openhands-sdk imports LiteLLM, which loads tiktoken cl100k_base at import time; fetch it once at build time so
# the service never needs outbound access for it at runtime.
RUN python -c "import litellm.litellm_core_utils.default_encoding" && ls /opt/tiktoken-cache

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1
ENTRYPOINT ["devagent"]
CMD ["serve"]
