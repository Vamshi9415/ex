"""
Session Memory — in-memory store for conversation turns.
Trade-off: sessions lost on restart. Acceptable for demo/assignment.
Production swap: implement SessionStore protocol against asyncpg/Redis.
"""
from collections import defaultdict
from typing import Protocol, runtime_checkable


@runtime_checkable
class SessionStore(Protocol):
    def get(self, session_id: str) -> list[dict]: ...
    def append(self, session_id: str, role: str, content: str) -> None: ...
    def clear(self, session_id: str) -> None: ...


class InMemorySessionStore:
    """
    Stores last `max_turns` messages per session_id.
    max_turns=20 keeps token budget manageable for the classifier prompt.
    """
    def __init__(self, max_turns: int = 20):
        self._store: dict[str, list[dict]] = defaultdict(list)
        self._max_turns = max_turns

    def get(self, session_id: str) -> list[dict]:
        return list(self._store[session_id][-self._max_turns:])

    def append(self, session_id: str, role: str, content: str) -> None:
        self._store[session_id].append({"role": role, "content": content})

    def clear(self, session_id: str) -> None:
        self._store.pop(session_id, None)

    def all_sessions(self) -> list[str]:
        return list(self._store.keys())


# Global singleton — acceptable for in-memory demo
session_store = InMemorySessionStore()