"""Bounded, in-process request measurements for the owner UI and API."""
import logging
import math
import threading
from collections import deque


class Perf:
    def __init__(self):
        self._requests = deque(maxlen=500)
        self._lock = threading.Lock()

    def record(self, path, method, duration_ms, git_spawns):
        row = dict(path=path, method=method, duration_ms=duration_ms, git_spawns=git_spawns)
        with self._lock:
            self._requests.append(row)
        if duration_ms > 1000:
            logging.getLogger("hub").warning("slow request path=%s method=%s duration_ms=%.2f git_spawns=%d",
                                             path, method, duration_ms, git_spawns)

    def snapshot(self):
        with self._lock:
            rows = list(self._requests)
        groups = {}
        for row in rows:
            groups.setdefault((row['method'], row['path']), []).append(row['duration_ms'])
        routes = []
        for (method, path), values in sorted(groups.items()):
            values.sort()
            routes.append(dict(method=method, path=path, count=len(values),
                               p50_ms=values[math.ceil(len(values) * .5) - 1],
                               p95_ms=values[math.ceil(len(values) * .95) - 1]))
        return dict(routes=routes, slow=[r for r in reversed(rows) if r['duration_ms'] > 1000])
