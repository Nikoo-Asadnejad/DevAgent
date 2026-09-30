"""HTTP surface: explicit GitHub issue submission plus run administration."""

import hmac
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from devagent.db.store import RunEvent, RunRecord, RunStore
from devagent.core.guardrails import GuardrailViolation
from devagent.providers.trackers.errors import TrackerRequestError
from devagent.app.worker import IssueAlreadySubmitted, IssueNotRunnable


class WorkerLike(Protocol):
    def submit_issue(self, issue_url: str) -> RunRecord: ...

    def request_retry(self, run_id: str) -> str: ...


class IssueRequest(BaseModel):
    issue_url: str = Field(min_length=1)


class IssueAccepted(BaseModel):
    run_id: str
    status: str


class RunDetail(BaseModel):
    run: RunRecord
    events: list[RunEvent]


class RetryAccepted(BaseModel):
    run_id: str


def create_app(
    *,
    worker: WorkerLike,
    store: RunStore,
    admin_token: str | None,
) -> FastAPI:
    app = FastAPI(title="devagent", docs_url=None, redoc_url=None, openapi_url=None)

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
        if admin_token is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "admin API disabled: no admin token configured",
            )
        expected = f"Bearer {admin_token}"
        if authorization is None or not hmac.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "invalid or missing bearer token"
            )

    admin = [Depends(require_admin)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/issues", dependencies=admin, status_code=status.HTTP_202_ACCEPTED)
    def submit_issue(request: IssueRequest) -> IssueAccepted:
        try:
            run = worker.submit_issue(request.issue_url)
        except TrackerRequestError as exc:
            code = (
                status.HTTP_404_NOT_FOUND
                if exc.status_code == 404
                else status.HTTP_502_BAD_GATEWAY
            )
            raise HTTPException(code, str(exc)) from None
        except IssueAlreadySubmitted as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        except IssueNotRunnable as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
            ) from None
        except GuardrailViolation as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        return IssueAccepted(run_id=run.id, status=run.status.value)

    @app.get("/runs", dependencies=admin)
    def list_runs(limit: int = 50) -> list[RunRecord]:
        return store.list_runs(limit=min(max(limit, 1), 500))

    @app.get("/runs/{run_id}", dependencies=admin)
    def get_run(run_id: str) -> RunDetail:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return RunDetail(run=run, events=store.events(run_id))

    @app.post(
        "/runs/{run_id}/retry", dependencies=admin, status_code=status.HTTP_202_ACCEPTED
    )
    def retry(run_id: str) -> RetryAccepted:
        try:
            return RetryAccepted(run_id=worker.request_retry(run_id))
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND) from None
        except GuardrailViolation as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    return app
