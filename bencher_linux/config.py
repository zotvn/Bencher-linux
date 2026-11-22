"""XDG paths and validated preferences."""

import json
import os
from pathlib import Path
import tempfile

HOME = Path.home()
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME') or HOME / '.config') / 'bencher-linux'
DATA = Path(os.environ.get('XDG_DATA_HOME') or HOME / '.local/share') / 'bencher-linux'
CACHE = Path(os.environ.get('XDG_CACHE_HOME') or HOME / '.cache') / 'bencher-linux'


def runtime_directory():
    base = os.environ.get('XDG_RUNTIME_DIR')
    root = Path(base) if base else CACHE
    return root / 'bencher-linux' if base else root / 'runtime'


RUNTIME = runtime_directory()
PREFERENCES = CONFIG / 'settings.json'
FIELDS = ('real_fps', 'presented_fps', 'generated_fps', 'simulation_fps', 'low_1',
          'cpu', 'gpu', 'ram', 'vram', 'temperatures', 'disk', 'network', 'chart')
DEFAULTS = dict(visible=list(FIELDS), interval=2, duration=30, delay=5, video=False,
                generation='unknown', profile='', capture='auto', report_dir=str(DATA / 'benchmarks'))


def read_json(path):
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, encoding='utf-8', delete=False) as stream:
        stage = Path(stream.name)
        try:
            json.dump(data, stream, allow_nan=False)
            stream.close()
            stage.replace(path)
        finally:
            stage.unlink(missing_ok=True)


def preferences():
    return validate({**DEFAULTS, **read_json(PREFERENCES)})


def validate(values):
    result = DEFAULTS.copy()
    visible = values.get('visible')
    if isinstance(visible, list):
        result['visible'] = [field for field in FIELDS if field in visible]
    for key, choices in dict(generation=('unknown', 'off', 'lsfg', 'in-game'),
                             capture=('auto', 'vulkan', 'mangohud')).items():
        if values.get(key) in choices:
            result[key] = values[key]
    for key, low, high in (('interval', 1, 10), ('duration', 5, 1800), ('delay', 0, 30)):
        value = values.get(key)
        if type(value) is int and low <= value <= high:
            result[key] = value
    for key in ('profile', 'report_dir'):
        if isinstance(values.get(key), str) and (values[key].strip() or key == 'profile'):
            result[key] = values[key].strip()
    result['video'] = values.get('video') is True
    return result


def reports(settings=None):
    return Path((settings or preferences())['report_dir']).expanduser().absolute()
