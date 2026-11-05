import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock

import httpx
import pytest

from devagent.core.config import SandboxConfig
from devagent.core.dotnet import DotnetRequirements, SdkPolicy, ToolchainError
from devagent.runtime.sandbox import OpenHandsSandbox
from devagent.runtime.sandbox_images import (
    DotnetToolchain,
    SandboxImageBuilder,
    resolve_dotnet,
)

BASE = "sha256:" + "a" * 64
BUILT = "sha256:" + "b" * 64


def test_custom_image_opt_out_and_session_pins_selected_image(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "global.json").write_text("not valid JSON")
    builder = Mock()
    builder.prepare.return_value = BUILT
    factory = Mock(return_value=MagicMock())
    sandbox = OpenHandsSandbox(
        SandboxConfig(
            image="custom:latest", detect_dotnet=False, workspaces_dir=str(tmp_path)
        ),
        image_builder=builder,
        workspace_factory=factory,
        reaper=lambda _: 0,
    )
    image = sandbox.prepare(repo)
    builder.prepare.assert_called_once_with("custom:latest", None)
    with sandbox.session("run", repo, image=image):
        pass
    assert factory.call_args.kwargs["server_image"] == BUILT


def test_metadata_resolution_keeps_pinned_sdk_and_adds_older_target_runtime() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("releases-index.json"):
            return httpx.Response(
                200,
                json={
                    "releases-index": [
                        {"channel-version": "8.0"},
                        {"channel-version": "10.0"},
                    ]
                },
            )
        channel = request.url.path.split("/")[-2]
        sdk, runtime = (
            ("8.0.403", "8.0.11") if channel == "8.0" else ("10.0.101", "10.0.1")
        )
        return httpx.Response(
            200,
            json={
                "releases": [
                    {
                        "sdks": [{"version": sdk}],
                        "aspnetcore-runtime": {"version": runtime},
                    }
                ]
            },
        )

    requirements = DotnetRequirements(
        SdkPolicy("10.0.100", "latestPatch"), ("8.0", "10.0")
    )
    resolved = resolve_dotnet(requirements, transport=httpx.MockTransport(handle))
    assert resolved == DotnetToolchain("10.0.101", ("8.0.11",))


def test_fallback_selects_sdk_for_highest_target_framework() -> None:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("releases-index.json"):
            return httpx.Response(
                200,
                json={
                    "releases-index": [
                        {"channel-version": "8.0"},
                        {"channel-version": "10.0"},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "releases": [
                    {
                        "sdk": {"version": "8.0.403"},
                        "aspnetcore-runtime": {"version": "8.0.11"},
                    }
                ]
            },
        )

    assert (
        resolve_dotnet(
            DotnetRequirements(SdkPolicy(), ("8.0",)),
            transport=httpx.MockTransport(handle),
        ).sdk_version
        == "8.0.403"
    )
    assert not any("/10.0/" in path for path in requests)


def test_cached_image_skips_metadata_and_build() -> None:
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, BASE),
            subprocess.CompletedProcess([], 0, BUILT),
        ]
    )
    resolver = Mock()
    image = SandboxImageBuilder(runner=runner, resolver=resolver).prepare(
        "base:latest", DotnetRequirements(SdkPolicy(), ("8.0",))
    )
    assert image == BUILT
    resolver.assert_not_called()
    assert all(
        call.args[0][1:3] == ["image", "inspect"] for call in runner.call_args_list
    )


def test_build_context_contains_only_trusted_recipe_and_cache_tracks_base_image() -> (
    None
):
    tags: list[str] = []
    recipe: list[str] = []
    base_ids = iter([BASE, "sha256:" + "c" * 64])
    current_base = BASE
    built = False

    def run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal current_base, built
        if args[-1] == "base:latest":
            current_base = next(base_ids)
            built = False
            return subprocess.CompletedProcess(args, 0, current_base)
        if args[1] == "build":
            tags.append(args[3])
            context = Path(args[-1])
            assert [p.name for p in context.iterdir()] == ["Dockerfile"]
            recipe.append((context / "Dockerfile").read_text())
            built = True
            return subprocess.CompletedProcess(args, 0, "built", "")
        if args[1] == "tag":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(
            args, 0 if built else 1, BUILT if built else "", ""
        )

    builder = SandboxImageBuilder(
        runner=run, resolver=lambda _: DotnetToolchain("10.0.100", ("8.0.11",))
    )
    requirements = DotnetRequirements(SdkPolicy("10.0.100", "disable"), ("8.0",))
    assert builder.prepare("base:latest", requirements) == BUILT
    assert builder.prepare("base:latest", requirements) == BUILT
    assert tags[0] != tags[1]
    assert f"FROM devagent-toolchain-base:{BASE[7:]}" in recipe[0]
    assert "--version 10.0.100" in recipe[0]
    assert "--runtime aspnetcore --version 8.0.11" in recipe[0]
    assert "/opt/devagent-dotnet" in recipe[0]


def test_failed_build_does_not_return_an_image() -> None:
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, BASE),
            subprocess.CompletedProcess([], 1, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "download failed"),
        ]
    )
    builder = SandboxImageBuilder(
        runner=runner, resolver=lambda _: DotnetToolchain("8.0.100", ())
    )
    with pytest.raises(ToolchainError, match="download failed"):
        builder.prepare("base:latest", DotnetRequirements(SdkPolicy(), ("8.0",)))
