"""Provider-neutral models shared by the core pipeline and all adapters."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


COMMENT_MARKER = "[devagent:"
"""Prefix of every task comment devagent posts; a task carrying one has already been handled."""


class Priority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"

    @property
    def rank(self) -> int:
        return _PRIORITY_RANK[self]


_PRIORITY_RANK = {
    Priority.URGENT: 1,
    Priority.HIGH: 2,
    Priority.NORMAL: 3,
    Priority.LOW: 4,
}


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Task(Frozen):
    id: str
    url: str
    title: str
    description: str = ""
    priority: Priority | None = None
    due: datetime | None = None
    created: datetime
    labels: tuple[str, ...] = ()
    status: str
    closed: bool = False
    """Closed on GitHub; never eligible for a run."""
    # GitHub location used to map the issue to a configured repo, e.g. {"owner": "acme", "name": "api"}.
    scope: dict[str, str] = Field(default_factory=dict[str, str])


class Comment(Frozen):
    id: str
    author_id: str
    body: str
    created: datetime


class RepoRef(Frozen):
    """GitHub repository coordinates, e.g. {"owner": "acme", "name": "booking-api"}."""

    host: str
    coordinates: dict[str, str]


class PrSpec(Frozen):
    repo: RepoRef
    source_branch: str
    target_branch: str
    title: str
    body: str
    task_id: str


class PrRef(Frozen):
    id: str
    url: str


class Limits(Frozen):
    max_minutes: int = Field(gt=0)
    max_usd: float | None = Field(default=None, gt=0)


class EngineStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXCEEDED = "budget_exceeded"


class EngineResult(Frozen):
    status: EngineStatus
    summary: str
    cost_usd: float | None = None


class VerifyResult(Frozen):
    passed: bool
    output: str = ""
    changed_files: tuple[str, ...] = ()
