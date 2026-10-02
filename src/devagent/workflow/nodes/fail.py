"""Record and report a failed workflow run."""

from typing import Any

from devagent.core.models import RunStatus
from devagent.workflow.nodes.shared import comment_once, tail_output
from devagent.workflow.state import Deps, RunState


def fail(state: RunState, deps: Deps) -> dict[str, Any]:
    error = deps.redactor.redact(state.error or "unknown error")
    comment_once(
        deps,
        state,
        "failed",
        f"failed at `{state.failed_node}`:\n{tail_output(error)}",
    )
    deps.store.set_status(state.run_id, RunStatus.FAILED, error=error)
    return {}
