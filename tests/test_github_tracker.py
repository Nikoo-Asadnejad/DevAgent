from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from devagent.core.config import GitHubSettings
from devagent.providers.trackers.github import ALLOWLIST, GitHubIssueTracker
from devagent.runtime.http import AllowlistedClient, ForbiddenRequest


def test_reads_github_issue_and_comments() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer secret-token"
        if (
            request.url.path == "/repos/acme/widgets/issues/42"
            and request.method == "GET"
        ):
            return httpx.Response(
                200,
                json={
                    "number": 42,
                    "title": "Fix checkout",
                    "body": "The total is wrong.",
                    "html_url": "https://github.com/acme/widgets/issues/42",
                    "state": "open",
                    "created_at": "2026-09-29T10:00:00Z",
                    "labels": [{"name": "bug"}, {"name": "backend"}],
                },
            )
        if (
            request.url.path == "/repos/acme/widgets/issues/42/comments"
            and request.method == "GET"
        ):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "user": {"login": "nikoo"},
                        "body": "Reproduces on main.",
                        "created_at": "2026-09-29T11:00:00Z",
                    }
                ],
            )
        return httpx.Response(404)

    tracker = GitHubIssueTracker(
        GitHubSettings(token=SecretStr("secret-token")),
        transport=httpx.MockTransport(handle),
    )

    # Act
    task = tracker.get_task_from_url("https://github.com/acme/widgets/issues/42")
    comments = tracker.get_comments(task.id)

    # Assert
    assert task.id == "acme/widgets#42"
    assert task.scope == {"owner": "acme", "name": "widgets"}
    assert task.labels == ("bug", "backend")
    assert task.closed is False
    assert [comment.body for comment in comments] == ["Reproduces on main."]
    assert [request.method for request in requests] == ["GET", "GET"]


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_issue_writes_are_blocked_before_sending(method: str) -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        pytest.fail("issue writes must never reach GitHub")

    client = AllowlistedClient(
        "https://api.github.com",
        ALLOWLIST,
        transport=httpx.MockTransport(unexpected_request),
    )
    try:
        with pytest.raises(ForbiddenRequest):
            client.request(method, "/repos/acme/widgets/issues/42/comments")
    finally:
        client.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/widgets/pull/42",
        "https://example.com/acme/widgets/issues/42",
        "https://github.com/acme/widgets/issues/42?notification=1",
    ],
)
def test_rejects_non_issue_urls(url: str) -> None:
    tracker = GitHubIssueTracker(
        GitHubSettings(token=SecretStr("secret-token")),
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )

    with pytest.raises(ValueError, match="issue_url"):
        tracker.get_task_from_url(url)


def test_rejects_pull_request_returned_by_issues_api() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "number": 42,
                "title": "A pull request",
                "body": "",
                "html_url": "https://github.com/acme/widgets/pull/42",
                "state": "open",
                "created_at": "2026-09-29T10:00:00Z",
                "pull_request": {
                    "url": "https://api.github.com/repos/acme/widgets/pulls/42"
                },
            },
        )

    tracker = GitHubIssueTracker(
        GitHubSettings(token=SecretStr("secret-token")),
        transport=httpx.MockTransport(handle),
    )

    with pytest.raises(ValueError, match="pull request"):
        tracker.get_task_from_url("https://github.com/acme/widgets/issues/42")
