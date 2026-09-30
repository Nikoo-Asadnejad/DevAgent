"""Secret scanning of the run's diff; scanner failures fail the run closed."""

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

Runner = Callable[..., subprocess.CompletedProcess[str]]


class SecretScanError(Exception):
    pass


class SecretScanner(Protocol):
    def scan(self, diff: str) -> list[str]:
        """Return human-readable findings (never the secret values); empty when clean."""
        ...


class GitleaksScanner:
    def __init__(
        self,
        binary: str = "gitleaks",
        *,
        runner: Runner = subprocess.run,
        timeout: float = 300,
    ) -> None:
        self._binary = binary
        self._run = runner
        self._timeout = timeout

    def scan(self, diff: str) -> list[str]:
        with tempfile.TemporaryDirectory(prefix="devagent-gitleaks-") as tmp:
            report = Path(tmp) / "report.json"
            cmd = [
                self._binary, "stdin",
                "--no-banner", "--redact", "--ignore-gitleaks-allow", "--exit-code", "1",
                "--report-format", "json", "--report-path", str(report),
            ]  # fmt: skip
            try:
                result = self._run(
                    cmd,
                    input=diff,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    check=False,
                )
            except FileNotFoundError:
                raise SecretScanError(
                    f"{self._binary} is not installed; refusing to publish unscanned changes"
                ) from None
            except subprocess.TimeoutExpired:
                raise SecretScanError(f"{self._binary} timed out") from None
            if result.returncode not in (0, 1):
                raise SecretScanError(
                    f"{self._binary} failed (exit {result.returncode}): {result.stderr.strip()[-500:]}"
                )
            if result.returncode == 0:
                return []
            entries: list[dict[str, Any]] = json.loads(
                report.read_text(encoding="utf-8") or "[]"
            )
            return [
                f"{e.get('RuleID', 'secret')} at diff line {e.get('StartLine', '?')}"
                for e in entries
            ]
