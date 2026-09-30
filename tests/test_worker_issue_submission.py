from __future__ import annotations

from datetime import UTC, datetime

import pytest

from devagent.app.worker import IssueAlreadySubmitted, IssueNotRunnable, Worker
from devagent.core.config import AgentConfig
from devagent.db.store import RunRecord, RunStore
from devagent.core.models import Comment, Task


class FakeTracker:
    def __init__(self, task: Task, comments: list[Comment] | None = None) -> None:
        self.task = task
        self.comments = comments or []

    def get_task_from_url(self, issue_url: str) -> Task:
        assert issue_url == self.task.url
        return self.task

    def get_task(self, task_id: str) -> Task:
        assert task_id == self.task.id
        return self.task

    def get_comments(self, task_id: str) -> list[Comment]:
        assert task_id == self.task.id
        return self.comments

    def add_comment(self, task_id: str, text: str) -> None:
        raise AssertionError("submission must not add a comment before the claim node")


def config() -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "github": {"token": "github-token"},
            "repos": [
                {
                    "repo": {"owner": "acme", "name": "widgets"},
                    "base": "main",
                    "commands": {"build": "make", "test": "make test"},
                }
            ],
        }
    )


def issue(*, closed: bool = False, labels: tuple[str, ...] = ()) -> Task:
    return Task(
        id="acme/widgets#42",
        url="https://github.com/acme/widgets/issues/42",
        title="Fix checkout",
        created=datetime(2026, 9, 29, tzinfo=UTC),
        labels=labels,
        status="closed" if closed else "open",
        closed=closed,
        scope={"owner": "acme", "name": "widgets"},
    )


def worker_for(task: Task) -> tuple[Worker, RunStore, list[RunRecord]]:
    store = RunStore.in_memory()
    store.create_schema()
    started: list[RunRecord] = []
    worker = Worker(
        config(),
        FakeTracker(task),
        store,
        start=started.append,
    )
    return worker, store, started


def test_submission_queues_run_with_codex() -> None:
    worker, store, started = worker_for(issue())

    # Act
    result = worker.submit_issue("https://github.com/acme/widgets/issues/42")
    worker.drain()

    # Assert
    assert result.engine == "codex"
    assert result.repo_index == 0
    assert result.status.value == "queued"
    assert started == [store.get_run(result.id)]


def test_duplicate_submission_is_rejected() -> None:
    worker, _, _ = worker_for(issue())
    worker.submit_issue("https://github.com/acme/widgets/issues/42")

    with pytest.raises(IssueAlreadySubmitted, match="already has a run"):
        worker.submit_issue("https://github.com/acme/widgets/issues/42")


def test_closed_issue_is_rejected() -> None:
    worker, _, _ = worker_for(issue(closed=True))

    with pytest.raises(IssueNotRunnable, match="closed"):
        worker.submit_issue("https://github.com/acme/widgets/issues/42")
