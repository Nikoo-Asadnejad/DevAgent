from pathlib import Path

import pytest

from devagent.core.config import ConfigError, load_config


def test_github_token_is_loaded_from_environment(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        """
github:
  token: ${GITHUB_TOKEN}
repos:
  - repo: {owner: acme, name: widgets}
    base: main
    commands: {build: make, test: make-test}
""".strip(),
        encoding="utf-8",
    )

    result = load_config(path, {"GITHUB_TOKEN": "secret", "CODEX_AUTH_JSON": "{}"})

    assert result.github.token.get_secret_value() == "secret"
    assert result.repos[0].repo == {"owner": "acme", "name": "widgets"}


def test_provider_blocks_from_old_design_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        """
tracker: {type: legacy, token: old}
github: {token: new}
repos:
  - repo: {owner: acme, name: widgets}
    base: main
    commands: {build: make, test: make-test}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="tracker: Extra inputs are not permitted"):
        load_config(path, {})


def test_database_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        """
github: {token: github-token}
database: {url: postgresql://localhost/devagent}
repos:
  - repo: {owner: acme, name: widgets}
    base: main
    commands: {build: make, test: make-test}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="database: Extra inputs are not permitted"):
        load_config(path, {"CODEX_AUTH_JSON": "{}"})


def test_codex_login_is_required(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        """
github: {token: github-token}
repos:
  - repo: {owner: acme, name: widgets}
    base: main
    commands: {build: make, test: make-test}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match="missing Codex credential env var.*CODEX_AUTH_JSON"
    ):
        load_config(path, {})
