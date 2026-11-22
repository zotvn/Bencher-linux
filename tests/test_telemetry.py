from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bencher_linux.telemetry import FrameReader, frame_stats

class Frames(unittest.TestCase):
    def test_frame_weighted_average_and_slowest_one_percent(self):
        data=frame_stats([10]*99+[100])
        self.assertAlmostEqual(data['avg_fps'],100000/1090)
        self.assertEqual(data['low_1'],10)
        self.assertEqual(data['max_fps'],100)
        self.assertEqual(frame_stats([0,-1,float('nan')]),{})

    def test_incremental_csv_handles_partial_writes_and_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp);path=directory/'game_1.csv'
            path.write_text('os,cpu,gpu\nLinux,CPU,GPU\nfps,frametime,cpu_load\n100,10,42\n50,2')
            reader=FrameReader();data=reader.poll(directory)
            self.assertEqual(data['samples'],1)
            self.assertIsNone(data['cpu_render_ms'])
            self.assertIsNone(data['gpu_render_ms'])
            with path.open('a') as stream:stream.write('0,43\n')
            self.assertEqual(reader.poll(directory)['samples'],2)
            self.assertEqual(reader.poll(directory)['samples'],2)
            (directory/'game_2.csv').write_text('fps,frametime,cpu_load\n25,40,40\n')
            (directory/'game_2_summary.csv').write_text('fps,frametime,cpu_load\n1,1000,100\n')
            data=reader.poll(directory)
            self.assertEqual(data['samples'],3)
            self.assertEqual(data['max_fps'],100)
            self.assertEqual(data['fps'],25)
            self.assertEqual(data['max_ms'],40)
            self.assertAlmostEqual(data['avg_fps'],3000/70)

    def test_windows_capture_excludes_proton_helper_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / 'xalia.exe_timestamp.csv').write_text('fps,frametime\n200,5\n')
            (directory / 'ExampleGame.exe_timestamp.csv').write_text('fps,frametime\n100,10\n')
            result = FrameReader().poll(directory, 'ExampleGame.exe')
            self.assertEqual(result['samples'], 1)
            self.assertEqual(result['fps'], 100)

    def test_presentation_samples_reject_invalid_intervals(self):
        reader = FrameReader()
        reader.record([0, 8, ((1 << 64) - 1) / 1_000_000, 12])
        result = reader.stats()
        self.assertEqual(result['samples'], 2)
        self.assertEqual(result['fps'], 100)
        self.assertEqual(result['frame_ms'], 10)
        self.assertEqual(result['max_ms'], 12)

    def test_stutter_marker_retains_individual_spike_without_moving_mean(self):
        reader = FrameReader()
        reader.record([6] * 100)
        reader.record([6] * 99 + [71])
        reader.record([6] * 100)
        result = reader.stats()
        self.assertEqual(result['stutters'], 1)
        self.assertEqual(result['stutter_graph'], [None, 71, None])
        self.assertAlmostEqual(result['graph'][1], 6.65)

    def test_compositor_fps_uses_measured_sample_duration(self):
        reader = FrameReader()
        reader.record([0.1] * 40, elapsed=1.0)
        result = reader.stats()
        self.assertEqual(result['fps'], 40)
        self.assertEqual(result['max_fps'], 40)
        self.assertAlmostEqual(result['avg_fps'], 40)

if __name__=='__main__':unittest.main()
