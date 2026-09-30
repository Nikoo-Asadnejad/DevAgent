"""HTTP client that only sends requests on an explicit allowlist.

Every adapter talks to its provider through this client. A request whose method + path is not allowed raises
`ForbiddenRequest` before anything is sent, so a bug (or a future mistake) cannot edit a task or merge a PR.

Path patterns: `*` alone allows any path; `{name}` matches exactly one path segment; `**` matches any number of
segments; everything else is literal. Paths are relative to the client's base URL and matched without the query.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx


class ForbiddenRequest(Exception):
    pass


_TOKEN = re.compile(r"\*\*|\{[A-Za-z_][A-Za-z0-9_]*\}")


def _compile(pattern: str) -> re.Pattern[str]:
    if pattern == "*":
        return re.compile(r"/.*")
    regex = ""
    last = 0
    for match in _TOKEN.finditer(pattern):
        regex += re.escape(pattern[last : match.start()])
        regex += ".*" if match.group() == "**" else "[^/]+"
        last = match.end()
    regex += re.escape(pattern[last:])
    return re.compile(regex)


@dataclass(frozen=True)
class AllowRule:
    method: str
    path: str
    _regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "_regex", _compile(self.path))

    def allows(self, method: str, path: str) -> bool:
        return method == self.method and self._regex.fullmatch(path) is not None


class AllowlistedClient:
    def __init__(
        self,
        base_url: str,
        rules: Sequence[AllowRule],
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._rules = tuple(rules)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=dict(headers or {}),
            params=dict(params or {}),
            timeout=timeout,
            transport=transport,
        )

    def check(self, method: str, path: str) -> None:
        method = method.upper()
        if "://" in path or path.startswith("//") or not path.startswith("/"):
            raise ForbiddenRequest(
                f"{method} {path}: only paths relative to the provider base URL are allowed"
            )
        bare = path.split("?", 1)[0]
        if any(segment in (".", "..") for segment in bare.split("/")):
            raise ForbiddenRequest(f"{method} {bare}: dot segments are not allowed")
        if not any(rule.allows(method, bare) for rule in self._rules):
            raise ForbiddenRequest(
                f"{method} {bare} is not on this provider's allowlist"
            )

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.check(method, path)
        return self._client.request(method.upper(), path, **kwargs)

    def close(self) -> None:
        self._client.close()
