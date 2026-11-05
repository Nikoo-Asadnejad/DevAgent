import json
from pathlib import Path

import pytest

from devagent.core.dotnet import SdkPolicy, ToolchainError, detect_dotnet, select_sdk


def project(root: Path, content: str, name: str = "App.csproj") -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<Project Sdk="Microsoft.NET.Sdk">{content}</Project>')


def test_global_json_with_comments_and_shared_project_properties(
    tmp_path: Path,
) -> None:
    (tmp_path / "global.json").write_text("""{
      // Keep the repo's SDK policy.
      "sdk": {"version": "8.0.100", "rollForward": "latestFeature", "allowPrerelease": false,},
    }""")
    (tmp_path / "Directory.Build.props").write_text("""<Project><PropertyGroup>
      <DotnetVersion>8.0</DotnetVersion>
      <TargetFrameworks Condition="'$(TargetFrameworks)' == ''">net$(DotnetVersion);net6.0</TargetFrameworks>
    </PropertyGroup></Project>""")
    project(tmp_path, "", "src/App/App.csproj")

    result = detect_dotnet(tmp_path)

    assert result is not None
    assert result.sdk == SdkPolicy("8.0.100", "latestFeature", False)
    assert result.runtime_channels == ("6.0", "8.0")
    assert result.sdk_from_global_json


def test_nearest_props_and_project_override(tmp_path: Path) -> None:
    (tmp_path / "Directory.Build.props").write_text(
        "<Project><PropertyGroup><TargetFramework>net6.0</TargetFramework></PropertyGroup></Project>"
    )
    project(
        tmp_path,
        "<PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup>",
        "src/App.csproj",
    )
    (tmp_path / "src/Directory.Build.props").write_text(
        "<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>"
    )
    result = detect_dotnet(tmp_path)
    assert result is not None
    assert result.runtime_channels == ("10.0",)
    assert result.sdk == SdkPolicy()


def test_literal_shared_property_import(tmp_path: Path) -> None:
    (tmp_path / "versions.props").write_text(
        "<Project><PropertyGroup><Framework>net9.0</Framework></PropertyGroup></Project>"
    )
    (tmp_path / "Directory.Build.props").write_text(
        '<Project><Import Project="versions.props" /></Project>'
    )
    project(
        tmp_path,
        "<PropertyGroup><TargetFramework>$(Framework)</TargetFramework></PropertyGroup>",
    )
    result = detect_dotnet(tmp_path)
    assert result is not None and result.runtime_channels == ("9.0",)


def test_properties_are_case_insensitive_and_targets_are_applied_last(
    tmp_path: Path,
) -> None:
    project(
        tmp_path,
        "<PropertyGroup><Framework>net8.0</Framework><TargetFramework>$(framework)</TargetFramework></PropertyGroup>",
    )
    (tmp_path / "Directory.Build.targets").write_text(
        "<Project><PropertyGroup><TargetFramework>net9.0</TargetFramework></PropertyGroup></Project>"
    )
    result = detect_dotnet(tmp_path)
    assert result is not None and result.runtime_channels == ("9.0",)


@pytest.mark.parametrize(
    "framework", ["net8.0-windows", "net48", "$(ExternalFramework)"]
)
def test_unsupported_or_unresolved_framework_fails_clearly(
    tmp_path: Path, framework: str
) -> None:
    project(
        tmp_path,
        f"<PropertyGroup><TargetFramework>{framework}</TargetFramework></PropertyGroup>",
    )
    with pytest.raises(ToolchainError):
        detect_dotnet(tmp_path)


def test_conditional_targets_are_not_guessed(tmp_path: Path) -> None:
    project(
        tmp_path,
        "<PropertyGroup Condition=\"'$(Configuration)' == 'Debug'\"><TargetFramework>net8.0</TargetFramework></PropertyGroup>",
    )
    with pytest.raises(ToolchainError, match="conditional target"):
        detect_dotnet(tmp_path)


def test_conflicting_nested_sdk_policies_are_rejected(tmp_path: Path) -> None:
    for directory, version in [("a", "8.0.100"), ("b", "9.0.100")]:
        project(
            tmp_path,
            "<PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>",
            f"{directory}/App.csproj",
        )
        (tmp_path / directory / "global.json").write_text(
            json.dumps({"sdk": {"version": version}})
        )
    with pytest.raises(ToolchainError, match="conflicting global.json"):
        detect_dotnet(tmp_path)


def test_symlink_project_cannot_read_outside_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project(
        tmp_path,
        "<PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>",
    )
    (repo / "App.csproj").symlink_to(tmp_path / "App.csproj")
    with pytest.raises(ToolchainError, match="regular file inside"):
        detect_dotnet(repo)


def test_non_dotnet_repo_uses_base_image(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    assert detect_dotnet(tmp_path) is None


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("disable", "8.0.100"),
        ("patch", "8.0.100"),
        ("latestPatch", "8.0.105"),
        ("feature", "8.0.105"),
        ("latestFeature", "8.0.402"),
        ("minor", "8.0.105"),
        ("latestMinor", "8.1.101"),
        ("major", "8.0.105"),
        ("latestMajor", "10.0.100"),
    ],
)
def test_sdk_roll_forward_policies(policy: str, expected: str) -> None:
    versions = [
        "8.0.100",
        "8.0.105",
        "8.0.402",
        "8.1.101",
        "9.0.200",
        "10.0.100",
        "11.0.100-preview.1",
    ]
    assert select_sdk(versions, SdkPolicy("8.0.100", policy, False)) == expected


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("patch", "8.0.105"),
        ("feature", "8.0.203"),
        ("minor", "8.1.101"),
        ("major", "9.0.101"),
    ],
)
def test_roll_forward_uses_closest_available_band(policy: str, expected: str) -> None:
    versions = {
        "patch": ["8.0.105", "8.0.203"],
        "feature": ["8.0.201", "8.0.203", "8.0.401"],
        "minor": ["8.1.101", "8.1.201", "8.2.101"],
        "major": ["9.0.101", "9.0.201", "10.0.100"],
    }
    assert select_sdk(versions[policy], SdkPolicy("8.0.100", policy)) == expected


def test_exact_sdk_and_prerelease_rules() -> None:
    with pytest.raises(ToolchainError, match="no SDK satisfies"):
        select_sdk(["8.0.101"], SdkPolicy("8.0.100", "disable"))
    with pytest.raises(ToolchainError):
        select_sdk(["8.0.200"], SdkPolicy("8.0.100", "patch"))
    versions = ["10.0.100", "11.0.100-preview.2", "11.0.100-preview.10"]
    assert (
        select_sdk(versions, SdkPolicy(allow_prerelease=True)) == "11.0.100-preview.10"
    )
    assert select_sdk(versions, SdkPolicy()) == "10.0.100"
