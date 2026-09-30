from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from devagent.app.api import create_app
from devagent.db.store import RunRecord, RunStore
from devagent.core.models import Task


class FakeWorker:
    def __init__(self, run: RunRecord) -> None:
        self.run = run
        self.submitted: list[str] = []

    def submit_issue(self, issue_url: str) -> RunRecord:
        self.submitted.append(issue_url)
        return self.run

    def request_retry(self, run_id: str) -> str:
        return run_id


def test_issue_endpoint_requires_token_and_returns_run_id() -> None:
    store = RunStore.in_memory()
    store.create_schema()
    task = Task(
        id="acme/widgets#42",
        url="https://github.com/acme/widgets/issues/42",
        title="Fix checkout",
        created=datetime(2026, 9, 29, tzinfo=UTC),
        status="open",
        scope={"owner": "acme", "name": "widgets"},
    )
    run = store.create_run(task, engine="codex", repo_index=0)
    assert run is not None
    worker = FakeWorker(run)
    client = TestClient(
        create_app(worker=worker, store=store, admin_token="admin-secret")
    )

    # Act
    unauthorized = client.post("/issues", json={"issue_url": task.url})
    result = client.post(
        "/issues",
        headers={"Authorization": "Bearer admin-secret"},
        json={"issue_url": task.url},
    )

    # Assert
    assert unauthorized.status_code == 401
    assert result.status_code == 202
    assert result.json() == {"run_id": run.id, "status": "queued"}
    assert worker.submitted == [task.url]
