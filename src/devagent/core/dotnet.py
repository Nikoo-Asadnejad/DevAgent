"""Read .NET requirements without executing repository-controlled MSBuild code."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


class ToolchainError(Exception):
    pass


_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z0-9.-]+))?$")
_PROPERTY = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_.]*)\)")
_JSON_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/')
_TRAILING_COMMA = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])')
_POLICIES = {
    "disable",
    "patch",
    "feature",
    "minor",
    "major",
    "latestPatch",
    "latestFeature",
    "latestMinor",
    "latestMajor",
}


@dataclass(frozen=True)
class SdkVersion:
    text: str

    def __post_init__(self) -> None:
        if not _VERSION.fullmatch(self.text):
            raise ToolchainError(f"invalid .NET version: {self.text!r}")

    @property
    def numbers(self) -> tuple[int, int, int]:
        parts = self.text.split("-", 1)[0].split(".")
        return int(parts[0]), int(parts[1]), int(parts[2])

    @property
    def channel(self) -> str:
        major, minor, _ = self.numbers
        return f"{major}.{minor}"

    @property
    def key(self) -> tuple[int, int, int, bool, tuple[str, ...]]:
        _, _, suffix = self.text.partition("-")
        prerelease = tuple(p.zfill(20) if p.isdigit() else p for p in suffix.split("."))
        return (*self.numbers, not bool(suffix), prerelease)


@dataclass(frozen=True)
class SdkPolicy:
    version: str | None = None
    roll_forward: str = "latestMajor"
    allow_prerelease: bool = False

    def __post_init__(self) -> None:
        if self.version is not None:
            SdkVersion(self.version)
        if self.roll_forward not in _POLICIES:
            raise ToolchainError(
                f"unsupported SDK rollForward policy: {self.roll_forward!r}"
            )
        if self.version is None and self.roll_forward != "latestMajor":
            raise ToolchainError(
                "global.json needs sdk.version for this rollForward policy"
            )


@dataclass(frozen=True)
class DotnetRequirements:
    sdk: SdkPolicy
    runtime_channels: tuple[str, ...]
    sdk_from_global_json: bool = False


def _read(path: Path, root: Path) -> str:
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ToolchainError(
            f"toolchain file must be a regular file inside the repository: {path.name}"
        )
    if path.stat().st_size > 1_000_000:
        raise ToolchainError(f"toolchain file is too large: {path.name}")
    return path.read_text(encoding="utf-8-sig")


def read_sdk_policy(path: Path, root: Path) -> SdkPolicy:
    # global.json supports JSON comments. Preserve comment-like text within strings.
    raw = _JSON_TOKEN.sub(
        lambda m: m[0] if m[0].startswith('"') else " ", _read(path, root)
    )
    raw = _TRAILING_COMMA.sub(lambda m: m[0] if m[0].startswith('"') else "", raw)
    try:
        data = json.loads(raw)
        sdk = data.get("sdk", {})
        version = sdk.get("version")
        policy = sdk.get("rollForward", "patch" if version else "latestMajor")
        allow = sdk.get("allowPrerelease", True)  # .NET CLI's non-Visual-Studio default
        if version is not None and not isinstance(version, str):
            raise TypeError("sdk.version must be a full SDK version string")
        if not isinstance(policy, str) or not isinstance(allow, bool):
            raise TypeError("invalid SDK rollForward or allowPrerelease value")
        if sdk.get("paths"):
            raise ValueError(
                "sdk.paths needs a custom sandbox image with detect_dotnet: false"
            )
        return SdkPolicy(version, policy, allow)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ToolchainError(f"invalid {path.relative_to(root)}: {exc}") from None


def select_sdk(versions: list[str], policy: SdkPolicy) -> str:
    """Apply the documented global.json matching rules to available SDK releases."""
    available = [SdkVersion(v) for v in set(versions)]
    available = [v for v in available if policy.allow_prerelease or "-" not in v.text]
    requested = SdkVersion(policy.version) if policy.version else None
    if requested is not None:
        available = [v for v in available if v.key >= requested.key]
        major, minor, patch = requested.numbers
        band = patch // 100
        mode = policy.roll_forward.removeprefix("latest").lower()
        if mode == "disable":
            available = [v for v in available if v.text == requested.text]
        elif mode == "patch":
            available = [
                v
                for v in available
                if v.numbers[:2] == (major, minor) and v.numbers[2] // 100 == band
            ]
        elif mode == "feature":
            available = [v for v in available if v.numbers[:2] == (major, minor)]
        elif mode == "minor":
            available = [v for v in available if v.numbers[0] == major]
        if policy.roll_forward == "patch" and requested in available:
            return requested.text
        if available and policy.roll_forward in {"feature", "minor", "major"}:
            # Non-latest policies prefer the closest compatible feature band, then its latest patch.
            closest = min((*v.numbers[:2], v.numbers[2] // 100) for v in available)
            available = [
                v for v in available if (*v.numbers[:2], v.numbers[2] // 100) == closest
            ]
    if not available:
        raise ToolchainError(
            f"no SDK satisfies version={policy.version}, rollForward={policy.roll_forward}, allowPrerelease={policy.allow_prerelease}"
        )
    return max(available, key=lambda v: v.key).text


def _properties(
    path: Path,
    root: Path,
    properties: dict[str, str | None],
    seen: frozenset[Path] = frozenset(),
) -> None:
    if path.resolve() in seen:
        raise ToolchainError(f"cyclic property import in {path.relative_to(root)}")
    seen = seen | {path.resolve()}
    try:
        tree = ElementTree.fromstring(_read(path, root))
    except ElementTree.ParseError as exc:
        raise ToolchainError(
            f"invalid project XML in {path.relative_to(root)}: {exc}"
        ) from None
    for group in tree:
        if group.tag.rsplit("}", 1)[-1] == "Import":
            imported = _PROPERTY.sub(
                lambda m: properties.get(m[1].casefold()) or m[0],
                group.get("Project", ""),
            )
            if (
                group.get("Condition")
                or not imported
                or any(c in imported for c in "$*?;")
            ):
                raise ToolchainError(
                    f"cannot statically resolve property import in {path.relative_to(root)}; use a custom image with detect_dotnet: false"
                )
            _properties(path.parent / imported, root, properties, seen)
            continue
        if group.tag.rsplit("}", 1)[-1] in {"Choose", "ImportGroup"}:
            raise ToolchainError(
                f"dynamic property evaluation in {path.relative_to(root)} requires a custom sandbox image"
            )
        if group.tag.rsplit("}", 1)[-1] != "PropertyGroup":
            continue
        for prop in group:
            name = prop.tag.rsplit("}", 1)[-1].casefold()
            value = (prop.text or "").strip()
            conditions = [
                c for c in (group.get("Condition"), prop.get("Condition")) if c
            ]
            condition = (
                conditions[0] if len(conditions) == 1 else " && ".join(conditions)
            )
            if condition:
                # Common shared-property default: set Foo only when Foo is unset.
                empty_default = re.fullmatch(
                    r"\s*['\"]\$\(" + re.escape(name) + r"\)['\"]\s*==\s*['\"]['\"]\s*",
                    condition,
                    flags=re.IGNORECASE,
                )
                if empty_default:
                    if properties.get(name):
                        continue
                else:
                    properties[name] = None
                    continue
            properties[name] = _PROPERTY.sub(
                lambda m: properties.get(m[1].casefold()) or m[0], value
            )


def detect_dotnet(root: Path) -> DotnetRequirements | None:
    root = root.resolve()
    root_policy = (
        read_sdk_policy(root / "global.json", root)
        if (root / "global.json").exists()
        else None
    )
    policies: set[SdkPolicy] = set()
    if root_policy:
        policies.add(root_policy)
    channels: set[str] = set()
    found_project = False
    for directory, dirs, files in os.walk(root, followlinks=False):
        here = Path(directory)
        dirs[:] = [
            d
            for d in dirs
            if d not in {".git", "bin", "obj", "node_modules", ".vs"}
            and not (here / d).is_symlink()
        ]
        for name in sorted(files):
            if not name.lower().endswith((".csproj", ".fsproj", ".vbproj")):
                continue
            found_project = True
            ancestors = [here, *here.parents[: len(here.relative_to(root).parts)]]
            policy_file = next(
                (p / "global.json" for p in ancestors if (p / "global.json").exists()),
                None,
            )
            if policy_file:
                policies.add(read_sdk_policy(policy_file, root))
            properties: dict[str, str | None] = {}
            props = next(
                (
                    p / "Directory.Build.props"
                    for p in ancestors
                    if (p / "Directory.Build.props").exists()
                ),
                None,
            )
            if props:
                _properties(props, root, properties)
            _properties(here / name, root, properties)
            targets_file = next(
                (
                    p / "Directory.Build.targets"
                    for p in ancestors
                    if (p / "Directory.Build.targets").exists()
                ),
                None,
            )
            if targets_file:
                _properties(targets_file, root, properties)
            if any(
                key in properties and properties[key] is None
                for key in ("targetframework", "targetframeworks")
            ):
                raise ToolchainError(
                    f"conditional target frameworks in {(here / name).relative_to(root)} require a custom sandbox image"
                )
            if properties.get("runtimeframeworkversion"):
                raise ToolchainError(
                    "explicit RuntimeFrameworkVersion requires a custom image with detect_dotnet: false"
                )
            targets = properties.get("targetframework") or properties.get(
                "targetframeworks"
            )
            if not targets or "$" in targets or "@(" in targets:
                raise ToolchainError(
                    f"cannot statically resolve TargetFramework(s) in {(here / name).relative_to(root)}; use a custom image with detect_dotnet: false"
                )
            for target in targets.split(";"):
                target = target.strip()
                if re.fullmatch(r"netstandard\d+\.\d+", target):
                    continue
                match = re.fullmatch(r"(?:net|netcoreapp)(\d+)\.(\d+)", target)
                if not match:
                    raise ToolchainError(
                        f"unsupported target framework {target!r}; the managed sandbox supports portable .NET targets on Linux"
                    )
                channels.add(f"{int(match[1])}.{int(match[2])}")
    if not found_project and not policies:
        return None
    if len(policies) > 1:
        raise ToolchainError(
            "repository has conflicting global.json SDK requirements; configure a custom image with detect_dotnet: false"
        )
    runtime_channels = tuple(
        sorted(channels, key=lambda v: tuple(map(int, v.split("."))))
    )
    return DotnetRequirements(
        next(iter(policies), SdkPolicy()), runtime_channels, bool(policies)
    )
