from typing import Protocol

from devagent.core.models import Comment, Task


class TaskTracker(Protocol):
    """GitHub issue access. There is deliberately no way to edit or close an issue."""

    def get_task_from_url(self, issue_url: str) -> Task: ...

    def get_task(self, task_id: str) -> Task: ...

    def get_comments(self, task_id: str) -> list[Comment]: ...

    def add_comment(self, task_id: str, text: str) -> None: ...
