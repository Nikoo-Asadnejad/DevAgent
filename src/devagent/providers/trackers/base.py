from typing import Protocol

from devagent.core.models import Comment, Task


class TaskTracker(Protocol):
    """Read-only GitHub issue and comment access."""

    def get_task_from_url(self, issue_url: str) -> Task: ...

    def get_task(self, task_id: str) -> Task: ...

    def get_comments(self, task_id: str) -> list[Comment]: ...
