"""Load `agent.yaml` into a validated, typed `AgentConfig`.

YAML is parsed first, then `${VAR}` placeholders in string values are substituted from the environment, then the
result is validated by Pydantic. Secrets are `SecretStr` and never rendered.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Self, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)


class ConfigError(Exception):
    """Raised when the configuration is invalid; the message never contains secret values."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GitHubSettings(Settings):
    """The only external provider: issues in, branches and draft pull requests out."""

    token: SecretStr
    api_url: str = "https://api.github.com"
    web_url: str = "https://github.com"


# --- Codex --------------------------------------------------------------------------------------------------------


class CodexConfig(Settings):
    command: list[str] = Field(
        default_factory=lambda: ["codex-acp"],
        min_length=1,
    )
    env: list[str] = Field(default_factory=lambda: ["CODEX_AUTH_JSON"])
    """Names of environment variables whose values are injected into the sandbox as secrets."""
    extra_env: dict[str, str] = Field(default_factory=lambda: {"NO_BROWSER": "1"})

    def credentials(self, environ: Mapping[str, str]) -> dict[str, SecretStr]:
        missing = [name for name in self.env if not environ.get(name)]
        if missing:
            raise ConfigError(
                f"missing Codex credential env var(s): {', '.join(missing)}"
            )
        return {name: SecretStr(environ[name]) for name in self.env}


# --- platform ------------------------------------------------------------------------------------------------------


class BudgetsConfig(Settings):
    max_concurrent: int = Field(default=1, ge=1)
    max_runs_per_day: int = Field(default=10, ge=1)
    max_minutes_per_run: int = Field(default=45, gt=0)
    max_usd_per_run: float | None = Field(default=None, gt=0)


DEFAULT_PROTECTED_PATHS = (
    ".github/**",
    "**/.gitlab-ci.yml",
    "**/Jenkinsfile",
    "**/.gitattributes",
    "**/.gitmodules",
    "**/*.env",
    "**/.env*",
)


class GuardrailsConfig(Settings):
    branch_prefix: str = "agent/"
    protected_branches: list[str] = Field(
        default_factory=lambda: ["main", "master", "develop", "release/*"]
    )
    protected_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PROTECTED_PATHS)
    )

    @field_validator("branch_prefix")
    @classmethod
    def _prefix_ends_with_slash(cls, value: str) -> str:
        if not value.endswith("/") or value == "/":
            raise ValueError(
                "branch_prefix must be a non-empty namespace ending with '/', e.g. 'agent/'"
            )
        return value


class SandboxConfig(Settings):
    image: str = "devagent-sandbox:latest"
    """OpenHands agent-server image with the Codex ACP CLI and repo toolchains installed."""
    workspaces_dir: str = "/var/lib/devagent/workspaces"
    """Where the orchestrator clones repos (one directory per run)."""
    host_workspaces_dir: str | None = None
    """The same directory as seen by the Docker host, when the orchestrator itself runs in a container."""
    network: str | None = None
    command_timeout_minutes: int = Field(default=30, gt=0)
    instance: str = Field(default="default", pattern=r"^[A-Za-z0-9_.-]{1,63}$")
    """Labels this deployment's sandboxes, so orphan cleanup never touches another deployment on the same host."""


class GitIdentityConfig(Settings):
    author_name: str = "devagent"
    author_email: str = "devagent@localhost"


class ServiceConfig(Settings):
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    admin_token: SecretStr | None = None
    """Bearer token for /issues and /runs; those endpoints are disabled when unset."""
    log_level: str = "INFO"


class RepoCommands(Settings):
    bootstrap: str | None = None
    build: str
    test: str


class RepoConfig(Settings):
    repo: dict[str, str] = Field(min_length=1)
    base: str
    commands: RepoCommands

    @model_validator(mode="after")
    def _github_coordinates(self) -> Self:
        if set(self.repo) != {"owner", "name"} or not all(self.repo.values()):
            raise ValueError(
                "repo must contain exactly non-empty 'owner' and 'name' values"
            )
        return self


class AgentConfig(Settings):
    github: GitHubSettings
    codex: CodexConfig = Field(default_factory=CodexConfig)
    budgets: BudgetsConfig = BudgetsConfig()
    guardrails: GuardrailsConfig = GuardrailsConfig()
    sandbox: SandboxConfig = SandboxConfig()
    git: GitIdentityConfig = GitIdentityConfig()
    service: ServiceConfig = ServiceConfig()
    repos: list[RepoConfig] = Field(min_length=1)


# --- loading -------------------------------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(
    value: object, environ: Mapping[str, str], missing: set[str]
) -> object:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in environ:
                missing.add(name)
                return ""
            return environ[name]

        return _PLACEHOLDER.sub(replace, value)
    if isinstance(value, dict):
        items = cast(dict[object, object], value)
        return {k: _interpolate(v, environ, missing) for k, v in items.items()}
    if isinstance(value, list):
        return [_interpolate(v, environ, missing) for v in cast(list[object], value)]
    return value


def _format_errors(error: ValidationError) -> str:
    lines: list[str] = []
    for item in error.errors(
        include_input=False, include_url=False, include_context=False
    ):
        path = ".".join(str(part) for part in item["loc"])
        lines.append(f"{path}: {item['msg']}" if path else item["msg"])
    return "invalid config:\n  " + "\n  ".join(lines)


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> AgentConfig:
    env = os.environ if environ is None else environ
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from None

    missing: set[str] = set()
    data = _interpolate(raw, env, missing)
    if missing:
        raise ConfigError(
            f"missing environment variable(s): {', '.join(sorted(missing))}"
        )

    try:
        config = AgentConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc)) from None

    config.codex.credentials(env)
    return config
