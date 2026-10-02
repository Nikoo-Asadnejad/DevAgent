"""Claim a run by assigning its branch and commenting on the GitHub issue."""

from typing import Any

from devagent.core.guardrails import check_push_target
from devagent.core.models import RunStatus
from devagent.core.slug import branch_name
from devagent.workflow.nodes.shared import comment_once
from devagent.workflow.state import Deps, RunState


def claim(state: RunState, deps: Deps) -> dict[str, Any]:
    branch = state.branch or branch_name(
        deps.config.guardrails.branch_prefix,
        state.task.id,
        state.task.title,
        state.run_id,
    )
    check_push_target(branch, deps.config.guardrails)
    deps.store.set_status(state.run_id, RunStatus.RUNNING, branch=branch)
    comment_once(
        deps,
        state,
        "started",
        f"started — engine `{state.engine}`, branch `{branch}`",
    )
    return {"branch": branch}
