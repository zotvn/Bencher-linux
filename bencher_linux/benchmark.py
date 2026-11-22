"""Timed benchmarks and local reports on every supported desktop."""
from collections import deque
import csv
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import tempfile
import time

from .config import RUNTIME, atomic, read_json, reports
from .frame_stream import FrameStream, SECOND, real_path
from .session import capture_sessions
from .telemetry import Hardware, frame_stats

STATUS = RUNTIME / 'benchmark.json'
STOP = RUNTIME / 'benchmark-stop'

def benchmark_frames(stream, start, end, *, real=False):
    rows = []
    if stream is not None:
        for stamp, ms, generated in stream.read():
            if 0 < ms < 60000 and stamp < end and stamp - round(ms * 1e6) >= start:
                row = [round((stamp - start) / SECOND, 9), ms]
                if real:
                    row.append(generated)
                rows.append(row)
    return rows


def source_frames(path, start, *, real=False):
    return FrameStream(path, start, rendered=real) if path and path.is_file() else None


def frame_summary(events, duration):
    intervals = [row[1] for row in events]
    result = frame_stats(intervals, include_low=False)
    if result:
        result['avg_fps'] = len(intervals) / duration
        # The slowest 1% boundary is the interval at the first percentile of FPS.
        result['low_1'] = 1000 / sorted(intervals)[-((len(intervals) + 99) // 100)] if len(intervals) >= 100 else None
    return result


def summarize(events, duration):
    previous = deque(maxlen=120)
    for event in events:
        limit = max(33.33, 2.5 * statistics.median(previous)) if previous else 33.33
        event.append(int(event[1] >= limit))
        previous.append(event[1])
    if not events:
        return {}
    start = max(0, events[0][0] - events[0][1] / 1000)
    end = min(duration, events[-1][0])
    result = frame_summary(events, max(.001, end - start))
    if result:
        result.update(observed_from_s=start, observed_until_s=end)
        result['stutters'] = sum(row[2] for row in events)
        bins = [0] * max(1, int(duration))
        for event in events:
            second = int(event[0])
            if second < len(bins):
                bins[second] += 1
        result['max_fps'] = max(bins)
    return result


def write_report(directory, report):
    metadata = {key: report[key] for key in ('title', 'started', 'mode', 'duration_s')}
    atomic(directory / 'run.json', metadata)
    (directory / 'telemetry.json').write_text(json.dumps(report, ensure_ascii=False, allow_nan=False))
    with (directory / 'statistics.csv').open('w', newline='') as stream:
        excluded = {'history', 'cores', 'graph', 'stutter_graph', 'render_updated', 'age'}
        rows = [dict(seconds=sample['t'], source=sample['source'],
                     lsfg_multiplier=sample['lsfg'].get('multiplier'),
                     lsfg_observed=sample['lsfg'].get('observed'),
                     lsfg_auto=sample['lsfg'].get('auto'),
                     lsfg_reason=sample['lsfg'].get('reason'),
                     **{f'hw_{k}': v for k, v in sample['hardware'].items() if k not in excluded},
                     **{f'cpu_thread_{i}': value for i, value in enumerate(sample['hardware'].get('cores', []))},
                     **{f'frame_{k}': v for k, v in sample['game'].items() if k not in excluded})
                for sample in report['samples']]
        keys = list(dict.fromkeys(key for row in rows for key in row))
        if keys:
            export = csv.DictWriter(stream, keys)
            export.writeheader()
            export.writerows(rows)
    with (directory / 'frames.csv').open('w', newline='') as stream:
        export = csv.writer(stream)
        export.writerow(['seconds_from_benchmark', 'gamescope_frame_ms', 'stutter'])
        export.writerows(report['events'])
    if report.get('real_events'):
        with (directory / 'real-frames.csv').open('w', newline='') as stream:
            export = csv.writer(stream)
            export.writerow(['seconds_from_benchmark', 'real_frame_ms', 'generated_frames'])
            export.writerows(report['real_events'])
    for key, file, columns in (
        ('application_events', 'application-frames.csv', ['seconds', 'frame_ms', 'lsfg_generated_frames']),
        ('simulation_events', 'simulation-frames.csv', ['seconds', 'interval_ms']),
    ):
        if report.get(key):
            with (directory / file).open('w', newline='') as stream:
                export = csv.writer(stream)
                export.writerow(columns)
                export.writerows(report[key])
    write_report_page(directory, report)


def write_report_page(directory, report):
    template = Path(__file__).with_name('benchmark_report.html').read_text()
    embedded = json.dumps(report, ensure_ascii=True, allow_nan=False).replace('<', '\\u003c')
    preview = directory / '.report.html.tmp'
    preview.write_text(template.replace('/*BENCHMARK_DATA*/null', embedded, 1))
    preview.replace(directory / 'report.html')


def paths(record):
    root = Path(record['capture_path'])
    real = real_path(root)
    return {
        'application': real or next(root.glob('gl-*.bin'), None),
        'presented': root / 'presented-cadence.bin',
        'simulation': next(root.glob('simulation-*.bin'), None),
    }


def get_session(ident):
    return next((record for record in capture_sessions() if record['id'] == ident and record['running']), None)


def read_sample(start, hardware, record):
    snapshot = hardware.sample()
    snapshot.pop('history', None)
    if time.monotonic_ns() - start < SECOND:
        snapshot.update(cpu=None, cores=[None] * len(snapshot['cores']))
    game = {}
    reflex = record.get('reflex', {})
    if snapshot['ts'] - reflex.get('at', 0) < 3 and time.monotonic_ns() - start > 2 * SECOND:
        for key in ('cpu_render_ms', 'gpu_render_ms', 'input_present_ms'):
            if key in reflex:
                game[key] = reflex[key]
    # MangoHud may buffer its log. The sampler exposes periodic readings with
    # their coverage label, rather than inventing per-frame wall-clock timestamps.
    mango = record.get('mango_stats', {})
    if mango.get('age', 60) < 2 and time.monotonic_ns() - start > 11 * SECOND:
        game['fps'] = mango.get('fps')
    return dict(t=(time.monotonic_ns() - start) / SECOND, hardware=snapshot, game=game,
                source='MangoHud' if record['mango'] else 'Bencher',
                lsfg={'profile': record['profile'], 'reason': record['generation']})


def make_report(record, streams, start, finish, samples, mode, warnings, video=None):
    duration = (finish - start) / SECOND
    raw = {key: benchmark_frames(stream, start, finish, real=key == 'application')
           for key, stream in streams.items()}
    events = raw.get('presented', [])
    application = raw.get('application', [])
    generation = record['generation']
    lsfg_probed = generation == 'lsfg' and any(row[2] for row in application)
    real_known = generation == 'off' or lsfg_probed
    real = application if real_known else []
    simulations = raw.get('simulation', [])
    summary = frame_summary(real, duration)
    if lsfg_probed:
        summary['generated_fps'] = sum(row[2] for row in real) / duration
    if not events:
        warnings.append('Gamescope presentation timing is unavailable. Application timing is shown when captured.')
    if not application:
        warnings.append('Application frame timing is unavailable. Check the launch target and run bencher doctor.')
    elif not real_known:
        warnings.append('Real and generated frames cannot be separated on this capture path. Application output and any Reflex simulation markers are available.')
    return dict(title=record['title'], started=datetime.fromtimestamp(time.time()-duration).isoformat(timespec='seconds'),
                mode=mode, duration_s=round(duration, 6), warnings=warnings, samples=samples,
                events=events, summary=summarize(events, duration), real_events=real, real_summary=summary,
                application_events=application, application_summary=frame_summary(application, duration),
                simulation_events=simulations, simulation_summary=frame_summary(simulations, duration),
                generation=generation, frame_metrics_version=2, video=video)


def record(ident, duration, delay, wants_video, settings):
    RUNTIME.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (RUNTIME / 'benchmark.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another benchmark is running') from error
        STOP.unlink(missing_ok=True)
        state = {'pid': os.getpid(), 'capture_id': ident, 'last_report': read_json(STATUS).get('last_report')}
        def publish(phase, message):
            state.update(phase=phase, message=message)
            atomic(STATUS, state)
        def stopped():
            return read_json(STOP).get('pid') == os.getpid()
        directory = None
        video = None
        streams = {}
        try:
            session = get_session(ident)
            if not session:
                raise ValueError('Launch a game with bencher run and select it first.')
            output = reports(settings)
            output.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix='.pending-', dir=output))
            mp4 = directory / 'benchmark.mp4'
            warnings = []
            if wants_video:
                publish('armed', 'Preparing video capture…')
                try:
                    from .benchmark_video import Video, prepare, scrub_previews, video_source
                    gamescope = next((child for child in children(int(ident)) if is_gamescope(child)), None)
                    if not gamescope:
                        raise ValueError('Video capture requires a game launched with nested Gamescope.')
                    video = Video(prepare(), video_source(gamescope), mp4)
                except (OSError, RuntimeError, ValueError, ImportError, subprocess.SubprocessError) as error:
                    warnings.append(f'Video unavailable · {error}')
            countdown = time.monotonic() + delay
            while time.monotonic() < countdown and not stopped():
                publish('armed', f'Return to your game · {max(1, int(countdown - time.monotonic() + .99))} s')
                time.sleep(.1)
            if stopped():
                publish('idle', 'Benchmark cancelled')
                return None
            start = time.monotonic_ns()
            end = start + duration * SECOND
            if video:
                video.bounds = (start, end)
            hardware = Hardware()
            samples = []
            current = session
            while time.monotonic_ns() < end and not stopped():
                current = get_session(ident)
                if current is None:
                    break
                for key, path in paths(current).items():
                    if key not in streams and path and path.is_file():
                        streams[key] = source_frames(path, start, real=key != 'presented')
                samples.append(read_sample(start, hardware, current))
                elapsed = samples[-1]['t']
                publish('recording', f'Recording · {int(elapsed)} / {duration} s')
                if video:
                    try:
                        video.check()
                    except RuntimeError as error:
                        warnings.append(str(error))
                        video.close()
                        video = None
                tick = min(end, start + (int(elapsed) + 1) * SECOND)
                while time.monotonic_ns() < tick and not stopped():
                    time.sleep(min(.1, max(0, (tick - time.monotonic_ns()) / SECOND)))
            finish = max(start + 1_000_000, min(time.monotonic_ns(), end))
            publish('saving', 'Saving benchmark…')
            if finish < end:
                warnings.append('Recording ended early')
            report = make_report(session, streams, start, finish, samples, 'video' if wants_video else 'stats', warnings)
            if video:
                video.bounds = (start, finish)
                try:
                    report['video'] = video.finish(mp4)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    warnings.append(str(error))
                if report['video']:
                    try:
                        report['video']['preview'] = scrub_previews(mp4, report['video']['frame_times_s'])
                    except (OSError, RuntimeError, subprocess.SubprocessError):
                        warnings.append('Scrub previews unavailable. Exact video seeking is available.')
                elif video.finalized and mp4.is_file():
                    report['unindexed_video'] = True
            if not report.get('video') and not report.get('unindexed_video'):
                mp4.unlink(missing_ok=True)
            stamp = datetime.now().strftime('%Y-%m-%d %H-%M-%S-%f')
            destination = output / stamp
            write_report(directory, report)
            directory.rename(destination)
            page = destination / 'report.html'
            state['last_report'] = str(page)
            publish('done', 'Benchmark saved')
            return page
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            publish('error', str(error))
            raise
        finally:
            for stream in streams.values():
                if stream:
                    stream.close()
            if video:
                video.close()
            if directory and directory.exists():
                shutil.rmtree(directory)


def children(pid):
    output = []
    try:
        for child in Path(f'/proc/{pid}/task/{pid}/children').read_text().split():
            output.extend([int(child), *children(int(child))])
    except OSError:
        pass
    return output


def is_gamescope(pid):
    try:
        return Path(f'/proc/{pid}/comm').read_text().startswith('gamescope')
    except OSError:
        return False


def stop():
    state = read_json(STATUS)
    if state.get('phase') in ('armed', 'recording'):
        atomic(STOP, {'pid': state['pid']})


def worker_running(state):
    if state.get('phase') not in ('armed', 'recording', 'saving'):
        return False
    try:
        return Path(f"/proc/{int(state['pid'])}/stat").read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except (OSError, ValueError, KeyError, IndexError):
        return False


def saved_reports():
    return sorted((p for p in reports().glob('*/report.html') if p.is_file()
                   and not p.parent.name.startswith('.') and not p.parent.is_symlink()
                   and not p.is_symlink()), reverse=True)


def rename_report(path, name):
    if path not in saved_reports():
        raise ValueError('This report is no longer available')
    name = name.strip()
    if len(name) > 80 or (name and not name.isprintable()):
        raise ValueError('Use up to 80 printable characters')
    metadata = read_json(path.with_name('run.json'))
    if not metadata:
        data = read_json(path.with_name('telemetry.json'))
        metadata = {key: data.get(key) for key in ('title', 'started', 'duration_s', 'mode')}
    metadata['name'] = name
    atomic(path.with_name('run.json'), metadata)


def trash(path):
    if path not in saved_reports():
        raise ValueError('This report is no longer available')
    result = subprocess.run(['gio', 'trash', str(path.parent)], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or 'Could not move this report to Trash')
