from typing import Protocol

from devagent.core.models import EngineResult, Limits


class Workspace(Protocol):
    @property
    def working_dir(self) -> str: ...


class CodingEngine(Protocol):
    def run(
        self, workspace: Workspace, prompt: str, limits: Limits
    ) -> EngineResult: ...
