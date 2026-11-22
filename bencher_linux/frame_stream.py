"""Read the capture's monotonic per-frame streams without rewriting snapshots."""
from bisect import bisect_left, insort
from collections import deque
from pathlib import Path
import struct

RECORD = struct.Struct('=Qd')
SECOND = 1_000_000_000
WINDOW = 300
OVERVIEW_SECONDS = 2


class FrameStream:
    def __init__(self, path, since, *, rendered=False):
        self.stream = path.open('rb')
        self.rendered = rendered
        self.previous = None
        # Search the fixed-size records so opening the overview does not reread
        # hours of gameplay. Keep one previous record to measure real intervals.
        lo, hi = 0, path.stat().st_size // RECORD.size
        while lo < hi:
            middle = (lo + hi) // 2
            self.stream.seek(middle * RECORD.size)
            stamp, _ = RECORD.unpack(self.stream.read(RECORD.size))
            if stamp < since:
                lo = middle + 1
            else:
                hi = middle
        self.stream.seek(max(0, lo - 1) * RECORD.size)

    def read(self):
        data = self.stream.read()
        complete = len(data) // RECORD.size * RECORD.size
        if complete != len(data):
            self.stream.seek(complete - len(data), 1)
        for stamp, value in RECORD.iter_unpack(data[:complete]):
            if self.rendered:
                previous, self.previous = self.previous, stamp
                if previous is None:
                    continue
                yield stamp, (stamp - previous) / 1e6, int(value)
            else:
                yield stamp, value, 0

    def close(self):
        self.stream.close()


def real_path(directory):
    return max(directory.glob('real-*.bin'), key=lambda path: path.stat().st_size, default=None)


class FrameWindow:
    """Exact slowest-tail boundary of interval samples in the last five minutes."""

    def __init__(self):
        self.frames = deque()
        self.recent = deque()
        self.sorted = []

    def add(self, stamp, ms, generated):
        if not 0 < ms < 60000:
            return
        self.frames.append((stamp, ms))
        self.recent.append((stamp, ms, generated))
        insort(self.sorted, ms)

    def read(self, now):
        cutoff = now - WINDOW * SECOND
        while self.frames and self.frames[0][0] <= cutoff:
            _, ms = self.frames.popleft()
            self.sorted.pop(bisect_left(self.sorted, ms))
        cutoff = now - OVERVIEW_SECONDS * SECOND
        while self.recent and self.recent[0][0] <= cutoff:
            self.recent.popleft()
        if not self.recent:
            return None
        stamp, ms, _ = self.recent[-1]
        span_ms = sum(interval for _, interval, _ in self.recent)
        count = len(self.sorted)
        return dict(stamp=stamp / SECOND, ms=ms, worst_ms=max(interval for _, interval, _ in self.recent),
                    fps=len(self.recent) * 1000 / span_ms,
                    generated_fps=sum(generated for _, _, generated in self.recent) * 1000 / span_ms,
                    low=1000 / self.sorted[-((count + 99) // 100)] if count >= 100 else None)
