import base64
from typing import Protocol

from pydantic import SecretStr

from devagent.core.models import PrRef, PrSpec, RepoRef


class CodeHost(Protocol):
    """Deliberately has no merge / complete / approve / auto-complete / abandon / delete operations."""

    def clone_url(self, repo: RepoRef) -> str: ...

    def push_auth(self, repo: RepoRef) -> dict[str, SecretStr]:
        """Git environment used only by the orchestrator's clone/push (never written to .git/config)."""
        ...

    def create_draft_pull_request(self, spec: PrSpec) -> PrRef: ...

    def find_open_pr(self, repo: RepoRef, source_branch: str) -> PrRef | None:
        """Open PR from `source_branch`, used to make publishing idempotent."""
        ...


def basic_auth_git_env(username: str, token: SecretStr) -> dict[str, SecretStr]:
    """Git env that sends HTTP basic auth via `http.extraheader` without persisting it in the repo config."""
    raw = f"{username}:{token.get_secret_value()}".encode()
    header = f"AUTHORIZATION: Basic {base64.b64encode(raw).decode()}"
    return {
        "GIT_CONFIG_COUNT": SecretStr("1"),
        "GIT_CONFIG_KEY_0": SecretStr("http.extraheader"),
        "GIT_CONFIG_VALUE_0": SecretStr(header),
    }
