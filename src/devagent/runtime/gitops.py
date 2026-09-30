"""Git operations performed by the orchestrator (never by the agent).

The agent edits the working tree inside the sandbox, so everything under it is untrusted. Therefore:
- nested repositories (any `.git` below the top level) are rejected before git looks at the tree — git would
  otherwise run a nested repo's own config (e.g. filter drivers) when checking it as a submodule;
- submodules are ignored by every command; repo hooks are disabled (empty `core.hooksPath`);
- git runs with a minimal environment (no orchestrator secrets), no system/global config, and `safe.directory`
  limited to the one repo;
- pushes go to the known clone URL, never `origin` (which could be repointed);
- diffs are forced to text without textconv/external drivers, so secret scanning sees every byte.
Credentials are passed only through `GIT_CONFIG_*` env for clone/ls-remote/push and are masked in errors.
"""

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from devagent.core.guardrails import Redactor

_ENV_ALLOWLIST = (
    "PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
)  # fmt: skip


class GitError(Exception):
    pass


def nested_git_entries(repo: Path) -> list[str]:
    """Paths (relative, posix) of `.git` files/dirs below the repo root. Symlinks are not followed."""
    found: list[str] = []
    for root, dirs, files in os.walk(repo):
        here = Path(root)
        if here == repo and ".git" in dirs:
            dirs.remove(".git")  # the real repository
        for name in [*dirs, *files]:
            if name == ".git":
                found.append((here / name).relative_to(repo).as_posix())
        dirs[:] = [d for d in dirs if d != ".git"]
    return sorted(found)


class GitOps:
    def __init__(
        self, *, author_name: str, author_email: str, timeout_seconds: float = 600
    ) -> None:
        self._author = (author_name, author_email)
        self._timeout = timeout_seconds
        scratch = Path(tempfile.mkdtemp(prefix="devagent-git-"))
        # Empty hooks dir, empty HOME and an empty global config: nothing outside our flags configures git.
        self._no_hooks = scratch / "hooks"
        self._no_hooks.mkdir()
        self._home = scratch / "home"
        self._home.mkdir()
        self._global_config = scratch / "gitconfig"
        self._global_config.write_text("", encoding="utf-8")

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
        env.update(
            HOME=str(self._home),
            USERPROFILE=str(self._home),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=str(self._global_config),
            GIT_TERMINAL_PROMPT="0",
        )
        env.update(extra or {})
        return env

    def _run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        secrets = list((env or {}).values())
        safe_dir = cwd.resolve().as_posix() if cwd is not None else ""
        command = [
            "git",
            "-c", f"core.hooksPath={self._no_hooks.as_posix()}",
            "-c", "core.fsmonitor=false",
            "-c", "credential.helper=",
            "-c", "protocol.ext.allow=never",
            "-c", "diff.ignoreSubmodules=all",
            "-c", "status.submoduleSummary=false",
            "-c", "submodule.recurse=false",
            "-c", "core.quotePath=false",
            # The sandbox writes files as its own uid; trust exactly this repo, nothing else.
            "-c", f"safe.directory={safe_dir}",
            *args,
        ]  # fmt: skip
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=self.child_env(env),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out after {self._timeout}s") from None
        if result.returncode != 0:
            detail = Redactor(secrets).redact((result.stderr or result.stdout).strip())[
                -2000:
            ]
            raise GitError(f"git {args[0]} failed (exit {result.returncode}): {detail}")
        return result.stdout.strip()

    def _reject_nested_repos(self, repo: Path) -> None:
        nested = nested_git_entries(repo)
        if nested:
            raise GitError(
                "nested git repository in the working tree (not allowed): "
                + ", ".join(nested[:10])
            )

    def clone(self, url: str, dest: Path, *, base: str, env: Mapping[str, str]) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ["clone", "--quiet", "--no-tags", "--no-recurse-submodules", "--branch", base, "--single-branch",
             url, str(dest)],
            env=env,
        )  # fmt: skip

    def create_branch(self, repo: Path, name: str) -> None:
        self._run(["switch", "--quiet", "--create", name], cwd=repo)

    def head_sha(self, repo: Path) -> str:
        return self._run(["rev-parse", "HEAD"], cwd=repo)

    def commit_all(self, repo: Path, message: str) -> bool:
        """Stage and commit everything in the working tree; returns False when there was nothing to commit."""
        self._reject_nested_repos(repo)
        self._run(["add", "--all"], cwd=repo)
        if not self._run(
            ["status", "--porcelain", "--ignore-submodules=all"], cwd=repo
        ):
            return False
        name, email = self._author
        self._run(
            [
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "--quiet",
                "--no-verify",
                "-m",
                message,
            ],
            cwd=repo,
        )
        return True

    def changed_files(self, repo: Path, *, base: str) -> list[str]:
        # -z gives exact, unquoted paths; git's default quoting turns non-ASCII names into "\303\251"-style
        # strings that would slip past protected-path globs.
        out = self._run(
            ["diff", "-z", "--name-only", "--no-renames", f"origin/{base}...HEAD"],
            cwd=repo,
        )
        return [path for path in out.split("\0") if path]

    def changed_entries(self, repo: Path, *, base: str) -> list[tuple[str, str]]:
        """(new git mode, path) for every changed path, e.g. ("100644", "a.py"); gitlinks are 160000."""
        out = self._run(
            [
                "diff",
                "-z",
                "--raw",
                "--no-renames",
                "--no-abbrev",
                f"origin/{base}...HEAD",
            ],
            cwd=repo,
        )
        parts = out.split("\0")
        entries: list[tuple[str, str]] = []
        for meta, path in zip(parts[0::2], parts[1::2], strict=False):
            fields = meta.split()
            if len(fields) >= 2 and path:
                entries.append((fields[1], path))
        return entries

    def diff(self, repo: Path, *, base: str) -> str:
        return self._run(
            [
                "diff",
                "--no-color",
                "--text",
                "--no-ext-diff",
                "--no-textconv",
                f"origin/{base}...HEAD",
            ],
            cwd=repo,
        )

    def remote_branch_sha(
        self, url: str, branch: str, *, env: Mapping[str, str]
    ) -> str | None:
        out = self._run(["ls-remote", "--heads", url, f"refs/heads/{branch}"], env=env)
        return out.split()[0] if out else None

    def push(
        self, repo: Path, url: str, refspec: str, *, env: Mapping[str, str]
    ) -> None:
        if refspec.startswith("+") or refspec.startswith("-"):
            raise GitError(
                "refusing to push: force pushes and push options are never allowed"
            )
        self._run(
            [
                "push",
                "--quiet",
                "--no-verify",
                "--porcelain",
                "--no-recurse-submodules",
                url,
                refspec,
            ],
            cwd=repo,
            env=env,
        )
