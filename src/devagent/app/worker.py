"""Explicit GitHub issue intake and scheduling.

An issue becomes a run only once; retries are requested against a failed run. The implementation workflow itself
remains claim → prepare → implement → verify → publish.
"""

import logging
import queue
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from devagent.core.config import AgentConfig, RepoConfig
from devagent.db.store import RunRecord, RunStore
from devagent.core.guardrails import Redactor, check_daily_budget
from devagent.core.models import COMMENT_MARKER, RunStatus, Task
from devagent.providers.trackers.base import TaskTracker

log = logging.getLogger(__name__)

StartFn = Callable[[RunRecord], None]


class IssueAlreadySubmitted(Exception):
    pass


class IssueNotRunnable(Exception):
    pass


def _match_repo(task: Task, repos: Sequence[RepoConfig]) -> RepoConfig | None:
    for repo in repos:
        if all(
            task.scope.get(key, "").casefold() == repo.repo[key].casefold()
            for key in ("owner", "name")
        ):
            return repo
    return None


class Worker:
    def __init__(
        self,
        config: AgentConfig,
        tracker: TaskTracker,
        store: RunStore,
        *,
        start: StartFn,
        redactor: Redactor | None = None,
    ) -> None:
        self._cfg = config
        self._tracker = tracker
        self._store = store
        self._start = start
        self._redact = (redactor or Redactor([])).redact
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- intake ------------------------------------------------------------------------------------------------------

    def pending(self) -> int:
        return self._queue.qsize()

    def submit_issue(self, issue_url: str) -> RunRecord:
        """Validate an explicitly submitted GitHub issue and queue its implementation run."""
        self._check_budget()
        task = self._tracker.get_task_from_url(issue_url)
        if task.closed:
            raise IssueNotRunnable("the GitHub issue is closed")
        if self._store.has_run_for_task(task.id):
            raise IssueAlreadySubmitted(f"GitHub issue {task.id} already has a run")
        if any(
            comment.body.startswith(COMMENT_MARKER)
            for comment in self._tracker.get_comments(task.id)
        ):
            raise IssueAlreadySubmitted(
                f"GitHub issue {task.id} already has a devagent comment"
            )
        repo = _match_repo(task, self._cfg.repos)
        if repo is None:
            owner = task.scope.get("owner", "")
            name = task.scope.get("name", "")
            raise IssueNotRunnable(f"repository {owner}/{name} is not configured")
        run = self._store.create_run(
            task, engine="codex", repo_index=self._cfg.repos.index(repo)
        )
        if run is None:
            raise IssueAlreadySubmitted(
                f"GitHub issue {task.id} already has an active run"
            )
        self._queue.put(run.id)
        log.info(
            "run queued",
            extra={"run_id": run.id, "task_id": task.id, "engine": "codex"},
        )
        return run

    def request_retry(self, run_id: str) -> str:
        self._check_budget()
        run = self._store.retry(run_id)
        self._queue.put(run.id)
        return run.id

    def _check_budget(self) -> None:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        check_daily_budget(
            runs_today=self._store.runs_created_since(today),
            max_runs_per_day=self._cfg.budgets.max_runs_per_day,
        )

    # --- processing --------------------------------------------------------------------------------------------------

    def _guarded(self, run_id: str, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:  # a bug must fail this run, not the worker
            message = self._redact(f"internal error: {type(exc).__name__}: {exc}")
            log.exception("run crashed", extra={"run_id": run_id})
            self._store.set_status(run_id, RunStatus.FAILED, error=message)

    def _handle(self, run_id: str) -> None:
        run = self._store.get_run(run_id)
        if run is not None:
            self._guarded(run.id, lambda: self._start(run))

    def drain(self) -> None:
        """Process everything queued, synchronously (tests, one-shot runs)."""
        while True:
            try:
                run_id = self._queue.get_nowait()
            except queue.Empty:
                return
            self._handle(run_id)

    # --- service loop ------------------------------------------------------------------------------------------------

    def _consume(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            self._handle(run_id)

    def start(self) -> None:
        self._threads = [
            threading.Thread(
                target=self._consume, name=f"devagent-worker-{i}", daemon=True
            )
            for i in range(self._cfg.budgets.max_concurrent)
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout)
