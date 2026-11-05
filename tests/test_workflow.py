from pathlib import Path
from unittest.mock import Mock, call

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from devagent.core.guardrails import Redactor
from devagent.core.models import EngineResult, EngineStatus, PrRef, RunStatus
from devagent.providers.code_hosts.base import CodeHost
from devagent.providers.engines.base import CodingEngine
from devagent.providers.trackers.base import TaskTracker
from devagent.runtime.gitops import GitOps
from devagent.runtime.sandbox import CommandOutput, Sandbox
from devagent.runtime.secretscan import SecretScanner
from devagent.workflow import Deps, build_graph, start_run
from test_worker_issue_submission import config, issue
from devagent.db.store import RunStore


@pytest.mark.parametrize(
    ("engine_succeeds", "prepare_fails"), [(True, False), (False, False), (True, True)]
)
def test_workflow_reads_issue_without_writing_comments(
    tmp_path: Path, engine_succeeds: bool, prepare_fails: bool
) -> None:
    cfg = config()
    cfg = cfg.model_copy(
        update={
            "sandbox": cfg.sandbox.model_copy(update={"workspaces_dir": str(tmp_path)})
        }
    )
    tracker = Mock(spec=TaskTracker)
    tracker.get_comments.return_value = []
    host = Mock(spec=CodeHost)
    host.clone_url.return_value = "https://github.com/acme/widgets.git"
    host.push_auth.return_value = {}
    host.find_open_pr.return_value = None
    host.create_draft_pull_request.return_value = PrRef(
        id="43", url="https://github.com/acme/widgets/pull/43"
    )
    engine = Mock(spec=CodingEngine)
    engine.run.return_value = EngineResult(
        status=EngineStatus.SUCCEEDED if engine_succeeds else EngineStatus.FAILED,
        summary="Implemented" if engine_succeeds else "error containing secret-token",
    )
    sandbox = Mock(spec=Sandbox)
    sandbox.prepare.return_value = "sha256:" + "a" * 64
    if prepare_fails:
        sandbox.prepare.side_effect = ValueError("unresolved toolchain")
    session = Mock()
    session.execute.return_value = CommandOutput(
        exit_code=0, output="checks passed", timed_out=False
    )
    sandbox.session.return_value.__enter__ = Mock(return_value=session)
    sandbox.session.return_value.__exit__ = Mock(return_value=False)
    git = Mock(spec=GitOps)
    git.changed_files.return_value = ["Program.cs"]
    git.changed_entries.return_value = [("100644", "Program.cs")]
    git.diff.return_value = "+weather endpoint"
    git.remote_branch_sha.return_value = None
    scanner = Mock(spec=SecretScanner)
    scanner.scan.return_value = []
    store = RunStore.in_memory()
    store.create_schema()
    try:
        task = issue()
        run = store.create_run(task, engine="codex", repo_index=0)
        assert run is not None
        deps = Deps(
            config=cfg,
            tracker=tracker,
            code_host=host,
            engine=engine,
            sandbox=sandbox,
            git=git,
            scanner=scanner,
            store=store,
            redactor=Redactor(["secret-token"]),
            rules="Implement the issue.",
        )

        start_run(build_graph(deps, InMemorySaver()), run)

        result = store.get_run(run.id)
        assert result is not None
        assert tracker.mock_calls == (
            [] if prepare_fails else [call.get_comments(task.id)]
        )
        assert not any(event.node == "comment" for event in store.events(run.id))
        if prepare_fails:
            assert result.status is RunStatus.FAILED
            assert "unresolved toolchain" in (result.error or "")
            engine.run.assert_not_called()
            sandbox.session.assert_not_called()
            git.push.assert_not_called()
            host.create_draft_pull_request.assert_not_called()
        elif engine_succeeds:
            sandbox.prepare.assert_called_once()
            assert sandbox.session.call_count == 2
            assert all(
                c.kwargs["image"] == sandbox.prepare.return_value
                for c in sandbox.session.call_args_list
            )
            assert result.status is RunStatus.DONE
            assert result.pr_url == "https://github.com/acme/widgets/pull/43"
            assert [
                event.node for event in store.events(run.id) if event.kind == "finish"
            ] == ["claim", "prepare", "implement", "verify", "publish"]
            git.push.assert_called_once()
            host.create_draft_pull_request.assert_called_once()
        else:
            assert result.status is RunStatus.FAILED
            assert result.error is not None
            assert "secret-token" not in result.error
            assert "internal error" not in result.error
            assert store.events(run.id)[-1].node == "implement"
            assert store.events(run.id)[-1].kind == "error"
            git.push.assert_not_called()
            host.create_draft_pull_request.assert_not_called()
    finally:
        store.close()
