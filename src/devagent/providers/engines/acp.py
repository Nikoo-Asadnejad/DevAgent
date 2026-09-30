"""Codex ACP coding engine driven by OpenHands `ACPAgent`.

The ACP CLI authenticates with the user's own subscription credential, which is passed only as an OpenHands
conversation secret into the sandbox. Results are mapped to a neutral `EngineResult`; secrets are masked in any
text that leaves this module.
"""

import time
from collections.abc import Callable
from typing import Protocol

from pydantic import SecretStr

from devagent.core.config import CodexConfig
from devagent.core.guardrails import Redactor, secret_fragments
from devagent.core.models import EngineResult, EngineStatus, Limits
from devagent.providers.engines.base import Workspace


class BudgetExceeded(Exception):
    def __init__(self, cost_usd: float) -> None:
        super().__init__(f"cost ${cost_usd:.2f} exceeded the per-run cap")
        self.cost_usd = cost_usd


def wait_with_limits(
    poll: Callable[[], tuple[bool, float | None]],
    stop: Callable[[], None],
    *,
    timeout: float,
    max_usd: float | None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 5.0,
) -> None:
    """Poll a running agent until it finishes; stop it and raise as soon as the deadline or cost cap is passed.

    `poll` returns (finished, cost_so_far). Raises TimeoutError or BudgetExceeded after calling `stop`.
    """
    deadline = clock() + timeout
    while True:
        finished, cost = poll()
        if max_usd is not None and cost is not None and cost > max_usd:
            stop()
            raise BudgetExceeded(cost)
        if finished:
            return
        if clock() >= deadline:
            stop()
            raise TimeoutError(f"agent did not finish within {timeout:.0f}s")
        sleep(interval)


class ConversationLike(Protocol):
    def send_message(self, message: str) -> None: ...

    def run(self, timeout: float, max_usd: float | None) -> None:
        """Wait for the agent; raise TimeoutError / BudgetExceeded (after stopping it) when a limit is passed."""
        ...

    def final_response(self) -> str: ...

    def cost_usd(self) -> float | None: ...

    def status(self) -> str: ...

    def close(self) -> None: ...


ConversationFactory = Callable[
    [CodexConfig, Workspace, dict[str, str]], ConversationLike
]


class _OpenHandsConversation:
    """Adapter from the OpenHands SDK conversation to `ConversationLike`."""

    def __init__(
        self, settings: CodexConfig, workspace: Workspace, secrets: dict[str, str]
    ) -> None:
        from openhands.sdk import Conversation, RemoteWorkspace
        from openhands.sdk.agent import ACPAgent

        if not isinstance(workspace, RemoteWorkspace):
            raise TypeError(
                "CodexEngine needs an OpenHands RemoteWorkspace (the sandbox container)"
            )
        agent = ACPAgent(acp_command=list(settings.command))
        self._conversation = Conversation(
            agent=agent, workspace=workspace, secrets=secrets, visualizer=None
        )

    def send_message(self, message: str) -> None:
        self._conversation.send_message(message)

    def run(self, timeout: float, max_usd: float | None) -> None:
        self._conversation.run(blocking=False)

        polls = 0

        def poll() -> tuple[bool, float | None]:
            nonlocal polls
            polls += 1
            state = self._conversation.state
            if (
                polls % 6 == 0
            ):  # ~30s: status normally arrives over the websocket; re-read in case it dropped
                refresh = getattr(state, "refresh_from_server", None)
                if callable(refresh):
                    try:
                        refresh()
                    except Exception:  # a failed refresh is retried next time; the deadline still applies
                        pass
            return state.execution_status.is_terminal(), self.cost_usd()

        wait_with_limits(
            poll, self._conversation.pause, timeout=timeout, max_usd=max_usd
        )

    def final_response(self) -> str:
        from openhands.sdk.conversation.response_utils import get_agent_final_response

        return get_agent_final_response(self._conversation.state.events)

    def cost_usd(self) -> float | None:
        metrics = self._conversation.conversation_stats.get_combined_metrics()
        cost = float(metrics.accumulated_cost)
        return cost if cost > 0 else None

    def status(self) -> str:
        return str(self._conversation.state.execution_status.value)

    def close(self) -> None:
        self._conversation.close()


_SUCCESS_STATUSES = frozenset({"finished", "idle"})


class CodexEngine:
    def __init__(
        self,
        settings: CodexConfig,
        credentials: dict[str, SecretStr],
        *,
        conversation_factory: ConversationFactory = _OpenHandsConversation,
    ) -> None:
        self.name = "codex"
        self._settings = settings
        self._credentials = credentials
        self._factory = conversation_factory
        raw = [c.get_secret_value() for c in credentials.values()]
        self._redactor = Redactor(
            [*raw, *(fragment for value in raw for fragment in secret_fragments(value))]
        )

    def run(self, workspace: Workspace, prompt: str, limits: Limits) -> EngineResult:
        secrets = {
            name: value.get_secret_value() for name, value in self._credentials.items()
        }
        secrets.update(self._settings.extra_env)
        redact = self._redactor.redact
        conversation = self._factory(self._settings, workspace, secrets)
        try:
            conversation.send_message(prompt)
            conversation.run(timeout=limits.max_minutes * 60, max_usd=limits.max_usd)
            status = conversation.status()
            summary = redact(conversation.final_response())
            cost = conversation.cost_usd()
        except BudgetExceeded as exc:
            return EngineResult(
                status=EngineStatus.BUDGET_EXCEEDED,
                summary=f"{self.name} stopped: {exc} (${limits.max_usd:.2f})",
                cost_usd=exc.cost_usd,
            )
        except TimeoutError:
            return EngineResult(
                status=EngineStatus.TIMED_OUT,
                summary=f"{self.name} did not finish within {limits.max_minutes} min",
            )
        except Exception as exc:  # the engine is a black box; any failure fails this run, not the service
            return EngineResult(
                status=EngineStatus.FAILED, summary=redact(f"{self.name} failed: {exc}")
            )
        finally:
            conversation.close()

        if limits.max_usd is not None and cost is not None and cost > limits.max_usd:
            return EngineResult(
                status=EngineStatus.BUDGET_EXCEEDED,
                summary=f"cost ${cost:.2f} exceeded the ${limits.max_usd:.2f} per-run cap",
                cost_usd=cost,
            )
        if status not in _SUCCESS_STATUSES:
            return EngineResult(
                status=EngineStatus.FAILED,
                summary=f"{self.name} ended with status '{status}': {summary}",
                cost_usd=cost,
            )
        return EngineResult(
            status=EngineStatus.SUCCEEDED, summary=summary, cost_usd=cost
        )
