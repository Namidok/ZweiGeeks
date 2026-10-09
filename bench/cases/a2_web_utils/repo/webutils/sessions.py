"""Signed-in user sessions, stored client-side in a cookie."""

from __future__ import annotations

import base64
import pickle
import time
from dataclasses import dataclass, field

SESSION_TTL_SECONDS = 3600


@dataclass
class Session:
    user_id: int
    roles: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def is_admin(self) -> bool:
        return "admin" in self.roles


def session_to_cookie(session: Session) -> str:
    return base64.b64encode(pickle.dumps(session)).decode("ascii")


def session_from_cookie(cookie: str) -> Session:
    return pickle.loads(base64.b64decode(cookie))


def is_fresh(session: Session, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - session.created_at < SESSION_TTL_SECONDS
