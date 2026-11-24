"""Signing for website sessions and creative links, admin check, and abuse limits.

The graded /chat endpoint must be open (user_id in the body), so the website does NOT rely on it for
reading data: its /api/me/* routes need a signed session token, and creative images need a signed link.
"""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque

from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)   # dev fallback: sessions reset on restart
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.@:\-]{1,64}$")


def sign(value: str) -> str:
    return hmac.new(SECRET_KEY.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(user_id: str) -> str:
    return f"{user_id}.{sign('session:' + user_id)}"


def read_token(token: str) -> str | None:
    user_id, _, sig = (token or "").rpartition(".")
    if user_id and ID_PATTERN.match(user_id) and hmac.compare_digest(sig, sign("session:" + user_id)):
        return user_id
    return None


def creative_sig(variant_id: str) -> str:
    return sign("creative:" + variant_id)[:16]


def is_admin(password: str | None) -> bool:
    return bool(ADMIN_PASSWORD) and hmac.compare_digest((password or "").encode(), ADMIN_PASSWORD.encode())


class RateLimiter:
    """Sliding window per key, in memory (one Render instance). Protects the $2 key from a public link."""

    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


_thread_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def thread_lock(key: str) -> threading.Lock:
    """One request at a time per conversation, so two messages can't interleave in the same chat."""
    with _locks_guard:
        return _thread_locks[key]
