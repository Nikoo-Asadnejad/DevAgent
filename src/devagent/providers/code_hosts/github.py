"""GitHub code host (github.com and GitHub Enterprise Server via `api_url` / `web_url`).

Only two calls leave this module: `GET /repos/{owner}/{repo}/pulls` and
`POST /repos/{owner}/{repo}/pulls` to create a *draft* PR.
There is no merge / approve / auto-merge / close / delete capability (guardrails #3, #5, #6).
"""

from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, TypeAdapter

from devagent.core.config import GitHubSettings
from devagent.core.models import PrRef, PrSpec, RepoRef
from devagent.runtime.http import AllowlistedClient, AllowRule
from devagent.providers.code_hosts.base import basic_auth_git_env

_SNIPPET = 300
_PUSH_USERNAME = "x-access-token"

RULES = (
    AllowRule("GET", "/repos/{owner}/{repo}/pulls"),
    AllowRule("POST", "/repos/{owner}/{repo}/pulls"),
)


class GitHubError(Exception):
    """Non-2xx response from GitHub; the message never contains the token."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _GhPullRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    number: int
    html_url: str


_PR_LIST = TypeAdapter(list[_GhPullRequest])


def _segment(value: str) -> str:
    return quote(value, safe="")


class GitHubHost:
    def __init__(
        self, settings: GitHubSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._web_url = settings.web_url.rstrip("/")
        self._token = settings.token
        self._client = AllowlistedClient(
            settings.api_url,
            RULES,
            headers={
                "Authorization": f"Bearer {settings.token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            transport=transport,
        )

    # --- CodeHost ----------------------------------------------------------------------------------------------

    def clone_url(self, repo: RepoRef) -> str:
        owner, name = self._coordinates(repo)
        return f"{self._web_url}/{_segment(owner)}/{_segment(name)}.git"

    def push_auth(self, repo: RepoRef) -> dict[str, SecretStr]:
        return basic_auth_git_env(_PUSH_USERNAME, self._token)

    def create_draft_pull_request(self, spec: PrSpec) -> PrRef:
        owner, name = self._coordinates(spec.repo)
        body = {
            "title": spec.title,
            "body": spec.body,
            "head": spec.source_branch,
            "base": spec.target_branch,
            "draft": True,
        }
        response = self._send("POST", self._pulls_path(owner, name), json=body)
        return self._pr_ref(_GhPullRequest.model_validate_json(response.content))

    def find_open_pr(self, repo: RepoRef, source_branch: str) -> PrRef | None:
        owner, name = self._coordinates(repo)
        params = {"head": f"{owner}:{source_branch}", "state": "open"}
        response = self._send("GET", self._pulls_path(owner, name), params=params)
        found = _PR_LIST.validate_json(response.content)
        return self._pr_ref(found[0]) if found else None

    def close(self) -> None:
        self._client.close()

    # --- helpers -----------------------------------------------------------------------------------------------

    @staticmethod
    def _coordinates(repo: RepoRef) -> tuple[str, str]:
        missing = [key for key in ("owner", "name") if not repo.coordinates.get(key)]
        if missing:
            raise ValueError(
                f"GitHub repo coordinates must include {', '.join(repr(k) for k in missing)} "
                f"(got keys: {sorted(repo.coordinates)})"
            )
        return repo.coordinates["owner"], repo.coordinates["name"]

    @staticmethod
    def _pulls_path(owner: str, name: str) -> str:
        return f"/repos/{_segment(owner)}/{_segment(name)}/pulls"

    @staticmethod
    def _pr_ref(pr: _GhPullRequest) -> PrRef:
        return PrRef(id=str(pr.number), url=pr.html_url)

    def _send(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        if not response.is_success:
            raise GitHubError(
                f"GitHub {method} {path} failed with HTTP {response.status_code}: {self._snippet(response)}",
                status_code=response.status_code,
            )
        return response

    def _snippet(self, response: httpx.Response) -> str:
        text = response.text
        token = self._token.get_secret_value()
        if token:
            text = text.replace(token, "***")
        return " ".join(text.split())[:_SNIPPET]
