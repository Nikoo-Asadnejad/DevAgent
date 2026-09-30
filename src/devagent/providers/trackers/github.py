"""GitHub issues as the task source.

The adapter can read issues and comments and add a comment. It cannot edit labels,
state, assignees, milestones, or issue text.
"""

import re
from datetime import datetime
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from devagent.core.config import GitHubSettings
from devagent.core.models import Comment, Task
from devagent.runtime.http import AllowlistedClient, AllowRule
from devagent.providers.trackers.errors import TrackerRequestError

ALLOWLIST = (
    AllowRule("GET", "/repos/{owner}/{repo}/issues/{number}"),
    AllowRule("GET", "/repos/{owner}/{repo}/issues/{number}/comments"),
    AllowRule("POST", "/repos/{owner}/{repo}/issues/{number}/comments"),
)

_ISSUE_PATH = re.compile(r"^/([^/]+)/([^/]+)/issues/([1-9][0-9]*)/?$")
_TASK_ID = re.compile(r"^([^/]+)/([^/#]+)#([1-9][0-9]*)$")
_COMMENTS_PER_PAGE = 100
_MAX_COMMENT_PAGES = 10


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Label(_Payload):
    name: str


class _Issue(_Payload):
    number: int
    title: str
    body: str | None = None
    html_url: str
    state: str
    created_at: datetime
    labels: list[_Label] = Field(default_factory=list[_Label])
    pull_request: dict[str, object] | None = None


class _User(_Payload):
    login: str


class _IssueComment(_Payload):
    id: int
    user: _User
    body: str
    created_at: datetime

    def to_comment(self) -> Comment:
        return Comment(
            id=str(self.id),
            author_id=self.user.login,
            body=self.body,
            created=self.created_at,
        )


_COMMENT_LIST = TypeAdapter(list[_IssueComment])


def _segment(value: str) -> str:
    return quote(value, safe="")


class GitHubIssueTracker:
    def __init__(
        self, settings: GitHubSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._web_url = urlsplit(settings.web_url.rstrip("/"))
        self._http = AllowlistedClient(
            settings.api_url,
            ALLOWLIST,
            headers={
                "Authorization": f"Bearer {settings.token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def get_task_from_url(self, issue_url: str) -> Task:
        owner, repo, number = self._parse_url(issue_url)
        return self._get_issue(owner, repo, number)

    def get_task(self, task_id: str) -> Task:
        owner, repo, number = self._parse_task_id(task_id)
        return self._get_issue(owner, repo, number)

    def get_comments(self, task_id: str) -> list[Comment]:
        owner, repo, number = self._parse_task_id(task_id)
        path = self._comments_path(owner, repo, number)
        found: list[Comment] = []
        for page in range(1, _MAX_COMMENT_PAGES + 1):
            response = self._call(
                "GET", path, params={"per_page": _COMMENTS_PER_PAGE, "page": page}
            )
            batch = _COMMENT_LIST.validate_json(response.content)
            found.extend(item.to_comment() for item in batch)
            if len(batch) < _COMMENTS_PER_PAGE:
                break
        return sorted(found, key=lambda comment: comment.created)

    def add_comment(self, task_id: str, text: str) -> None:
        owner, repo, number = self._parse_task_id(task_id)
        self._call(
            "POST", self._comments_path(owner, repo, number), json={"body": text}
        )

    def _get_issue(self, owner: str, repo: str, number: int) -> Task:
        path = self._issue_path(owner, repo, number)
        issue = _Issue.model_validate_json(self._call("GET", path).content)
        if issue.pull_request is not None:
            raise ValueError(
                "the submitted GitHub URL points to a pull request, not an issue"
            )
        return Task(
            id=f"{owner}/{repo}#{issue.number}",
            url=issue.html_url,
            title=issue.title,
            description=issue.body or "",
            created=issue.created_at,
            labels=tuple(label.name for label in issue.labels),
            status=issue.state,
            closed=issue.state.casefold() != "open",
            scope={"owner": owner, "name": repo},
        )

    def _parse_url(self, issue_url: str) -> tuple[str, str, int]:
        parsed = urlsplit(issue_url)
        if (
            parsed.scheme.casefold() != self._web_url.scheme.casefold()
            or parsed.netloc.casefold() != self._web_url.netloc.casefold()
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                f"issue_url must be a plain issue URL on {self._web_url.geturl()}"
            )
        match = _ISSUE_PATH.fullmatch(parsed.path)
        if match is None:
            raise ValueError(
                "issue_url must have the form https://github.com/<owner>/<repo>/issues/<number>"
            )
        owner, repo, number = match.groups()
        owner, repo = unquote(owner), unquote(repo)
        if any(character in owner or character in repo for character in ("/", "#")):
            raise ValueError("issue_url contains invalid repository coordinates")
        return owner, repo, int(number)

    @staticmethod
    def _parse_task_id(task_id: str) -> tuple[str, str, int]:
        match = _TASK_ID.fullmatch(task_id)
        if match is None:
            raise ValueError(f"invalid GitHub issue task id: {task_id!r}")
        owner, repo, number = match.groups()
        return owner, repo, int(number)

    @staticmethod
    def _issue_path(owner: str, repo: str, number: int) -> str:
        return f"/repos/{_segment(owner)}/{_segment(repo)}/issues/{number}"

    @classmethod
    def _comments_path(cls, owner: str, repo: str, number: int) -> str:
        return cls._issue_path(owner, repo, number) + "/comments"

    def _call(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        response = self._http.request(method, path, **kwargs)
        if not response.is_success:
            raise TrackerRequestError(method, path, response.status_code)
        return response
