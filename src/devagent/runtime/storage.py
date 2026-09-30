"""Process-local run storage and LangGraph checkpoints. Everything is lost on restart."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from devagent.db.store import RunStore


@dataclass(frozen=True)
class Storage:
    store: RunStore
    checkpointer: Any  # langgraph BaseCheckpointSaver


@contextmanager
def open_storage() -> Generator[Storage]:
    from langgraph.checkpoint.memory import InMemorySaver

    store = RunStore.in_memory()
    store.create_schema()
    try:
        yield Storage(store=store, checkpointer=InMemorySaver())
    finally:
        store.close()
