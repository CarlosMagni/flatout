"""OpError: a refused command, shared by the app and the features it delegates to."""

from __future__ import annotations

from typing import Any


class OpError(Exception):
    """Command refused: the message (English) is shown as is to agents and, failing a translation, to
    the user. `code` (+ `params`) is the stable reason behind it, set where a page words the refusal
    in its own language, like a log entry's `key`: it travels in the error reply."""

    def __init__(self, message: str, code: str | None = None, **params: Any):
        super().__init__(message)
        self.code = code
        self.params = params
