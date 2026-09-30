"""Guardrails enforced before workflow side effects run."""

import json
import re
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any

from devagent.core.config import GuardrailsConfig


class GuardrailViolation(Exception):
    pass


# Subset of `git check-ref-format` rules, plus a few characters we never want in an agent branch.
_BAD_REF = re.compile(r"\.\.|//|@\{|[\s~^:?*\[\\]|\.lock(/|$)|(^|/)[.-]|/$")


def check_push_target(branch: str, guardrails: GuardrailsConfig) -> None:
    """#2: pushes only go to `<branch_prefix>*` branches that are not protected and are well-formed refs."""
    prefix = guardrails.branch_prefix
    if not branch.startswith(prefix) or len(branch) == len(prefix):
        raise GuardrailViolation(
            f"refusing to push to '{branch}': only '{prefix}*' branches are allowed"
        )
    if _BAD_REF.search(branch):
        raise GuardrailViolation(
            f"refusing to push to '{branch}': not a well-formed branch name"
        )
    for pattern in guardrails.protected_branches:
        if fnmatchcase(branch, pattern):
            raise GuardrailViolation(
                f"refusing to push to '{branch}': matches protected branch '{pattern}'"
            )


def ensure_new_branch(branch: str, *, exists_on_remote: bool) -> None:
    """#1: never push to an existing branch."""
    if exists_on_remote:
        raise GuardrailViolation(
            f"branch '{branch}' already exists on the remote; agent runs always use a new branch"
        )


def push_refspec(branch: str, guardrails: GuardrailsConfig) -> str:
    """The only refspec the publish step may use: explicit, single ref, never forced (no leading '+')."""
    check_push_target(branch, guardrails)
    return f"HEAD:refs/heads/{branch}"


def protected_path_violations(
    changed_files: Iterable[str], patterns: Iterable[str]
) -> list[str]:
    """#8: changed files that match any protected path pattern."""
    pattern_list = list(patterns)
    violations: list[str] = []
    for raw in changed_files:
        path = raw.replace("\\", "/")
        if any(PurePosixPath(path).full_match(pattern) for pattern in pattern_list):
            violations.append(path)
    return violations


def secret_fragments(raw: str) -> list[str]:
    """String values inside a JSON credential blob (e.g. Codex auth.json tokens), so they are masked individually."""
    try:
        data: Any = json.loads(raw)
    except ValueError:
        return []
    leaves: list[str] = []
    stack: list[Any] = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        elif isinstance(item, list):
            stack.extend(item)  # pyright: ignore[reportUnknownArgumentType]
        elif isinstance(item, str) and len(item) >= 8:
            leaves.append(item)
    return leaves


class Redactor:
    """#7: masks known secret values in logs and transcripts before they are stored."""

    MASK = "***"
    MIN_LENGTH = 4  # shorter values would mask ordinary text

    def __init__(self, secrets: Iterable[str]) -> None:
        values = {s for s in secrets if len(s) >= self.MIN_LENGTH}
        self._secrets = sorted(values, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, self.MASK)
        return text

    def contains_secret(self, text: str) -> bool:
        return any(secret in text for secret in self._secrets)


_DISALLOWED_MODES = {"160000": "submodule/gitlink", "120000": "symlink"}


def disallowed_entries(entries: Iterable[tuple[str, str]]) -> list[str]:
    """#8: gitlinks and symlinks are never published (they can point outside the reviewed diff)."""
    return [
        f"{path} ({_DISALLOWED_MODES[mode]})"
        for mode, path in entries
        if mode in _DISALLOWED_MODES
    ]


def check_daily_budget(*, runs_today: int, max_runs_per_day: int) -> None:
    """#10: stop starting new runs once the daily budget is used."""
    if runs_today >= max_runs_per_day:
        raise GuardrailViolation(
            f"daily run budget reached ({runs_today}/{max_runs_per_day})"
        )
