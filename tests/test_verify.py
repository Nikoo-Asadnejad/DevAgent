from unittest.mock import Mock, call

import pytest

from devagent.core.guardrails import Redactor
from devagent.runtime.sandbox import CommandOutput
from devagent.workflow.nodes.shared import StepFailed
from devagent.workflow.nodes.verify import verify
from devagent.workflow.state import Deps, RunState
from test_worker_issue_submission import config, issue


@pytest.mark.parametrize("bootstrap", [None, "restore"])
def test_verification_restores_dependencies_in_its_own_sandbox(
    bootstrap: str | None,
) -> None:
    deps, session, state = verification_setup(bootstrap)

    result = verify(state, deps)["verify"]

    commands = ([bootstrap] if bootstrap else []) + ["make", "make test"]
    assert session.execute.call_args_list == [
        call(command, timeout_seconds=1800) for command in commands
    ]
    assert result.passed
    assert all(f"$ {command}\n" in result.output for command in commands)


def test_failed_verification_restore_prevents_build_and_test() -> None:
    deps, session, state = verification_setup("restore")
    session.execute.return_value = CommandOutput(
        exit_code=1, output="package download failed", timed_out=False
    )

    with pytest.raises(StepFailed, match="verification bootstrap.*failed"):
        verify(state, deps)

    session.execute.assert_called_once_with("restore", timeout_seconds=1800)


def verification_setup(bootstrap: str | None) -> tuple[Deps, Mock, RunState]:
    cfg = config()
    repo = cfg.repos[0]
    repo = repo.model_copy(
        update={"commands": repo.commands.model_copy(update={"bootstrap": bootstrap})}
    )
    cfg = cfg.model_copy(update={"repos": [repo]})
    deps = Mock(spec=Deps)
    deps.config = cfg
    deps.git = Mock()
    deps.git.changed_files.return_value = ["Program.cs"]
    deps.git.changed_entries.return_value = [("100644", "Program.cs")]
    deps.git.diff.return_value = "+new endpoint"
    deps.redactor = Redactor([])
    deps.scanner = Mock()
    deps.scanner.scan.return_value = []
    deps.sandbox = Mock()
    command_session = Mock()
    deps.sandbox.session.return_value.__enter__ = Mock(return_value=command_session)
    command_session.execute.return_value = CommandOutput(
        exit_code=0, output="passed", timed_out=False
    )
    deps.sandbox.session.return_value.__exit__ = Mock(return_value=False)
    state = RunState(run_id="verify-run", task=issue(), repo_index=0, engine="codex")
    return deps, command_session, state
