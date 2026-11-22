"""Read this game's presentation intervals from its Gamescope instance."""
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time

from .frame_stream import RECORD
from .config import RUNTIME


HEADER = struct.Struct('=II')
FRAME = struct.Struct('=III')
CONTROL = b'gamescope_control\0'


class GamescopeFrames:
    def __init__(self, connection, app_id):
        self.connection = connection
        self.app_id = app_id
        self.buffer = bytearray()

    def send(self, object_id, opcode, payload):
        header = HEADER.pack(object_id, ((8 + len(payload)) << 16) | opcode)
        self.connection.sendall(header + payload)

    def receive(self):
        while True:
            if len(self.buffer) >= HEADER.size:
                object_id, flags = HEADER.unpack_from(self.buffer)
                size = flags >> 16
                if size < HEADER.size or size % 4:
                    raise ValueError('Invalid Wayland message size')
                if len(self.buffer) >= size:
                    payload = self.buffer[HEADER.size:size]
                    del self.buffer[:size]
                    opcode = flags & 0xffff
                    if object_id == 1 and opcode == 0:
                        raise OSError('Gamescope rejected the Wayland request')
                    return object_id, opcode, payload
            chunk = self.connection.recv(4096)
            if not chunk:
                raise ConnectionError('Gamescope closed the Wayland socket')
            self.buffer.extend(chunk)

    def bind(self):
        # wl_display.get_registry -> object 2, wl_display.sync -> callback 3.
        self.send(1, 1, struct.pack('=I', 2))
        self.send(1, 0, struct.pack('=I', 3))
        while True:
            object_id, event, payload = self.receive()
            if object_id == 3:
                return False
            if object_id != 2 or event != 0:
                continue
            _, length = HEADER.unpack_from(payload)
            if payload[8:8 + length] != CONTROL:
                continue
            padded_length = (length + 3) & ~3
            version, = struct.unpack_from('=I', payload, 8 + padded_length)
            if version < 6:
                return False
            # wl_registry.bind; Gamescope performance events were added in v6.
            self.send(2, 0, payload[:8 + padded_length] + HEADER.pack(6, 4))
            return True

    def request_frame(self):
        self.send(4, 6, struct.pack('=I', self.app_id))

    def next_frame(self):
        while True:
            try:
                object_id, event, payload = self.receive()
            except TimeoutError:
                return None
            if object_id == 4 and event == 3:
                app_id, low, high = FRAME.unpack(payload)
                if app_id != self.app_id:
                    continue
                self.request_frame()
                return ((high << 32) | low) / 1_000_000


def game_process(executable, config):
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal():
            continue
        try:
            argv0 = (process / 'cmdline').read_bytes().partition(b'\0')[0].decode(errors='replace')
            if Path(argv0.replace('\\', '/')).name.casefold() != executable.casefold():
                continue
            environment = dict(entry.split('=', 1) for entry in
                               (process / 'environ').read_bytes().decode(errors='replace').split('\0')
                               if '=' in entry)
        except OSError:
            continue
        if environment.get('BENCHER_SESSION') == config and environment.get('GAMESCOPE_WAYLAND_DISPLAY'):
            return process, environment
    return None


def gamescope_window_id(process, environment):
    if not environment.get('DISPLAY'):
        return None
    xenv = dict(os.environ, DISPLAY=environment['DISPLAY'], LC_ALL='C')
    if environment.get('XAUTHORITY'):
        xenv['XAUTHORITY'] = str(process / 'root' / environment['XAUTHORITY'].lstrip('/'))
    result = subprocess.run(['xprop', '-root', 'GAMESCOPE_FOCUSABLE_WINDOWS'], env=xenv,
                            capture_output=True, text=True, timeout=1, check=True)
    _, separator, values = result.stdout.partition(' = ')
    if not separator:
        return None
    entries = [int(value.strip()) for value in values.split(',') if value.strip()]
    # The root property lists triples of X window, Gamescope app ID and PID.
    for window_id, app_id, pid in zip(entries[::3], entries[1::3], entries[2::3]):
        if window_id and app_id and pid == int(process.name):
            return app_id
    return None


def collect_frames(launcher, executable, config):
    launcher_process = Path(f'/proc/{launcher}')
    running = True

    def stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def active():
        return running and launcher_process.exists()

    while active():
        game = game_process(executable, config)
        if game:
            process, environment = game
            try:
                app_id = gamescope_window_id(process, environment)
                if app_id:
                    supported = listen(process, environment, app_id, active)
                    if not supported:
                        return
            except (OSError, ValueError, struct.error, subprocess.SubprocessError):
                pass
        if active():
            time.sleep(1)


def listen(process, environment, app_id, active):
    display = Path(environment['GAMESCOPE_WAYLAND_DISPLAY'])
    if not display.is_absolute():
        display = Path(environment.get('XDG_RUNTIME_DIR', RUNTIME.parent)) / display
    path = process / 'root' / str(display).lstrip('/')
    stream_path = Path(environment['BENCHER_SESSION']) / 'presented-cadence.bin'
    with stream_path.open('ab', buffering=0) as stream, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.35)
        connection.connect(str(path))
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        peer, uid, _ = struct.unpack('=iii', credentials)
        if uid != os.getuid() or not Path(f'/proc/{peer}/comm').read_text().startswith('gamescope'):
            return False
        frames = GamescopeFrames(connection, app_id)
        if not frames.bind():
            return False
        # Gamescope 3.17 only sends app performance events in this timing mode.
        subprocess.run(['gamescopectl', 'mangoapp_use_output_timing', 'false'],
                       env=dict(os.environ, GAMESCOPE_WAYLAND_DISPLAY=str(path)),
                       timeout=2, check=True, capture_output=True)
        frames.request_frame()
        previous = None
        check = 0
        while active() and process.exists():
            sample = frames.next_frame()
            stamp = time.monotonic_ns()
            if sample is not None:
                if previous is not None and stamp > previous:
                    stream.write(RECORD.pack(stamp, (stamp - previous) / 1e6))
                previous = stamp
            if stamp >= check and (previous is None or stamp - previous > 4_000_000_000):
                check = stamp + 1_000_000_000
                if gamescope_window_id(process, environment) != app_id:
                    break
    return True


if __name__ == '__main__':
    launcher, executable, config = sys.argv[1:]
    collect_frames(int(launcher), executable, config)
