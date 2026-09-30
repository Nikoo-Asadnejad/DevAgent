"""GitHub issue API errors. Messages never contain tokens or other secrets."""


class TrackerRequestError(Exception):
    """GitHub answered with a non-2xx status. The message never contains request credentials."""

    def __init__(self, method: str, path: str, status_code: int) -> None:
        super().__init__(
            f"{method.upper()} {path.split('?', 1)[0]} failed with HTTP {status_code}"
        )
        self.status_code = status_code
