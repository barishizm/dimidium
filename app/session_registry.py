"""In-memory registry of active calls, keyed by our own call SID (live-...).

This is the only intentional piece of global mutable state in the app.
"""

from app.models import CallSession


class CallSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, CallSession] = {}

    def add(self, session: CallSession) -> None:
        if session.call_sid in self._sessions:
            raise ValueError(f"Session already registered: {session.call_sid}")
        self._sessions[session.call_sid] = session

    def get(self, call_sid: str) -> CallSession | None:
        return self._sessions.get(call_sid)

    def remove(self, call_sid: str) -> CallSession | None:
        return self._sessions.pop(call_sid, None)

    def active(self) -> list[CallSession]:
        return list(self._sessions.values())

    def __len__(self) -> int:
        return len(self._sessions)


CALL_SESSION_REGISTRY = CallSessionRegistry()
