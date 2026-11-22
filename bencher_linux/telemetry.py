#!/usr/bin/env python3
"""Hardware telemetry and incremental, frame-based MangoHud statistics."""
import csv
import heapq
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time
from collections import deque
from itertools import islice, repeat

from .config import RUNTIME, atomic


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def frame_stats(frames, *, include_low=True):
    valid = [v for v in frames if v > 0 and math.isfinite(v)]
    if not valid:
        return {}
    mean = sum(valid) / len(valid)
    result = dict(avg_fps=1000/mean, max_fps=1000/min(valid), min_fps=1000/max(valid),
                  avg_ms=mean, max_ms=max(valid), samples=len(valid))
    if include_low:
        count = (len(valid) + 99) // 100
        result['low_1'] = 1000 * count / sum(heapq.nlargest(count, valid))
    return result



class RollingLows:
    seconds = 300

    def __init__(self):
        self.batches = deque()
        self.count = 0

    def update(self, now, durations):
        while self.batches and self.batches[0][0] <= now - self.seconds:
            self.count -= len(self.batches.popleft()[1])
        if durations:
            batch = sorted(durations, reverse=True)
            self.batches.append((now, batch))
            self.count += len(batch)
        if self.count < 100:
            return None
        # Each batch is sorted once. Merge only as far as the slowest 1%.
        slowest = heapq.merge(*(batch for _, batch in self.batches), reverse=True)
        tail_count = (self.count + 99) // 100
        return 1000 * tail_count / sum(islice(slowest, tail_count))


class FrameReader:
    def __init__(self):
        self.lows = RollingLows()
        self.low_1 = None
        self.offsets = {}
        self.columns = {}
        self.frames = deque(maxlen=120000)
        self.sample_seconds = deque(maxlen=self.frames.maxlen)
        self.latest = {}
        self.last_frame = 0
        self.tail = deque(maxlen=180)
        self.intervals = deque(maxlen=180)
        self.peak_fps = 0
        self.stutters = 0
        self.stutter_peaks = deque(maxlen=180)
        self.frame_baseline = 0

    def poll(self, directory, executable=None):
        fresh=[]
        for path in sorted(directory.glob('*.csv')):
            if path.name.endswith('_summary.csv'):
                continue
            if executable and not path.name.casefold().startswith(
                (executable.casefold() + '_', Path(executable).stem.casefold() + '_')
            ):
                continue
            offset = self.offsets.get(path, 0)
            with path.open() as stream:
                stream.seek(offset)
                while True:
                    position=stream.tell()
                    line=stream.readline()
                    if not line or not line.endswith('\n'):
                        self.offsets[path]=position
                        break
                    row=next(csv.reader([line]))
                    if 'fps' in row and 'frametime' in row:
                        self.columns[path]=row
                        continue
                    columns=self.columns.get(path)
                    if not columns or len(row)!=len(columns):continue
                    record=dict(zip(columns,row))
                    value=number(record.get('frametime'))
                    if value and 0 < value < 60000:
                        fresh.append(value)
                        self.latest={key:number(val) for key,val in record.items()}
        self.record(fresh)
        return self.stats()

    def record(self, frames, elapsed=None):
        fresh = [value for value in frames if 0 < value < 60000]
        self.low_1 = self.lows.update(time.monotonic(), fresh)
        if fresh:
            self.frames.extend(fresh)
            self.last_frame = time.time()
            baseline = statistics.median(fresh)
            threshold = max(33.33, 2.5 * (self.frame_baseline or baseline))
            hitches = [value for value in fresh if value >= threshold]
            self.stutters += len(hitches)
            self.stutter_peaks.append(max(hitches, default=None))
            self.frame_baseline = baseline
            self.tail.append(sum(fresh)/len(fresh))
            if elapsed and elapsed > 0:
                self.sample_seconds.extend(repeat(elapsed / len(fresh), len(fresh)))
                measured = len(fresh) / elapsed
            else:
                measured = 1000 * len(fresh) / sum(fresh)
            self.intervals.append(measured)
            self.peak_fps = max(self.peak_fps, self.intervals[-1])

    def stats(self):
        result = frame_stats(self.frames, include_low=False)
        if result:
            if self.sample_seconds:
                result['avg_fps'] = len(self.sample_seconds) / sum(self.sample_seconds)
            result.update(low_1=self.low_1, low_window_seconds=self.lows.seconds, fps=self.intervals[-1] if self.intervals else None,
                          max_fps=self.peak_fps if self.intervals else None,
                          frame_ms=self.tail[-1] if self.tail else None,
                          age=time.time()-self.last_frame, graph=list(self.tail),
                          stutters=self.stutters, stutter_graph=list(self.stutter_peaks),
                          cpu_render_ms=None, gpu_render_ms=None)
        return result


class Hardware:
    def __init__(self):
        self.cpu_prev = None
        self.net_prev = None
        self.disk_prev = None
        self.time_prev = time.monotonic()
        self.history = deque(maxlen=120)
        self.nvidia = shutil.which("nvidia-smi")
        self.cpu_name = next((x.split(':',1)[1].strip() for x in Path('/proc/cpuinfo').read_text().splitlines()
                              if x.startswith('model name')), 'CPU')

    def sample(self):
        now = time.monotonic()
        dt = max(.01, now - self.time_prev)
        self.time_prev = now
        cpus = []
        cores = []
        for line in Path('/proc/stat').read_text().splitlines():
            if not line.startswith('cpu'): break
            values = [int(x) for x in line.split()[1:9]]
            cpus.append((sum(values), values[3]+values[4]))
        for index, (total, idle) in enumerate(cpus):
            prev = self.cpu_prev[index] if self.cpu_prev and index < len(self.cpu_prev) else (total,idle)
            delta = total-prev[0]
            cores.append(max(0,min(100,100*(1-(idle-prev[1])/delta))) if delta else 0)
        self.cpu_prev = cpus
        mem = {line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
        gib = 1024**2
        result = dict(ts=time.time(), cpu=cores[0], cores=cores[1:], cpu_name=self.cpu_name,
                      ram_used=(mem['MemTotal']-mem['MemAvailable'])/gib, ram_total=mem['MemTotal']/gib,
                      swap_used=(mem['SwapTotal']-mem['SwapFree'])/gib, swap_total=mem['SwapTotal']/gib,
                      cpu_temp=None, cpu_mhz=None, gpu=None, gpu_name='GPU',gpu_temp=None,
                      gpu_power=None,gpu_limit=None,gpu_clock=None,gpu_mem_clock=None,fan=None,
                      vram_used=None,vram_total=None)
        mhz = [float(line.split(':')[1]) for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('cpu MHz')]
        if mhz: result['cpu_mhz'] = sum(mhz)/len(mhz)
        for hwmon in Path('/sys/class/hwmon').glob('hwmon*'):
            try:
                if hwmon.joinpath('name').read_text().strip() in ('coretemp','k10temp','zenpower'):
                    temps = [int(p.read_text())/1000 for p in hwmon.glob('temp*_input')]
                    if temps:result['cpu_temp']=max(temps)
            except (OSError,ValueError):pass
        try:
            query='name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,clocks.gr,clocks.mem,fan.speed'
            arguments = ['--query-gpu=' + query, '--format=csv,noheader,nounits']
            binary = self.nvidia
            if not binary:
                raise FileNotFoundError
            gpu = subprocess.run([binary, *arguments], text=True, capture_output=True, timeout=1.5)
            gpu.check_returncode()
            data = gpu.stdout.splitlines()[0]
            fields=next(csv.reader([data]))
            result['gpu_name']=fields[0].strip()
            for key,value in zip(('gpu','vram_used','vram_total','gpu_temp','gpu_power','gpu_limit','gpu_clock','gpu_mem_clock','fan'),fields[1:]):
                result[key]=number(value)
            for key in ('vram_used','vram_total'):
                if result[key] is not None: result[key]/=1024
        except (OSError,subprocess.SubprocessError,IndexError):pass
        if result['gpu'] is None:
            apply_linux_gpu(result)
        # Physical network devices only, to avoid counting VPN packets twice.
        rx=tx=0
        for interface in Path('/sys/class/net').iterdir():
            if not (interface/'device').exists():continue
            try:
                rx+=int((interface/'statistics/rx_bytes').read_text())
                tx+=int((interface/'statistics/tx_bytes').read_text())
            except OSError:pass
        result['download']=max(0,(rx-self.net_prev[0])/dt) if self.net_prev else 0
        result['upload']=max(0,(tx-self.net_prev[1])/dt) if self.net_prev else 0
        self.net_prev=(rx,tx)
        read=write=0
        for line in Path('/proc/diskstats').read_text().splitlines():
            parts=line.split();name=parts[2]
            if not (Path('/sys/block')/name/'device').exists():continue
            read+=int(parts[5])*512;write+=int(parts[9])*512
        result['disk_read']=max(0,(read-self.disk_prev[0])/dt) if self.disk_prev else 0
        result['disk_write']=max(0,(write-self.disk_prev[1])/dt) if self.disk_prev else 0
        self.disk_prev=(read,write)
        self.history.append(dict(cpu=result['cpu'],gpu=result['gpu'],
                                 ram=result['ram_used']/result['ram_total']*100,
                                 vram=(result['vram_used']/result['vram_total']*100 if result['vram_total'] else None)))
        result['history']=list(self.history)
        return result


def apply_linux_gpu(result, drm=Path('/sys/class/drm')):
    """Read amdgpu and other DRM driver counters when published."""
    for entry in sorted(drm.glob('card[0-9]*')):
        if not entry.name[4:].isdigit() or not (device := entry / 'device').is_dir():
            continue
        def read(path):
            try:
                return path.read_text().strip()
            except OSError:
                return None

        vendors = {'0x1002': 'AMD', '0x8086': 'Intel', '0x10de': 'NVIDIA'}
        vendor = vendors.get(read(device / 'vendor'))
        if not vendor:
            continue
        result['gpu_name'] = f'{vendor} GPU'
        for filename, field, divisor in (
            ('gpu_busy_percent', 'gpu', 1), ('mem_info_vram_used', 'vram_used', 2**30),
            ('mem_info_vram_total', 'vram_total', 2**30),
        ):
            value = number(read(device / filename))
            if value is not None:
                result[field] = value / divisor
        for sensor in (device / 'hwmon').glob('hwmon*/temp1_input'):
            value = number(read(sensor))
            if value is not None:
                result['gpu_temp'] = value / 1000
                break
        if vendor == 'AMD' or result['gpu'] is not None:
            return
