"""Per-run sandbox: an OpenHands agent-server container with the run's repo mounted.

The repo working tree is mounted read-write so the agent can edit files, but its `.git` directory is mounted
read-only: the agent cannot rewrite remotes, config or hooks, and all commits/pushes are done by the orchestrator.
The GitHub credential is never passed into the container; the engine credential travels only as an
OpenHands conversation secret used by Codex (see `providers/engines/acp.py`).

The container is started by `HardenedDockerWorkspace` rather than stock `DockerWorkspace`, which publishes the
agent server on all interfaces and never sends its session key. Here the port is bound to 127.0.0.1, each container
gets its own random session key (passed via a 0600 env-file, not the command line) that the client sends on every
request, containers carry a run label so orphans from a crashed orchestrator can be removed, and container logs are
not streamed raw into the service logs.
"""

import os
import secrets
import subprocess
import tempfile
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from devagent.core.config import SandboxConfig
from devagent.providers.engines.base import Workspace

REPO_MOUNT = "/workspace/repo"
RUN_LABEL = "devagent.run"
INSTANCE_LABEL = "devagent.instance"

Runner = Callable[..., subprocess.CompletedProcess[str]]


class CommandOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_code: int
    output: str
    timed_out: bool


class SandboxSession(Protocol):
    @property
    def workspace(self) -> Workspace: ...

    def execute(self, command: str, timeout_seconds: float) -> CommandOutput: ...


class Sandbox(Protocol):
    def session(
        self, run_id: str, repo_dir: Path
    ) -> AbstractContextManager[SandboxSession]: ...


def volume_mounts(repo_dir: Path, cfg: SandboxConfig) -> list[str]:
    root = Path(cfg.workspaces_dir)
    try:
        relative = repo_dir.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError(
            f"repo dir {repo_dir} is outside the configured workspaces_dir {root}"
        ) from None
    if cfg.host_workspaces_dir is not None:
        host_repo = str(PurePosixPath(cfg.host_workspaces_dir, *relative.parts))
    else:
        host_repo = repo_dir.resolve().as_posix()
    return [f"{host_repo}:{REPO_MOUNT}:rw", f"{host_repo}/.git:{REPO_MOUNT}/.git:ro"]


def docker_run_command(
    *,
    image: str,
    host_port: int,
    volumes: list[str],
    env_file: str,
    run_id: str,
    network: str | None,
    instance: str,
) -> list[str]:
    cmd = [
        "docker", "run", "-d", "--rm",
        "--name", f"devagent-{run_id}-{secrets.token_hex(4)}",
        "--label", f"{INSTANCE_LABEL}={instance}",
        "--label", f"{RUN_LABEL}={run_id}",
        "--security-opt", "no-new-privileges:true",
        "--ulimit", "nofile=65536:65536",
        "--env-file", env_file,
        "-p", f"127.0.0.1:{host_port}:8000",
    ]  # fmt: skip
    for volume in volumes:
        cmd += ["-v", volume]
    if network:
        cmd += ["--network", network]
    return [*cmd, image, "--host", "0.0.0.0", "--port", "8000"]


def reap_sandbox_containers(
    run_id: str | None, *, instance: str, runner: Runner = subprocess.run
) -> int:
    """Force-remove this instance's sandbox containers of `run_id` (all of them when None). Returns the count."""
    filters = ["--filter", f"label={INSTANCE_LABEL}={instance}"]
    if run_id:
        filters += ["--filter", f"label={RUN_LABEL}={run_id}"]
    try:
        listed = runner(
            ["docker", "ps", "-aq", *filters],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:  # no docker CLI (e.g. local dev without sandboxes)
        return 0
    ids = [line for line in listed.stdout.split() if line]
    if ids:
        runner(
            ["docker", "rm", "-f", *ids], capture_output=True, text=True, check=False
        )
    return len(ids)


class _CommandResultLike(Protocol):
    exit_code: int
    stdout: str
    stderr: str
    timeout_occurred: bool


class _WorkspaceLike(Protocol):
    working_dir: str

    def __enter__(self) -> Any: ...

    def __exit__(self, *exc: Any) -> Any: ...

    def execute_command(
        self, command: str, cwd: str | None = None, timeout: float = 30.0
    ) -> _CommandResultLike: ...


def _hardened_workspace(**kwargs: Any) -> _WorkspaceLike:
    from openhands.sdk import RemoteWorkspace
    from openhands.workspace import DockerWorkspace
    from openhands.workspace.docker.workspace import find_available_tcp_port

    class HardenedDockerWorkspace(DockerWorkspace):
        run_label: str = ""
        instance_label: str = "default"

        def _start_container(self, image: str, context: Any) -> None:
            self._image_name = image
            port = (
                find_available_tcp_port()
                if self.host_port is None
                else int(self.host_port)
            )
            self.host_port = port
            key = secrets.token_urlsafe(32)
            with tempfile.TemporaryDirectory(prefix="devagent-sbx-") as tmp:
                env_file = Path(tmp) / "env"
                env_file.write_text(f"SESSION_API_KEY={key}\n", encoding="utf-8")
                os.chmod(env_file, 0o600)
                command = docker_run_command(
                    image=image,
                    host_port=port,
                    volumes=list(self.volumes),
                    env_file=str(env_file),
                    run_id=self.run_label,
                    network=self.network,
                    instance=self.instance_label,
                )
                proc = subprocess.run(
                    command, capture_output=True, text=True, check=False, timeout=300
                )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"failed to start sandbox container: {proc.stderr.strip()[-500:]}"
                )
            self._container_id = proc.stdout.strip()
            object.__setattr__(self, "host", f"http://127.0.0.1:{port}")
            object.__setattr__(self, "api_key", key)
            self._wait_for_health(timeout=self.health_check_timeout)
            RemoteWorkspace.model_post_init(self, context)

    return HardenedDockerWorkspace(**kwargs)  # pyright: ignore[reportReturnType]


class _OpenHandsSession:
    def __init__(self, ws: _WorkspaceLike) -> None:
        self._ws = ws

    @property
    def workspace(self) -> Workspace:
        return self._ws

    def execute(self, command: str, timeout_seconds: float) -> CommandOutput:
        result = self._ws.execute_command(
            command, cwd=REPO_MOUNT, timeout=timeout_seconds
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        return CommandOutput(
            exit_code=result.exit_code, output=output, timed_out=result.timeout_occurred
        )


class OpenHandsSandbox:
    def __init__(
        self,
        cfg: SandboxConfig,
        *,
        workspace_factory: Callable[..., _WorkspaceLike] = _hardened_workspace,
        reaper: Callable[[str], int] | None = None,
    ) -> None:
        self._cfg = cfg
        self._factory = workspace_factory

        def default_reaper(run_id: str) -> int:
            return reap_sandbox_containers(run_id, instance=cfg.instance)

        self._reap: Callable[[str], int] = reaper or default_reaper

    @contextmanager
    def session(self, run_id: str, repo_dir: Path) -> Generator[SandboxSession]:
        # A crashed orchestrator can leave this run's previous container running on the same tree.
        self._reap(run_id)
        ws = self._factory(
            server_image=self._cfg.image,
            volumes=volume_mounts(repo_dir, self._cfg),
            working_dir=REPO_MOUNT,
            network=self._cfg.network,
            forward_env=[],
            detach_logs=False,
            run_label=run_id,
            instance_label=self._cfg.instance,
        )
        with ws:
            yield _OpenHandsSession(ws)
