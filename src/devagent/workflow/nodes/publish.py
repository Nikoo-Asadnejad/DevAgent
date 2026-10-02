"""Push the verified branch and open or reuse its draft pull request."""

from typing import Any

from devagent.core.guardrails import ensure_new_branch, push_refspec
from devagent.core.models import PrSpec, RunStatus
from devagent.workflow.nodes.shared import (
    comment_once,
    get_git_env,
    get_repo,
    get_repo_dir,
)
from devagent.workflow.state import Deps, RunState


def _pr_body(state: RunState, deps: Deps) -> str:
    result = state.engine_result
    checks = state.verify
    files = "\n".join(
        f"- `{file}`" for file in (checks.changed_files if checks else ())
    )
    cost = (
        f"${result.cost_usd:.2f}"
        if result and result.cost_usd is not None
        else "unknown"
    )
    body = (
        f"Task: [{state.task.title}]({state.task.url})\n\n"
        f"## Summary (from {state.engine})\n"
        f"{result.summary if result else ''}\n\n"
        f"## Changed files\n{files}\n\n"
        "## Verification (run by devagent, not the agent)\n"
        f"```\n{checks.output if checks else ''}\n```\n\n"
        f"Engine: `{state.engine}` · cost: {cost} · run: `{state.run_id}`\n\n"
        "_Opened as a draft by devagent. Review before marking ready; "
        "devagent never merges._"
    )
    return deps.redactor.redact(body)


def publish(state: RunState, deps: Deps) -> dict[str, Any]:
    config, repo = get_repo(deps, state)
    assert state.branch is not None
    repo_dir = get_repo_dir(deps, state)
    host = deps.code_host
    url = host.clone_url(repo)
    env = get_git_env(deps, state)
    refspec = push_refspec(state.branch, deps.config.guardrails)

    head = deps.git.head_sha(repo_dir)
    remote = deps.git.remote_branch_sha(url, state.branch, env=env)
    if remote is None:
        deps.git.push(repo_dir, url, refspec, env=env)
    else:
        ensure_new_branch(state.branch, exists_on_remote=remote != head)

    pr = host.find_open_pr(repo, state.branch) or host.create_draft_pull_request(
        PrSpec(
            repo=repo,
            source_branch=state.branch,
            target_branch=config.base,
            title=f"[{state.task.id}] {state.task.title}",
            body=_pr_body(state, deps),
            task_id=state.task.id,
        )
    )
    comment_once(deps, state, "pr", f"opened draft PR: {pr.url}")
    deps.store.set_status(state.run_id, RunStatus.DONE, pr_url=pr.url)
    return {"pr": pr}
