"""Session-scoped id generation for request_id / call_id / entry_id / flush_id.

Ids are short, unique within a session, and carry a prefix so a trace
reader can tell what they are: `req-7`, `call-3`, `ent-41`, `f-2`.
"""

from __future__ import annotations

import itertools
from collections import defaultdict


class IdGen:
    def __init__(self) -> None:
        self._counters: dict[str, itertools.count[int]] = defaultdict(lambda: itertools.count(1))

    def new(self, prefix: str) -> str:
        return f"{prefix}-{next(self._counters[prefix])}"

    def new_turn_n(self) -> int:
        """Next gateway-side turn number (`s<n>`)."""
        return next(self._counters["s"])
