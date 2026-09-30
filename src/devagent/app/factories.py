"""Build the GitHub adapters and Codex engine."""

from collections.abc import Iterator, Mapping

from pydantic import BaseModel, SecretStr

from devagent.core.config import AgentConfig
from devagent.core.guardrails import secret_fragments
from devagent.providers.code_hosts.base import CodeHost
from devagent.providers.code_hosts.github import GitHubHost
from devagent.providers.engines.acp import CodexEngine
from devagent.providers.engines.base import CodingEngine
from devagent.providers.trackers.base import TaskTracker
from devagent.providers.trackers.github import GitHubIssueTracker


def build_tracker(cfg: AgentConfig) -> TaskTracker:
    return GitHubIssueTracker(cfg.github)


def build_code_host(cfg: AgentConfig) -> CodeHost:
    return GitHubHost(cfg.github)


def build_engine(cfg: AgentConfig, environ: Mapping[str, str]) -> CodingEngine:
    return CodexEngine(cfg.codex, cfg.codex.credentials(environ))


def _secret_values(model: BaseModel) -> Iterator[str]:
    for value in model.__dict__.values():
        if isinstance(value, SecretStr):
            yield value.get_secret_value()
        elif isinstance(value, BaseModel):
            yield from _secret_values(value)
        elif isinstance(value, dict):
            for item in value.values():  # pyright: ignore[reportUnknownVariableType]
                if isinstance(item, BaseModel):
                    yield from _secret_values(item)


def collect_secrets(cfg: AgentConfig, environ: Mapping[str, str]) -> list[str]:
    """Every secret value the service knows, so logs, comments and PR bodies can be redacted."""
    values = list(_secret_values(cfg))
    for credential in cfg.codex.credentials(environ).values():
        raw = credential.get_secret_value()
        values += [raw, *secret_fragments(raw)]
    return values
