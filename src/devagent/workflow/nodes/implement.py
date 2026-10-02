"""Run the configured bootstrap and let Codex implement the GitHub issue."""

from typing import Any

from devagent.core.models import EngineStatus, Limits, RunStatus
from devagent.prompts import build_prompt
from devagent.workflow.nodes.shared import (
    StepFailed,
    get_repo,
    get_repo_dir,
    run_checked,
)
from devagent.workflow.state import Deps, RunState


def implement(state: RunState, deps: Deps) -> dict[str, Any]:
    config, _ = get_repo(deps, state)
    budgets = deps.config.budgets
    prompt = build_prompt(
        state.task,
        deps.tracker.get_comments(state.task.id),
        deps.rules,
    )
    with deps.sandbox.session(state.run_id, get_repo_dir(deps, state)) as session:
        if config.commands.bootstrap:
            run_checked(
                session,
                deps,
                "bootstrap on the untouched base branch",
                config.commands.bootstrap,
            )
        result = deps.engine.run(
            session.workspace,
            prompt,
            Limits(
                max_minutes=budgets.max_minutes_per_run,
                max_usd=budgets.max_usd_per_run,
            ),
        )
    deps.store.set_status(
        state.run_id,
        RunStatus.RUNNING,
        cost_usd=result.cost_usd,
    )
    if result.status is not EngineStatus.SUCCEEDED:
        raise StepFailed(
            f"engine {state.engine} {result.status.value}: {result.summary}"
        )
    return {"engine_result": result}
