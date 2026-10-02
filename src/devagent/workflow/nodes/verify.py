"""Validate the generated changes, then run the configured build and tests."""

from typing import Any

from devagent.core.guardrails import disallowed_entries, protected_path_violations
from devagent.core.models import VerifyResult
from devagent.workflow.nodes.shared import (
    StepFailed,
    get_repo,
    get_repo_dir,
    run_checked,
    tail_output,
)
from devagent.workflow.state import Deps, RunState


def verify(state: RunState, deps: Deps) -> dict[str, Any]:
    config, _ = get_repo(deps, state)
    repo_dir = get_repo_dir(deps, state)
    message = (
        f"{state.task.title}\n\n"
        f"Task: {state.task.url}\n"
        f"Run: {state.run_id}\n"
        f"Engine: {state.engine}"
    )
    deps.git.commit_all(repo_dir, message)
    changed = deps.git.changed_files(repo_dir, base=config.base)
    if not changed:
        raise StepFailed("the engine finished but made no changes")

    violations = protected_path_violations(
        changed,
        deps.config.guardrails.protected_paths,
    )
    violations += disallowed_entries(
        deps.git.changed_entries(repo_dir, base=config.base)
    )
    if violations:
        raise StepFailed("changes touch protected paths: " + ", ".join(violations))

    diff = deps.git.diff(repo_dir, base=config.base)
    if deps.redactor.contains_secret(diff):
        raise StepFailed(
            "the diff contains one of the service's configured credentials"
        )
    findings = deps.scanner.scan(diff)
    if findings:
        raise StepFailed("possible secrets in the diff: " + "; ".join(findings))

    with deps.sandbox.session(state.run_id, repo_dir) as session:
        build = run_checked(session, deps, "build", config.commands.build)
        test = run_checked(session, deps, "test", config.commands.test)
    output = (
        f"$ {config.commands.build}\n{tail_output(build)}\n\n"
        f"$ {config.commands.test}\n{tail_output(test)}"
    )
    return {
        "verify": VerifyResult(
            passed=True,
            output=output,
            changed_files=tuple(changed),
        )
    }
