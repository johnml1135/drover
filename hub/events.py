"""In-process pub/sub that feeds the UI's SSE stream at /events."""
import asyncio
import itertools
import threading
from collections import deque

try:
    from fastapi import APIRouter, Request
    from fastapi.sse import EventSourceResponse
except ModuleNotFoundError as exc:  # Bus and CoalescedWake are also useful without hub dependencies.
    if exc.name not in ("fastapi", "fastapi.sse"):
        raise
    APIRouter = EventSourceResponse = None
    Request = object

QUEUE_MAX = 256  # above the replay history so a replay always fits


class CoalescedWake:
    """A disposable, thread-safe hint with a generation to avoid clear/wait races.

    Callers snapshot before doing work, then wait for a later generation. Any
    number of wake calls during that work collapse into one immediate pass.
    """
    def __init__(self):
        self._condition = threading.Condition()
        self._generation = 0

    def generation(self) -> int:
        with self._condition:
            return self._generation

    def wake(self) -> None:
        with self._condition:
            self._generation += 1
            self._condition.notify_all()

    def wait(self, after: int, timeout: float | None = None) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._generation != after, timeout)


def _offer(q: asyncio.Queue, ev: dict) -> None:
    """Put without growing unbounded: drop the oldest and tell the client to resync."""
    if q.full():
        while q.full():
            q.get_nowait()
        q.put_nowait({"id": ev["id"], "kind": "resync", "data": {}})
        if q.full():
            q.get_nowait()
    q.put_nowait(ev)

class Bus:
    """Thread-safe publish; each subscriber gets its own asyncio queue.
    A ring buffer of recent events supports Last-Event-ID replay.
    """
    def __init__(self, history: int = 200):
        self._lock = threading.Lock()
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._ids = itertools.count(1)
        self._recent: deque = deque(maxlen=history)
        self.on_publish = None
    def publish(self, kind: str, data: dict | None = None) -> dict:
        with self._lock:
            ev = {"id": next(self._ids), "kind": kind, "data": data or {}}
            self._recent.append(ev)
            subs = list(self._subs)
        if self.on_publish is not None:
            self.on_publish(kind, data or {})
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_offer, q, ev)
            except RuntimeError:  # loop closed
                self.unsubscribe(q)
        return ev
    def subscribe(self, after_id: int | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        with self._lock:
            if after_id is not None:
                for ev in self._recent:
                    if ev["id"] > after_id:
                        q.put_nowait(ev)
            self._subs.append((asyncio.get_running_loop(), q))
        return q
    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [(lo, s) for lo, s in self._subs if s is not q]

    async def wait(self, q: asyncio.Queue, timeout: float | None = None) -> bool:
        """Wait for a disposable hint and coalesce any queued hints into one pass."""
        try:
            await asyncio.wait_for(q.get(), timeout)
        except asyncio.TimeoutError:
            return False
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                return True

    def recent(self) -> list[dict]:
        with self._lock:
            return list(self._recent)
async def stream(bus: Bus, after_id: int | None = None, limit: int | None = None):
    """Async generator of ServerSentEvent; ends after `limit` events if set."""
    from fastapi.sse import ServerSentEvent
    q = bus.subscribe(after_id)
    sent = 0
    try:
        while limit is None or sent < limit:
            ev = await q.get()
            yield ServerSentEvent(
                data={"kind": ev["kind"], **ev["data"]}, event="ledger",
                id=str(ev["id"])
            )
            sent += 1
    finally:
        bus.unsubscribe(q)
router = APIRouter() if APIRouter is not None else None
if router is not None:
    @router.get("/events", response_class=EventSourceResponse)
    async def sse(request: Request, limit: int | None = None):
        last = request.headers.get("last-event-id")
        after = int(last) if last and last.isdigit() else None
        async for ev in stream(request.app.state.bus, after, limit):
            yield ev
