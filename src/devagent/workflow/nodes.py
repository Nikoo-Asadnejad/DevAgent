"""Graph nodes (spec 1.2). Each node does one step; the orchestrator performs every external write.

Nodes return a partial state update. Raising `StepFailed` (or any other exception) routes the run to `fail`.
Side-effecting nodes are idempotent so a repeated call never duplicates a comment, push, or PR.
"""

from pathlib import Path
from typing import Any

from devagent.core.config import RepoConfig
from devagent.core.guardrails import (
    check_push_target,
    disallowed_entries,
    ensure_new_branch,
    protected_path_violations,
    push_refspec,
)
from devagent.core.models import (
    COMMENT_MARKER,
    EngineStatus,
    Limits,
    PrSpec,
    RepoRef,
    RunStatus,
    VerifyResult,
)
from devagent.prompts import build_prompt
from devagent.core.slug import branch_name
from devagent.runtime.sandbox import SandboxSession
from devagent.workflow.state import Deps, RunState

_OUTPUT_TAIL = 1500


class StepFailed(Exception):
    """A step failed for a reason worth reporting on the task (not a bug in the service)."""


def _tail(text: str, limit: int = _OUTPUT_TAIL) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def _repo(deps: Deps, state: RunState) -> tuple[RepoConfig, RepoRef]:
    cfg = deps.config.repos[state.repo_index]
    return cfg, RepoRef(host="github", coordinates=cfg.repo)


def _repo_dir(deps: Deps, state: RunState) -> Path:
    return Path(deps.config.sandbox.workspaces_dir) / state.run_id / "repo"


def _git_env(deps: Deps, state: RunState) -> dict[str, str]:
    _, ref = _repo(deps, state)
    return {k: v.get_secret_value() for k, v in deps.code_host.push_auth(ref).items()}


def comment_once(deps: Deps, state: RunState, key: str, text: str) -> None:
    """Post a task comment at most once per run, tracked in run events."""
    if any(
        e.node == "comment" and e.detail.get("key") == key
        for e in deps.store.events(state.run_id)
    ):
        return
    deps.tracker.add_comment(
        state.task.id, f"{COMMENT_MARKER}{state.run_id}] {deps.redactor.redact(text)}"
    )
    deps.store.add_event(state.run_id, "comment", "posted", {"key": key})


def _run_checked(session: SandboxSession, deps: Deps, label: str, command: str) -> str:
    timeout = deps.config.sandbox.command_timeout_minutes * 60
    result = session.execute(command, timeout_seconds=timeout)
    if result.timed_out:
        raise StepFailed(
            f"{label} (`{command}`) timed out after {deps.config.sandbox.command_timeout_minutes} min"
        )
    if result.exit_code != 0:
        raise StepFailed(
            f"{label} (`{command}`) failed with exit code {result.exit_code}:\n{_tail(result.output)}"
        )
    return result.output


# --- nodes -----------------------------------------------------------------------------------------------------------


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
        deps, state, "started", f"started — engine `{state.engine}`, branch `{branch}`"
    )
    return {"branch": branch}


def prepare(state: RunState, deps: Deps) -> dict[str, Any]:
    cfg, ref = _repo(deps, state)
    repo_dir = _repo_dir(deps, state)
    if not (
        repo_dir / ".git"
    ).exists():  # avoid recloning if this node is invoked twice
        url = deps.code_host.clone_url(ref)
        deps.git.clone(url, repo_dir, base=cfg.base, env=_git_env(deps, state))
        assert state.branch is not None
        deps.git.create_branch(repo_dir, state.branch)
    return {"repo_dir": str(repo_dir)}


def implement(state: RunState, deps: Deps) -> dict[str, Any]:
    cfg, _ = _repo(deps, state)
    budgets = deps.config.budgets
    engine = deps.engine
    prompt = build_prompt(
        state.task, deps.tracker.get_comments(state.task.id), deps.rules
    )
    with deps.sandbox.session(state.run_id, _repo_dir(deps, state)) as session:
        if cfg.commands.bootstrap:
            _run_checked(
                session,
                deps,
                "bootstrap on the untouched base branch",
                cfg.commands.bootstrap,
            )
        result = engine.run(
            session.workspace,
            prompt,
            Limits(
                max_minutes=budgets.max_minutes_per_run, max_usd=budgets.max_usd_per_run
            ),
        )
    deps.store.set_status(state.run_id, RunStatus.RUNNING, cost_usd=result.cost_usd)
    if result.status is not EngineStatus.SUCCEEDED:
        raise StepFailed(
            f"engine {state.engine} {result.status.value}: {result.summary}"
        )
    return {"engine_result": result}


def verify(state: RunState, deps: Deps) -> dict[str, Any]:
    cfg, _ = _repo(deps, state)
    repo_dir = _repo_dir(deps, state)
    message = f"{state.task.title}\n\nTask: {state.task.url}\nRun: {state.run_id}\nEngine: {state.engine}"
    deps.git.commit_all(repo_dir, message)
    changed = deps.git.changed_files(repo_dir, base=cfg.base)
    if not changed:
        raise StepFailed("the engine finished but made no changes")
    violations = protected_path_violations(
        changed, deps.config.guardrails.protected_paths
    )
    violations += disallowed_entries(deps.git.changed_entries(repo_dir, base=cfg.base))
    if violations:
        raise StepFailed("changes touch protected paths: " + ", ".join(violations))
    diff = deps.git.diff(repo_dir, base=cfg.base)
    if deps.redactor.contains_secret(diff):
        raise StepFailed(
            "the diff contains one of the service's configured credentials"
        )
    findings = deps.scanner.scan(diff)
    if findings:
        raise StepFailed("possible secrets in the diff: " + "; ".join(findings))
    with deps.sandbox.session(state.run_id, repo_dir) as session:
        build = _run_checked(session, deps, "build", cfg.commands.build)
        test = _run_checked(session, deps, "test", cfg.commands.test)
    output = f"$ {cfg.commands.build}\n{_tail(build)}\n\n$ {cfg.commands.test}\n{_tail(test)}"
    return {
        "verify": VerifyResult(passed=True, output=output, changed_files=tuple(changed))
    }


def _pr_body(state: RunState, deps: Deps) -> str:
    result, checks = state.engine_result, state.verify
    files = "\n".join(f"- `{f}`" for f in (checks.changed_files if checks else ()))
    cost = (
        f"${result.cost_usd:.2f}"
        if result and result.cost_usd is not None
        else "unknown"
    )
    body = (
        f"Task: [{state.task.title}]({state.task.url})\n\n"
        f"## Summary (from {state.engine})\n{result.summary if result else ''}\n\n"
        f"## Changed files\n{files}\n\n"
        f"## Verification (run by devagent, not the agent)\n```\n{checks.output if checks else ''}\n```\n\n"
        f"Engine: `{state.engine}` · cost: {cost} · run: `{state.run_id}`\n\n"
        "_Opened as a draft by devagent. Review before marking ready; devagent never merges._"
    )
    return deps.redactor.redact(body)


def publish(state: RunState, deps: Deps) -> dict[str, Any]:
    cfg, ref = _repo(deps, state)
    assert state.branch is not None
    repo_dir = _repo_dir(deps, state)
    host = deps.code_host
    url = host.clone_url(ref)
    env = _git_env(deps, state)
    refspec = push_refspec(state.branch, deps.config.guardrails)

    head = deps.git.head_sha(repo_dir)
    remote = deps.git.remote_branch_sha(url, state.branch, env=env)
    if remote is None:
        deps.git.push(repo_dir, url, refspec, env=env)
    else:
        # Our own earlier push is fine; anything else is someone else's branch.
        ensure_new_branch(state.branch, exists_on_remote=remote != head)

    pr = host.find_open_pr(ref, state.branch) or host.create_draft_pull_request(
        PrSpec(
            repo=ref,
            source_branch=state.branch,
            target_branch=cfg.base,
            title=f"[{state.task.id}] {state.task.title}",
            body=_pr_body(state, deps),
            task_id=state.task.id,
        )
    )
    comment_once(deps, state, "pr", f"opened draft PR: {pr.url}")
    deps.store.set_status(state.run_id, RunStatus.DONE, pr_url=pr.url)
    return {"pr": pr}


def fail(state: RunState, deps: Deps) -> dict[str, Any]:
    error = deps.redactor.redact(state.error or "unknown error")
    comment_once(
        deps, state, "failed", f"failed at `{state.failed_node}`:\n{_tail(error)}"
    )
    deps.store.set_status(state.run_id, RunStatus.FAILED, error=error)
    return {}
