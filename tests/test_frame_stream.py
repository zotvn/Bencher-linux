import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bencher_linux.benchmark import benchmark_frames, frame_summary
from bencher_linux.frame_stream import FrameStream, FrameWindow, RECORD, SECOND


class FrameTimingTests(unittest.TestCase):
    def test_benchmark_excludes_frames_crossing_start_or_end(self):
        start = 10 * SECOND
        stamps = [start + offset for offset in (-10_000_000, 5_000_000, 30_000_000, 50_000_000)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'real-test.bin'
            path.write_bytes(b''.join(RECORD.pack(stamp, 2) for stamp in stamps))
            stream = FrameStream(path, start, rendered=True)
            try:
                self.assertEqual(benchmark_frames(stream, start, stamps[-1], real=True),
                                 [[0.03, 25.0, 2]])
            finally:
                stream.close()

    def test_real_and_presented_boundaries_use_their_own_frames(self):
        for interval, boundary in ((16, 30), (8, 20)):
            ms = [interval] * 198 + [boundary, 400]
            rows = [[1, frame] for frame in ms]
            with self.subTest(interval=interval):
                summary = frame_summary(rows, 5)
                self.assertEqual(summary['low_1'], 1000 / boundary)
                self.assertEqual(summary['max_ms'], 400)
                self.assertEqual(summary['avg_fps'], 40)
                window = FrameWindow()
                for stamp, frame in enumerate(ms):
                    window.add(SECOND + stamp, frame, 0)
                self.assertEqual(window.read(2 * SECOND)['low'], summary['low_1'])
                self.assertIsNone(window.read(400 * SECOND))


if __name__ == '__main__':
    unittest.main()
