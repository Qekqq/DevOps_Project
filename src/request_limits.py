"""Bounded process-local admission limits; edge limits are enforced by Nginx."""

import time
from collections import OrderedDict, deque
from threading import Lock


class LoginLimits:
    def __init__(self, *, per_identity=10, total=60, window=60, capacity=4096):
        self.per_identity = per_identity
        self.total = total
        self.window = window
        self.capacity = capacity
        self.identities = OrderedDict()
        self.attempts = deque()
        self.lock = Lock()

    def admit(self, identity, *, now=None):
        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        with self.lock:
            while self.attempts and self.attempts[0] <= cutoff:
                self.attempts.popleft()
            # Entries are ordered by their latest attempt.
            while self.identities:
                key, events = next(iter(self.identities.items()))
                if events[-1] > cutoff:
                    break
                del self.identities[key]
            events = self.identities.get(identity, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(self.attempts) >= self.total or len(events) >= self.per_identity:
                return False
            if (
                identity not in self.identities
                and len(self.identities) >= self.capacity
            ):
                return False
            events.append(now)
            self.identities[identity] = events
            self.identities.move_to_end(identity)
            self.attempts.append(now)
            return True
