"""Game launch and isolated capture lifecycle."""

import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from .config import RUNTIME, atomic, read_json
from .probe import add_vulkan, executable_name, libraries, lsfg_manifest
from .telemetry import FrameReader

POLL_SECONDS = 1


def capture_sessions(now=None):
    now = now if now is not None else time.time()
    sessions = []
    for path in RUNTIME.glob('session-*.json'):
        record = read_json(path)
        if not record:
            continue
        if now - record.get('updated', 0) > 5:
            record['running'] = False
        sessions.append(record)
    return sorted(sessions, key=lambda session: (session['running'], session['started']), reverse=True)


def control(prefix):
    try:
        lines = Path('/proc/net/unix').read_text().splitlines()
    except OSError:
        return
    for line in lines:
        address = line.split()[-1]
        if not address.startswith('@' + prefix + '-'):
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(.05)
                client.connect('\0' + address[1:])
                client.sendall(b':logging=1;')
        except OSError:
            continue


def game_environment(command, settings, directory, target):
    env = dict(os.environ, BENCHER_SESSION=str(directory),
               BENCHER_FRAME_DIR=str(directory), BENCHER_FRAME_TARGET=target)
    generation = settings['generation']
    if generation == 'lsfg':
        if not lsfg_manifest():
            raise ValueError('LSFG-VK is enabled. Install it first or change Frame generation in Settings.')
        env.pop('DISABLE_LSFGVK', None)
        if settings['profile']:
            env['LSFGVK_PROFILE'] = settings['profile']
    else:
        env['DISABLE_LSFGVK'] = '1'

    mode = settings['capture']
    if mode != 'mangohud':
        vulkan = add_vulkan(directory, env, generation == 'lsfg')
        opengl = libraries() / 'libBencherGL.so'
        if mode == 'auto' and opengl.is_file():
            env['LD_PRELOAD'] = ':'.join(filter(None, (str(opengl), env.get('LD_PRELOAD'))))
        if mode == 'vulkan' and not vulkan:
            raise ValueError('Vulkan capture is selected. Run bencher build-probe first.')
        if vulkan or (mode == 'auto' and opengl.is_file()):
            return command, env, False
    if not shutil.which('mangohud'):
        raise ValueError('Install MangoHud or run bencher build-probe for native frame timing.')
    prefix = f'bencher-{os.getpid()}'
    config = directory / 'MangoHud.conf'
    config.write_text(f'no_display\nfps\nframetime\ncontrol={prefix}-%p\n'
                      f'output_folder={directory}\nlog_interval=0\nlog_duration=10\nautostart_log=1\n')
    env.pop('MANGOHUD_CONFIG', None)
    env['MANGOHUD_CONFIGFILE'] = str(config)
    return ['mangohud', *command], env, True


def run(command, settings, target=None):
    if not command:
        raise ValueError('Usage: bencher run -- GAME [ARGS...]')
    target = target or executable_name(command)
    # /tmp is shared with the Steam Linux Runtime for native Steam installs.
    with tempfile.TemporaryDirectory(prefix='bencher-linux-', dir='/tmp') as capture:
        return launch(command, settings, Path(capture), target)


def launch(command, settings, directory, target):
    ident = str(os.getpid())
    script, env, mango = game_environment(command, settings, directory, target)
    gamescope = any(Path(arg).name == 'gamescope' for arg in command) and '--' in command
    started = time.time()
    state = dict(id=ident, title=target, started=started, running=True, updated=started,
                 capture_path=str(directory), generation=settings['generation'], profile=env.get('LSFGVK_PROFILE', ''),
                 gamescope=gamescope, mango=mango, video_available=gamescope and shutil.which('pw-dump') is not None)
    path = RUNTIME / f'session-{ident}.json'
    atomic(path, state)
    child = None
    collector = None
    reader = FrameReader()
    old_handlers = {}

    def forward(signum, _frame):
        if child and child.poll() is None:
            child.send_signal(signum)

    try:
        child = subprocess.Popen(script, env=env)
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, forward)

        if gamescope and shutil.which('gamescopectl') and shutil.which('xprop'):
            collector = subprocess.Popen([sys.executable, '-m', 'bencher_linux.gamescope_frames',
                                          ident, target, str(directory)])
        while child.poll() is None:
            state['updated'] = time.time()
            if mango:
                control(f'bencher-{ident}')
                try:
                    state['mango_stats'] = reader.poll(directory, target)
                except FileNotFoundError:
                    pass
            reports = [(file, read_json(file)) for file in directory.glob('submit-*.json')]
            if reports:
                try:
                    file, data = max(reports, key=lambda entry: entry[1].get('rendered_frames', 0))
                    state['reflex'] = {**data, 'at': file.stat().st_mtime}
                except FileNotFoundError:
                    pass
            atomic(path, state)
            try:
                child.wait(timeout=POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        return child.returncode
    finally:
        if collector and collector.poll() is None:
            collector.terminate()
            try:
                collector.wait(timeout=3)
            except subprocess.TimeoutExpired:
                collector.kill()
                collector.wait()
        if child and child.poll() is None:
            child.terminate()
        state.update(running=False, updated=time.time())
        atomic(path, state)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
