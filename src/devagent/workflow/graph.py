# pyright: reportMissingTypeStubs=false
"""LangGraph wiring: claim → prepare → implement → verify → publish, any error → fail.

Checkpoints are held in memory for the lifetime of the process. Phase 1 has no automatic retries.
"""

import logging
from collections.abc import Callable
from typing import Any, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langchain_core.runnables import RunnableConfig

from devagent.db.store import RunRecord
from devagent.workflow import nodes
from devagent.workflow.state import Deps, RunState

log = logging.getLogger(__name__)

Graph = CompiledStateGraph[RunState, None, RunState, RunState]
NodeFn = Callable[[RunState, Deps], dict[str, Any]]

_PIPELINE: list[tuple[str, NodeFn]] = [
    ("claim", nodes.claim),
    ("prepare", nodes.prepare),
    ("implement", nodes.implement),
    ("verify", nodes.verify),
    ("publish", nodes.publish),
]


class _Node(Protocol):
    def __call__(self, state: RunState) -> dict[str, Any]: ...


def _wrap(name: str, fn: NodeFn, deps: Deps) -> _Node:
    def node(state: RunState) -> dict[str, Any]:
        deps.store.add_event(state.run_id, name, "start")
        log.info("node start", extra={"run_id": state.run_id, "node": name})
        try:
            update = fn(state, deps)
        except (
            Exception
        ) as exc:  # an ordinary failure of this run, reported on the task
            message = deps.redactor.redact(
                str(exc)
                if isinstance(exc, nodes.StepFailed)
                else f"{type(exc).__name__}: {exc}"
            )
            deps.store.add_event(state.run_id, name, "error", {"error": message[:2000]})
            log.warning(
                "node error",
                extra={"run_id": state.run_id, "node": name, "error": message[:500]},
            )
            return {"error": message, "failed_node": name}
        deps.store.add_event(state.run_id, name, "finish")
        log.info("node finish", extra={"run_id": state.run_id, "node": name})
        return update

    return node


def _route(next_node: str) -> Callable[[RunState], str]:
    def route(state: RunState) -> str:
        return "fail" if state.error else next_node

    return route


def build_graph(deps: Deps, checkpointer: BaseCheckpointSaver[Any]) -> Graph:
    def fail_node(state: RunState) -> dict[str, Any]:
        return nodes.fail(state, deps)

    builder = StateGraph(RunState)
    for name, fn in _PIPELINE:
        builder.add_node(name, _wrap(name, fn, deps))  # pyright: ignore[reportUnknownMemberType, reportArgumentType]
    builder.add_node("fail", fail_node)  # pyright: ignore[reportUnknownMemberType, reportArgumentType]

    builder.add_edge(START, _PIPELINE[0][0])
    names = [name for name, _ in _PIPELINE]
    for current, following in zip(names, [*names[1:], END], strict=True):
        builder.add_conditional_edges(current, _route(following), [following, "fail"])
    builder.add_edge("fail", END)
    return builder.compile(checkpointer=checkpointer)  # pyright: ignore[reportUnknownMemberType]


def _config(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}}


def start_run(graph: Graph, run: RunRecord) -> None:
    initial = RunState(
        run_id=run.id, task=run.task, repo_index=run.repo_index, engine=run.engine
    )
    graph.invoke(initial, _config(run.id))  # pyright: ignore[reportUnknownMemberType]
