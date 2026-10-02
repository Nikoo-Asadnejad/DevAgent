"""Helpers shared by multiple workflow nodes."""

from pathlib import Path

from devagent.core.config import RepoConfig
from devagent.core.models import COMMENT_MARKER, RepoRef
from devagent.runtime.sandbox import SandboxSession
from devagent.workflow.state import Deps, RunState

OUTPUT_TAIL = 1500


class StepFailed(Exception):
    """A workflow step failed for a reason worth reporting on the task."""


def tail_output(text: str, limit: int = OUTPUT_TAIL) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def get_repo(deps: Deps, state: RunState) -> tuple[RepoConfig, RepoRef]:
    config = deps.config.repos[state.repo_index]
    return config, RepoRef(host="github", coordinates=config.repo)


def get_repo_dir(deps: Deps, state: RunState) -> Path:
    return Path(deps.config.sandbox.workspaces_dir) / state.run_id / "repo"


def get_git_env(deps: Deps, state: RunState) -> dict[str, str]:
    _, repo = get_repo(deps, state)
    return {
        key: value.get_secret_value()
        for key, value in deps.code_host.push_auth(repo).items()
    }


def comment_once(deps: Deps, state: RunState, key: str, text: str) -> None:
    """Post a task comment at most once per run, tracked in run events."""
    if any(
        event.node == "comment" and event.detail.get("key") == key
        for event in deps.store.events(state.run_id)
    ):
        return
    deps.tracker.add_comment(
        state.task.id,
        f"{COMMENT_MARKER}{state.run_id}] {deps.redactor.redact(text)}",
    )
    deps.store.add_event(state.run_id, "comment", "posted", {"key": key})


def run_checked(
    session: SandboxSession,
    deps: Deps,
    label: str,
    command: str,
) -> str:
    timeout = deps.config.sandbox.command_timeout_minutes * 60
    result = session.execute(command, timeout_seconds=timeout)
    if result.timed_out:
        raise StepFailed(
            f"{label} (`{command}`) timed out after "
            f"{deps.config.sandbox.command_timeout_minutes} min"
        )
    if result.exit_code != 0:
        raise StepFailed(
            f"{label} (`{command}`) failed with exit code {result.exit_code}:\n"
            f"{tail_output(result.output)}"
        )
    return result.output
