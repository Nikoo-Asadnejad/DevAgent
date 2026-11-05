"""Build and cache trusted toolchain layers on the common OpenHands/ACP image."""

import hashlib
import json
import logging
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from devagent.core.dotnet import (
    DotnetRequirements,
    SdkVersion,
    ToolchainError,
    select_sdk,
)

log = logging.getLogger(__name__)
_METADATA = "https://builds.dotnet.microsoft.com/dotnet/release-metadata"
_BUILD_LOCK = threading.Lock()
_RECIPE_VERSION = 1
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class DotnetToolchain:
    sdk_version: str
    runtime_versions: tuple[str, ...]


def resolve_dotnet(
    requirements: DotnetRequirements, *, transport: httpx.BaseTransport | None = None
) -> DotnetToolchain:
    """Resolve SDKs/runtimes only from Microsoft's release metadata, never repository URLs."""
    try:
        with httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=2), timeout=30
        ) as client:

            def get(path: str) -> dict[str, Any]:
                response = client.get(f"{_METADATA}/{path}")
                response.raise_for_status()
                return response.json()

            index = get("releases-index.json")
            channels: list[str] = [
                entry["channel-version"]
                for entry in index["releases-index"]
                if isinstance(entry.get("channel-version"), str)
                and _valid_channel(entry["channel-version"])
            ]
            policy = requirements.sdk
            if policy.version:
                requested = SdkVersion(policy.version)
                mode = policy.roll_forward.removeprefix("latest").lower()
                if mode in {"disable", "patch", "feature"}:
                    sdk_channels = [requested.channel]
                elif mode == "minor":
                    sdk_channels = [
                        c
                        for c in channels
                        if int(c.split(".")[0]) == requested.numbers[0]
                    ]
                else:
                    sdk_channels = [
                        c for c in channels if _channel_key(c) >= requested.numbers[:2]
                    ]
            elif (
                requirements.runtime_channels and not requirements.sdk_from_global_json
            ):
                sdk_channels = [max(requirements.runtime_channels, key=_channel_key)]
            else:
                sdk_channels = channels

            releases = {
                c: get(f"{c}/releases.json")["releases"]
                for c in set(sdk_channels) | set(requirements.runtime_channels)
            }
            versions: list[str] = []
            for channel in sdk_channels:
                for release in releases[channel]:
                    sdks = release.get("sdks", []) + (
                        [release["sdk"]] if release.get("sdk") else []
                    )
                    versions.extend(sdk["version"] for sdk in sdks)
            sdk_version = select_sdk(versions, policy)
            selected = SdkVersion(sdk_version)
            if requirements.runtime_channels and selected.numbers[:2] < max(
                map(_channel_key, requirements.runtime_channels)
            ):
                raise ToolchainError(
                    f"SDK {sdk_version} cannot build the detected target frameworks {requirements.runtime_channels}"
                )
            runtimes: list[str] = []
            for channel in requirements.runtime_channels:
                if channel == selected.channel:
                    continue  # The SDK already includes its matching .NET and ASP.NET Core runtimes.
                candidates = [
                    r["aspnetcore-runtime"]["version"]
                    for r in releases[channel]
                    if r.get("aspnetcore-runtime")
                    and "-" not in r["aspnetcore-runtime"]["version"]
                ]
                if not candidates:
                    raise ToolchainError(
                        f"no stable ASP.NET Core runtime is available for {channel}"
                    )
                runtimes.append(max(candidates, key=lambda v: SdkVersion(v).key))
            return DotnetToolchain(sdk_version, tuple(runtimes))
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise ToolchainError(
            f"could not resolve .NET release metadata: {exc}"
        ) from None


def _channel_key(channel: str) -> tuple[int, int]:
    major, minor = channel.split(".")
    return int(major), int(minor)


def _valid_channel(channel: str) -> bool:
    parts = channel.split(".")
    return len(parts) == 2 and all(p.isascii() and p.isdigit() for p in parts)


def toolchain_dockerfile(base_ref: str, toolchain: DotnetToolchain) -> str:
    # Versions come from validated data, and no repository files/scripts enter the build context.
    versions = [toolchain.sdk_version, *toolchain.runtime_versions]
    for version in versions:
        SdkVersion(version)
    installs = [
        f"bash /tmp/dotnet-install.sh --version {toolchain.sdk_version} --install-dir /opt/devagent-dotnet"
    ]
    installs += [
        f"bash /tmp/dotnet-install.sh --runtime aspnetcore --version {version} --install-dir /opt/devagent-dotnet --skip-non-versioned-files"
        for version in toolchain.runtime_versions
    ]
    return (
        f"FROM {base_ref}\nUSER root\n"
        "RUN curl --fail --silent --show-error --location --retry 3 --connect-timeout 30 "
        "https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh "
        "&& " + " && ".join(installs) + " && rm /tmp/dotnet-install.sh\n"
        'ENV DOTNET_ROOT=/opt/devagent-dotnet PATH="/opt/devagent-dotnet:${PATH}"\n'
        "RUN dotnet --info && dotnet --list-runtimes\n"
    )


class SandboxImageBuilder:
    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        resolver: Callable[[DotnetRequirements], DotnetToolchain] = resolve_dotnet,
    ) -> None:
        self._run = runner
        self._resolve = resolver

    def _image_id(self, image: str) -> str | None:
        result = self._run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            return None
        image_id = result.stdout.strip()
        if (
            not image_id.startswith("sha256:")
            or len(image_id) != 71
            or any(c not in "0123456789abcdef" for c in image_id[7:])
        ):
            raise ToolchainError("Docker returned an invalid image ID")
        return image_id

    def prepare(self, base: str, requirements: DotnetRequirements | None) -> str:
        with _BUILD_LOCK:
            base_id = self._image_id(base)
            if base_id is None:
                raise ToolchainError(
                    f"base sandbox image {base!r} is unavailable; build or pull it first"
                )
            if requirements is None:
                return base_id
            key = hashlib.sha256(
                json.dumps(
                    {
                        "base": base_id,
                        "requirements": asdict(requirements),
                        "recipe": _RECIPE_VERSION,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()[:24]
            tag = f"devagent-toolchain:{key}"
            cached = self._image_id(tag)
            if cached:
                log.info("using cached .NET sandbox", extra={"image": tag})
                return cached
            toolchain = self._resolve(requirements)
            log.info(
                "building .NET sandbox",
                extra={"image": tag, "sdk": toolchain.sdk_version},
            )
            # Dockerfile FROM needs a named image reference, not a config digest (sha256:...).
            base_ref = f"devagent-toolchain-base:{base_id[7:]}"
            tagged = self._run(
                ["docker", "tag", base_id, base_ref],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if tagged.returncode != 0:
                raise ToolchainError(
                    f"could not pin the base image: {tagged.stderr[-1000:]}"
                )
            with tempfile.TemporaryDirectory(prefix="devagent-image-") as tmp:
                Path(tmp, "Dockerfile").write_text(
                    toolchain_dockerfile(base_ref, toolchain), encoding="utf-8"
                )
                try:
                    result = self._run(
                        ["docker", "build", "--tag", tag, tmp],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=1800,
                    )
                except subprocess.TimeoutExpired:
                    raise ToolchainError(
                        "toolchain image build timed out after 30 minutes"
                    ) from None
            if result.returncode != 0:
                raise ToolchainError(
                    f"toolchain image build failed: {(result.stderr or result.stdout)[-2000:]}"
                )
            image_id = self._image_id(tag)
            if image_id is None:
                raise ToolchainError(
                    "toolchain image was built but cannot be inspected"
                )
            return image_id
