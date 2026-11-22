"""Read only the recent frames required by the live desktop window."""

import statistics
import time

from .benchmark import paths
from .frame_stream import FrameStream, FrameWindow, SECOND, WINDOW
from .telemetry import Hardware


class Live:
    def __init__(self):
        self.hardware = Hardware()
        self.ident = None
        self.streams = {}
        self.windows = {}

    def close(self):
        for stream in self.streams.values():
            stream.close()
        self.streams.clear()
        self.windows.clear()

    def sample(self, game):
        ident = game['id'] if game else None
        if self.ident != ident:
            self.close()
            self.ident = ident
        hardware = self.hardware.sample()
        now = time.monotonic_ns()
        frames = {}
        if game:
            for key, location in paths(game).items():
                if key not in self.streams and location and location.is_file():
                    try:
                        self.streams[key] = FrameStream(location, now - WINDOW * SECOND, rendered=key != 'presented')
                    except FileNotFoundError:
                        continue
                    self.windows[key] = FrameWindow()
                if key in self.streams:
                    window = self.windows[key]
                    for row in self.streams[key].read():
                        window.add(*row)
                    frames[key] = window.read(now)
        return hardware, frames

    def plot(self, key):
        window = self.windows.get(key)
        if not window:
            return []
        # Draw a compact median series for recent frames. Exact dips remain in the report.
        recent = [row[1] for row in list(window.recent)[-240:]]
        return [statistics.median(recent[i:i+4]) for i in range(0, len(recent), 4)]
