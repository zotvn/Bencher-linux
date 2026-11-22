"""Record the selected Gamescope video source on the benchmark's monotonic clock."""

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .config import CACHE


def video_source(pid):
    objects = json.loads(subprocess.check_output(['pw-dump'], timeout=3))
    props = {item['id']: item.get('info', {}).get('props', {}) for item in objects}
    for item in objects:
        node = props[item['id']]
        client = props.get(node.get('client.id'), {})
        if (node.get('node.name') == 'gamescope' and node.get('media.class') == 'Video/Source'
                and str(client.get('pipewire.sec.pid')) == str(pid)):
            return int(node['object.serial'])
    raise RuntimeError('This game has no Gamescope video source')


def scrub_previews(path, frame_times):
    columns = rows = 8
    expected = math.ceil(len(frame_times) / (columns * rows))
    destination = path.with_name('previews')
    if not destination.exists():
        with tempfile.TemporaryDirectory(prefix='.scrub-', dir=path.parent) as temp:
            sheets = Path(temp) / 'previews'
            sheets.mkdir()
            subprocess.run([
                'nice', '-n', '10', 'ffmpeg', '-nostdin', '-v', 'error', '-threads', '2',
                '-filter_threads', '2', '-i', str(path), '-an',
                '-vf', ('scale=160:90:force_original_aspect_ratio=decrease,'
                        f'pad=160:90:(ow-iw)/2:(oh-ih)/2,setsar=1,tile={columns}x{rows}'),
                '-fps_mode', 'passthrough', '-threads', '2', '-q:v', '5', str(sheets / '%04d.jpg'),
            ], check=True, capture_output=True, timeout=max(30, min(240, frame_times[-1])))
            if len(list(sheets.glob('*.jpg'))) != expected:
                raise RuntimeError('Scrub preview count does not match the recording')
            sheets.rename(destination)
    if len(list(destination.glob('*.jpg'))) != expected:
        raise RuntimeError('Scrub preview count does not match the recording')
    return dict(columns=columns, rows=rows, sheets=expected)


def prepare():
    cache = CACHE
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('GST_REGISTRY', str(cache / 'benchmark-gstreamer.bin'))
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst
    Gst.init(None)
    return Gst


class Video:
    def __init__(self, Gst, serial, path):
        from gi.repository import GLib
        self.gst = Gst
        self.bounds = None
        self.first = self.last = None
        self.finalized = False
        if Gst.ElementFactory.find('nvh264enc'):
            self.encoder = 'NVIDIA NVENC'
            limit = 165
            encode = ('nvh264enc preset=p2 tune=low-latency rc-mode=vbr bitrate=30000 '
                      'max-bitrate=48000 bframes=0 gop-size=90 zerolatency=true')
            convert = ''
        else:
            self.encoder = 'Software H.264 (720p, up to 60 FPS)'
            limit = 60
            encode = 'x264enc speed-preset=ultrafast tune=zerolatency threads=2 bitrate=14000 key-int-max=60'
            convert = 'videoconvertscale ! video/x-raw,format=I420,width=1280,height=720 ! '
        self.minimum_interval = 1_000_000_000 // limit - 200_000
        try:
            self.pipeline = Gst.parse_launch(
                f'pipewiresrc target-object={serial} always-copy=true do-timestamp=true name=game ! '
                'video/x-raw,format=BGRx ! queue max-size-buffers=3 max-size-bytes=0 '
                f'max-size-time=0 leaky=downstream name=frames ! {convert}'
                f'{encode} ! video/x-h264,profile=high ! h264parse ! '
                'mp4mux trak-timescale=90000 faststart=true ! filesink name=output')
        except GLib.Error as error:
            raise RuntimeError(f'Could not prepare the video encoder ({error.message})') from error
        self.pipeline.get_by_name('output').set_property('location', str(path))
        source = self.pipeline.get_by_name('game').get_static_pad('src')
        source.add_probe(Gst.PadProbeType.BUFFER, self.timestamp)
        frame_output = self.pipeline.get_by_name('frames').get_static_pad('src')
        frame_output.add_probe(Gst.PadProbeType.BUFFER, self.normalize)
        self.bus = self.pipeline.get_bus()
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError('Could not start video recording')

    def timestamp(self, _pad, info):
        Gst = self.gst
        now = time.monotonic_ns()
        if self.bounds is None:
            return Gst.PadProbeReturn.DROP
        begin, end = self.bounds
        if now < begin or now >= end or (
                self.last is not None and now - self.last < self.minimum_interval):
            return Gst.PadProbeReturn.DROP
        packet = info.get_buffer()
        packet.pts = now - begin
        packet.dts = Gst.CLOCK_TIME_NONE
        packet.duration = Gst.CLOCK_TIME_NONE
        self.last = now
        return Gst.PadProbeReturn.OK

    def normalize(self, _pad, info):
        packet = info.get_buffer()
        begin = self.bounds[0]
        if self.first is None:
            self.first = packet.pts + begin
        packet.pts -= self.first - begin
        return self.gst.PadProbeReturn.OK

    def check(self):
        message = self.bus.pop_filtered(self.gst.MessageType.ERROR)
        if message:
            error, _debug = message.parse_error()
            raise RuntimeError(f'Video recording failed ({error.message})')

    def finish(self, path):
        Gst = self.gst
        try:
            self.check()
            if self.first is None:
                raise RuntimeError('Gamescope returned no video frames')
            self.pipeline.send_event(Gst.Event.new_eos())
            message = self.bus.timed_pop_filtered(30 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if not message or message.type != Gst.MessageType.EOS:
                raise RuntimeError('Could not finalize the video recording')
            self.finalized = True
        finally:
            self.close()
        timing = json.loads(subprocess.check_output([
            'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_packets',
            '-show_entries', 'packet=pts_time', '-of', 'json', str(path)
        ], timeout=90))
        frames = sorted(float(packet['pts_time']) for packet in timing['packets'] if 'pts_time' in packet)
        if not frames:
            raise RuntimeError('The saved video has no decodable frames')
        video_zero = frames[0]
        return dict(file=path.name, encoder=self.encoder, offset_s=(self.first - self.bounds[0]) / 1e9,
                    frame_times_s=[round(t - video_zero, 6) for t in frames])

    def close(self):
        self.pipeline.set_state(self.gst.State.NULL)
