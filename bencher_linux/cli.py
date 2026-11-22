"""Bencher's desktop and command-line entry points."""

import argparse
import importlib
import shutil
import subprocess
import sys

from . import benchmark, probe, session
from .config import preferences


def available_module(name):
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def doctor():
    directory = probe.libraries()
    checks = {
        'Vulkan capture': (directory / 'libBencherFrameProbe.so').is_file(),
        'OpenGL capture': (directory / 'libBencherGL.so').is_file(),
        'MangoHud fallback': bool(shutil.which('mangohud')),
        'LSFG-VK layer': probe.lsfg_manifest() is not None,
        'Gamescope timing': all(shutil.which(x) for x in ('gamescope', 'gamescopectl', 'xprop')),
        'Video tools': all(shutil.which(x) for x in ('pw-dump', 'ffprobe', 'ffmpeg'))
                       and available_module('gi'),
        'Desktop interface': available_module('tkinter'),
    }
    for name, installed in checks.items():
        print(f'{name:24} {"ready" if installed else "unavailable"}')
    if not checks['Vulkan capture']:
        print('Build native capture with bencher build-probe (requires a compiler and Vulkan headers).')
    if not any(checks[key] for key in ('Vulkan capture', 'OpenGL capture', 'MangoHud fallback')):
        return 1
    return 0


def parse(argv):
    parser = argparse.ArgumentParser(prog='bencher', description='Linux gaming benchmarks')
    parser.add_argument('--version', action='version', version='Bencher Linux 0.1.1')
    commands = parser.add_subparsers(dest='action')
    commands.add_parser('gui', help='Open Live View and settings')
    commands.add_parser('doctor', help='Check which capture paths are ready')
    build = commands.add_parser('build-probe', help='Compile the native timing layers')
    build.add_argument('--headers', help='Extra Vulkan headers include directory')
    game = commands.add_parser('run', help='Launch a game with telemetry')
    game.add_argument('--target', help='Executable basename for games started by a launcher')
    game.add_argument('--generation', choices=('unknown', 'off', 'lsfg', 'in-game'))
    game.add_argument('--gamescope', action='store_true', help='Launch inside nested Gamescope')
    game.add_argument('command', nargs=argparse.REMAINDER)
    record = commands.add_parser('record', help='Record a timed benchmark')
    record.add_argument('--session')
    record.add_argument('--seconds', type=int)
    record.add_argument('--delay', type=int)
    record.add_argument('--video', action=argparse.BooleanOptionalAction, default=None)
    commands.add_parser('stop', help='Stop or cancel the current recording')
    commands.add_parser('sessions', help='List active game sessions')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse(argv)
    settings = preferences()
    try:
        if args.action in (None, 'gui'):
            from .gui import main as open_window
            open_window()
        elif args.action == 'doctor':
            return doctor()
        elif args.action == 'build-probe':
            print(probe.build(args.headers))
        elif args.action == 'run':
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
            if not command:
                raise ValueError('Supply a game command after --')
            if args.generation:
                settings['generation'] = args.generation
            if args.gamescope:
                command = ['gamescope', '-b', '--', *command]
            return session.run(command, settings, args.target)
        elif args.action == 'sessions':
            for game in session.capture_sessions():
                if game['running']:
                    print(game['id'], game['title'])
        elif args.action == 'stop':
            benchmark.stop()
        elif args.action == 'record':
            duration = settings['duration'] if args.seconds is None else args.seconds
            delay = settings['delay'] if args.delay is None else args.delay
            if not 5 <= duration <= 1800 or not 0 <= delay <= 30:
                raise ValueError('Use 5 to 1800 seconds and a delay of 0 to 30 seconds')
            ident = args.session or next((s['id'] for s in session.capture_sessions() if s['running']), '')
            result = benchmark.record(ident, duration, delay, settings['video'] if args.video is None else args.video, settings)
            if result:
                print(result)
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        print(f'Bencher: {error}', file=sys.stderr)
        return 1
    return 0
