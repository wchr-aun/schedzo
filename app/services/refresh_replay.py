"""Bounded, process-local responses for immediate refresh retries."""

from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from weakref import WeakKeyDictionary

from app.db.session import SessionFactory
from app.domain.sessions import AppTokenPair

REFRESH_REPLAY_WINDOW_SECONDS = 5
MAX_CACHED_REFRESH_RESPONSES = 4096


@dataclass(frozen=True)
class _Response:
    predecessor_hash: str
    successor_hash: str
    pair: AppTokenPair = field(repr=False)
    deadline: float


class RefreshReplayCache:
    def __init__(self) -> None:
        self._responses: OrderedDict[str, _Response] = OrderedDict()
        self._lock = Lock()

    def _prune(self, now: float) -> None:
        # Access-token lifetimes can shorten the replay window, so deadlines
        # are not necessarily ordered by insertion time.
        for session_id, response in list(self._responses.items()):
            if response.deadline <= now:
                del self._responses[session_id]

    def record(
        self,
        session_id: str,
        predecessor_hash: str,
        successor_hash: str,
        pair: AppTokenPair,
        lifetime: float,
    ) -> None:
        with self._lock:
            now = monotonic()
            self._prune(now)
            self._responses.pop(session_id, None)
            if lifetime <= 0:
                return
            while len(self._responses) >= MAX_CACHED_REFRESH_RESPONSES:
                self._responses.popitem(last=False)
            self._responses[session_id] = _Response(
                predecessor_hash,
                successor_hash,
                pair,
                now + min(lifetime, REFRESH_REPLAY_WINDOW_SECONDS),
            )

    def lookup(
        self, session_id: str, predecessor_hash: str, current_hash: str
    ) -> AppTokenPair | None:
        with self._lock:
            self._prune(monotonic())
            response = self._responses.get(session_id)
            if (
                response is not None
                and response.predecessor_hash == predecessor_hash
                and response.successor_hash == current_hash
            ):
                return response.pair
        return None

    def discard(self, session_id: str) -> None:
        with self._lock:
            self._responses.pop(session_id, None)


_caches: WeakKeyDictionary[SessionFactory, RefreshReplayCache] = WeakKeyDictionary()
_cache_lock = Lock()


def refresh_replay_cache(session_factory: SessionFactory) -> RefreshReplayCache:
    # Scope results to this application's database/session factory. Never return
    # another app's tokens or persist recoverable app refresh tokens in SQLite.
    with _cache_lock:
        cache = _caches.get(session_factory)
        if cache is None:
            cache = RefreshReplayCache()
            _caches[session_factory] = cache
        return cache
