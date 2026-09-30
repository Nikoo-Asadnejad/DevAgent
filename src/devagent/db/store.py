"""Run bookkeeping in process-local SQLite. The source GitHub issue is never modified."""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from devagent.db.tables import Base, RunEventRow, RunRow
from devagent.core.models import RunStatus, Task

_UPDATABLE = frozenset({"branch", "pr_url", "error", "cost_usd"})


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class RunRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    task_id: str
    task: Task
    repo_index: int
    engine: str
    status: RunStatus
    branch: str | None
    pr_url: str | None
    error: str | None
    cost_usd: float | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: RunRow) -> "RunRecord":
        return cls(
            id=row.id,
            task_id=row.task_id,
            task=Task.model_validate(row.task),
            repo_index=row.repo_index,
            engine=row.engine,
            status=RunStatus(row.status),
            branch=row.branch,
            pr_url=row.pr_url,
            error=row.error,
            cost_usd=row.cost_usd,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )


class RunEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    node: str
    kind: str
    detail: dict[str, Any]
    at: datetime


class RunStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def in_memory(cls) -> "RunStore":
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        return cls(engine)

    @property
    def engine(self) -> Engine:
        return self._engine

    def close(self) -> None:
        self._engine.dispose()

    def create_schema(self) -> None:
        """Create the in-memory tables for this process."""
        Base.metadata.create_all(self._engine)

    def _session(self) -> Session:
        return self._sessions()

    def create_run(
        self, task: Task, *, engine: str, repo_index: int
    ) -> RunRecord | None:
        """Insert a queued run, or return None if the task already has an active run."""
        row = RunRow(
            id=uuid.uuid4().hex[:12],
            task_id=task.id,
            task=task.model_dump(mode="json"),
            repo_index=repo_index,
            engine=engine,
            status=RunStatus.QUEUED.value,
        )
        with self._session() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return None
            return RunRecord.from_row(row)

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._session() as session:
            row = session.scalar(select(RunRow).where(RunRow.id == run_id))
            return RunRecord.from_row(row) if row else None

    def has_run_for_task(self, task_id: str) -> bool:
        with self._session() as session:
            return (
                session.scalar(
                    select(RunRow.pk).where(RunRow.task_id == task_id).limit(1)
                )
                is not None
            )

    def set_status(
        self, run_id: str, status: RunStatus, **fields: str | float | None
    ) -> None:
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        with self._session() as session:
            row = session.scalar(select(RunRow).where(RunRow.id == run_id))
            if row is None:
                raise KeyError(run_id)
            row.status = status.value
            for name, value in fields.items():
                setattr(row, name, value)
            session.commit()

    def add_event(
        self, run_id: str, node: str, kind: str, detail: dict[str, Any] | None = None
    ) -> None:
        with self._session() as session:
            session.add(
                RunEventRow(run_id=run_id, node=node, kind=kind, detail=detail or {})
            )
            session.commit()

    def events(self, run_id: str) -> list[RunEvent]:
        with self._session() as session:
            rows = session.scalars(
                select(RunEventRow)
                .where(RunEventRow.run_id == run_id)
                .order_by(RunEventRow.id)
            ).all()
            return [
                RunEvent(node=r.node, kind=r.kind, detail=r.detail, at=_aware(r.at))
                for r in rows
            ]

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._session() as session:
            rows = session.scalars(
                select(RunRow).order_by(RunRow.pk.desc()).limit(limit)
            ).all()
            return [RunRecord.from_row(r) for r in rows]

    def runs_created_since(self, since: datetime) -> int:
        with self._session() as session:
            count = session.scalar(
                select(func.count())
                .select_from(RunRow)
                .where(RunRow.created_at >= since)
            )
            return int(count or 0)

    def retry(self, run_id: str) -> RunRecord:
        """Queue a fresh run (new id, therefore a new branch) for the task of a failed run."""
        previous = self.get_run(run_id)
        if previous is None:
            raise KeyError(run_id)
        if previous.status is not RunStatus.FAILED:
            raise ValueError("only failed runs can be retried")
        run = self.create_run(
            previous.task, engine=previous.engine, repo_index=previous.repo_index
        )
        if run is None:
            raise ValueError(f"task {previous.task_id} already has an active run")
        return run
