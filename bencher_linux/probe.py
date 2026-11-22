"""Build and enable optional native capture libraries for one launch."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .config import CACHE, CONFIG

NATIVE = Path(__file__).with_name('native')
LIBRARIES = CACHE / 'probes'


def libraries():
    # The environment override is useful for distribution packages.
    return Path(os.environ.get('BENCHER_PROBE_DIR', str(LIBRARIES)))


def build(headers=None):
    output = libraries()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    commands = (
        ('libBencherGL.so', ['cc', '-std=c11', '-O2', '-shared', '-fPIC',
                            str(NATIVE / 'gl.c'), '-ldl', '-pthread']),
        ('libBencherFrameProbe.so', ['c++', '-std=c++17', '-O2', '-shared', '-fPIC',
                                    '-fvisibility=hidden', '-fvisibility-inlines-hidden', '-pthread',
                                    *(['-I', str(headers)] if headers else []), str(NATIVE / 'vulkan.cpp')]),
    )
    for name, command in commands:
        with tempfile.TemporaryDirectory(dir=output) as folder:
            staged = Path(folder) / name
            subprocess.run([*command, '-o', str(staged)], check=True)
            staged.replace(output / name)
    return output


def lsfg_manifest():
    base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    data_dirs = os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share'
    roots = [CONFIG.parent, base, *(Path(folder) for folder in data_dirs.split(':') if folder)]
    for root in roots:
        for candidate in sorted((root / 'vulkan/implicit_layer.d').glob('*.json')):
            if 'lsfg' not in candidate.name.casefold():
                continue
            try:
                layer = json.loads(candidate.read_text()).get('layer', {})
                name = layer.get('name', '').upper()
                if ('LSFGVK' in name and not name.endswith(('_X86', '_I686'))
                        and str(layer.get('library_arch', '64')) != '32'):
                    return candidate
            except (ValueError, OSError, AttributeError):
                continue
    return None


def add_vulkan(directory, env, with_lsfg):
    library = libraries() / 'libBencherFrameProbe.so'
    if not library.is_file():
        return False
    manifest = lsfg_manifest() if with_lsfg else None
    stages = [('game', 'bencherGameProbe')]
    if manifest:
        stages.extend([('lsfg', None), ('output', 'bencherOutputProbe')])
    paths = []
    for stage, entry in stages:
        folder = directory / stage
        folder.mkdir()
        if entry:
            description = {'file_format_version': '1.2.1', 'layer': {
                'name': f'VK_LAYER_BENCHER_frame_{stage}', 'type': 'GLOBAL',
                'library_path': str(library.resolve()), 'library_arch': '64',
                'api_version': '1.3.0', 'implementation_version': '1',
                'description': 'Bencher Linux frame timings',
                'enable_environment': {'BENCHER_FRAME_CAPTURE': '1'},
                'disable_environment': {'BENCHER_FRAME_OFF': '1'},
                'functions': {'vkNegotiateLoaderLayerInterfaceVersion': entry},
            }}
        else:
            description = json.loads(manifest.read_text())
            layer = description['layer']
            # Loader search names (libfoo.so) retain their normal lookup rules.
            path = layer['library_path']
            if '/' in path and not Path(path).is_absolute():
                layer['library_path'] = str((manifest.parent / path).resolve())
        (folder / 'layer.json').write_text(json.dumps(description))
        paths.append(str(folder))
    if existing := env.get('VK_ADD_IMPLICIT_LAYER_PATH'):
        paths.append(existing)
    env.update(BENCHER_FRAME_CAPTURE='1', VK_ADD_IMPLICIT_LAYER_PATH=':'.join(paths))
    return True


def executable_name(command):
    for token in command:
        if token.lower().endswith('.exe'):
            return token.replace('\\', '/').rsplit('/', 1)[-1]
    if 'gamescope' in [Path(token).name for token in command] and '--' in command:
        command = command[command.index('--') + 1:]
    for token in command:
        if '=' in token.split('/')[-1] and not token.startswith('/'):
            continue
        if Path(token).name not in ('env', 'gamemoderun', 'mangohud'):
            return Path(token).name
    raise ValueError('Specify the executable with --target')
