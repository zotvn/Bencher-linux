import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bencher_linux import benchmark, config, probe
from bencher_linux.frame_stream import SECOND


class SourceAttribution(unittest.TestCase):
    def make(self, generation, generated):
        class Stream:
            def read(self):
                finish = SECOND * 5
                return iter((stamp, 10.0, generated) for stamp in range(10_000_000, finish, 10_000_000))

        return benchmark.make_report(
            {'title': 'Example', 'generation': generation}, {'application': Stream()},
            0, 5 * SECOND, [], 'stats', [],
        )

    def test_builtin_generation_does_not_get_labeled_as_real_or_lsfg(self):
        data = self.make('in-game', 0)
        self.assertFalse(data['real_events'])
        self.assertAlmostEqual(data['application_summary']['avg_fps'], 99.8)

    def test_observed_lsfg_output_is_counted_separately(self):
        data = self.make('lsfg', 2)
        self.assertAlmostEqual(data['real_summary']['avg_fps'], 99.8)
        self.assertAlmostEqual(data['real_summary']['generated_fps'], 199.6)

    def test_unknown_generation_keeps_application_frames(self):
        self.assertFalse(self.make('unknown', 0)['real_summary'])

    def test_presented_average_excludes_missing_tail(self):
        events = [[index / 100, 10.0] for index in range(1, 301)]
        stats = benchmark.summarize(events, 5)
        self.assertEqual(stats['observed_until_s'], 3)
        self.assertAlmostEqual(stats['avg_fps'], 100)


class LaunchAndSettings(unittest.TestCase):
    def test_native_and_proton_target_selection(self):
        self.assertEqual(probe.executable_name(['gamescope', '-b', '--', '/usr/bin/example']), 'example')
        self.assertEqual(probe.executable_name(['launcher', r'C:\Example\Game.exe']), 'Game.exe')

    def test_settings_preserve_metric_visibility_and_trim_unknowns(self):
        result = config.validate({'interval': 3, 'visible': ['gpu', 'invalid'], 'generation': 'in-game'})
        self.assertEqual(result['visible'], ['gpu'])
        self.assertEqual(result['interval'], 3)
        self.assertEqual(result['generation'], 'in-game')

    def test_lsfg_prefers_64_bit_manifest(self):
        with TemporaryDirectory() as folder, patch.dict('os.environ', {'XDG_DATA_HOME': folder}, clear=True):
            root = Path(folder)
            manifest_dir = root / 'vulkan/implicit_layer.d'
            manifest_dir.mkdir(parents=True)
            for label, name in (('a', 'VK_LAYER_LSFGVK_frame_generation_x86'),
                                ('b', 'VK_LAYER_LSFGVK_frame_generation')):
                (manifest_dir / f'{label}-lsfg.json').write_text(json.dumps({'layer': {'name': name}}))
            with patch.object(probe, 'CONFIG', root / 'app'):
                self.assertEqual(probe.lsfg_manifest(), manifest_dir / 'b-lsfg.json')
