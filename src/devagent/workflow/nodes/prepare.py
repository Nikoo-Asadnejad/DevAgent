"""Clone the configured repository and create the run's local branch."""

from typing import Any

from devagent.workflow.nodes.shared import get_git_env, get_repo, get_repo_dir
from devagent.workflow.state import Deps, RunState


def prepare(state: RunState, deps: Deps) -> dict[str, Any]:
    config, repo = get_repo(deps, state)
    repo_dir = get_repo_dir(deps, state)
    if not (repo_dir / ".git").exists():
        url = deps.code_host.clone_url(repo)
        deps.git.clone(
            url,
            repo_dir,
            base=config.base,
            env=get_git_env(deps, state),
        )
        assert state.branch is not None
        deps.git.create_branch(repo_dir, state.branch)
    return {"repo_dir": str(repo_dir)}
