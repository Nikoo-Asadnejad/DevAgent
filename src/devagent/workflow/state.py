from dataclasses import dataclass

from pydantic import BaseModel

from devagent.core.config import AgentConfig
from devagent.db.store import RunStore
from devagent.core.guardrails import Redactor
from devagent.core.models import EngineResult, PrRef, Task, VerifyResult
from devagent.providers.code_hosts.base import CodeHost
from devagent.providers.engines.base import CodingEngine
from devagent.providers.trackers.base import TaskTracker
from devagent.runtime.gitops import GitOps
from devagent.runtime.sandbox import Sandbox
from devagent.runtime.secretscan import SecretScanner


class RunState(BaseModel):
    """LangGraph state for one run; validated by Pydantic on every node boundary and checkpointed."""

    run_id: str
    task: Task
    repo_index: int
    engine: str
    branch: str | None = None
    repo_dir: str | None = None
    engine_result: EngineResult | None = None
    verify: VerifyResult | None = None
    pr: PrRef | None = None
    error: str | None = None
    failed_node: str | None = None


@dataclass(frozen=True)
class Deps:
    config: AgentConfig
    tracker: TaskTracker
    code_host: CodeHost
    engine: CodingEngine
    sandbox: Sandbox
    git: GitOps
    scanner: SecretScanner
    store: RunStore
    redactor: Redactor
    rules: str
