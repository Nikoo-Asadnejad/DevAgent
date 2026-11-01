"""Record a failed workflow run without writing to the source issue."""

from typing import Any

from devagent.core.models import RunStatus
from devagent.workflow.state import Deps, RunState


def fail(state: RunState, deps: Deps) -> dict[str, Any]:
    error = deps.redactor.redact(state.error or "unknown error")
    deps.store.set_status(state.run_id, RunStatus.FAILED, error=error)
    return {}
