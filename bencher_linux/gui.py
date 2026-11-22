"""Portable desktop Live View, benchmark library, and preferences."""

from concurrent.futures import ThreadPoolExecutor
import shlex
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import benchmark, config
from .live import Live
from .session import capture_sessions

BG = '#111821'
CARD = '#192631'
PANEL = '#15212b'
INK = '#edf4f6'
MUTED = '#a9bbc4'
ACCENT = '#8ee2cf'
BORDER = '#334b58'
FIELDS = dict(real_fps='Application / real FPS', presented_fps='Presented FPS',
              generated_fps='LSFG-generated FPS', simulation_fps='Reflex simulation FPS',
              low_1='1% lows', cpu='CPU load', gpu='GPU load', ram='RAM', vram='VRAM',
              temperatures='Temperatures', disk='Disk I/O', network='Network', chart='Frame-time graph')
GENERATION = {'unknown': 'LSFG-VK off · In-game mode unknown',
              'off': 'Frame generation disabled in game',
              'lsfg': 'Enable LSFG-VK',
              'in-game': 'In-game frame generation enabled'}
CAPTURE = {'auto': 'Automatic', 'vulkan': 'Vulkan probe only', 'mangohud': 'MangoHud'}


def fmt(value, unit='', digits=0):
    return f'{value:.{digits}f}{unit}' if isinstance(value, (float, int)) else '—'


class App:
    def __init__(self, root):
        self.root = root
        self.prefs = config.preferences()
        self.live = Live()
        self.worker = ThreadPoolExecutor(max_workers=1)
        self.pending = None
        self.games = {}
        self.tiles = {}
        self.report_paths = {}
        self.last_status = None
        self.status = tk.StringVar(value='Ready')
        self.benchmark_child = None
        root.title('Bencher Linux')
        root.geometry('1000x730')
        root.minsize(720, 640)
        root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.style()
        header = ttk.Frame(root, padding=(25, 17))
        header.pack(fill='x')
        ttk.Label(header, text='Bencher Linux', style='Title.TLabel').pack(side='left')
        ttk.Button(header, text='Copy Steam launch option', command=self.copy_launch).pack(side='right')
        tabs = ttk.Notebook(root)
        tabs.pack(fill='both', expand=True, padx=24, pady=(0, 15))
        self.tabs = tabs
        self.live_page = ttk.Frame(tabs, padding=14)
        self.library_page = ttk.Frame(tabs, padding=14)
        self.settings_page = ttk.Frame(tabs, padding=14)
        for page, title in ((self.live_page,'Live View'), (self.library_page,'Benchmarks'), (self.settings_page,'Settings')):
            tabs.add(page, text=title)
        tabs.bind('<<NotebookTabChanged>>', self.switch_page)
        self.build_live()
        self.build_library()
        self.build_settings()
        ttk.Label(root, textvariable=self.status, style='Muted.TLabel', padding=(25, 0, 25, 12)).pack(fill='x')
        self.tick()

    def style(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', background=BG, foreground=INK, font=('DejaVu Sans', 10),
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure('Title.TLabel', font=('DejaVu Sans', 24, 'bold'))
        style.configure('Muted.TLabel', foreground=MUTED)
        style.configure('TNotebook', background=BG, borderwidth=0)
        style.configure('TNotebook.Tab', padding=(15, 8), background=PANEL, foreground=MUTED)
        style.map('TNotebook.Tab', background=[('selected', CARD)], foreground=[('selected', ACCENT)])
        style.configure('TButton', padding=(10, 7), background=CARD, foreground=INK, borderwidth=1)
        style.map('TButton', background=[('active', '#2b4952')], foreground=[('disabled', '#73828a')])
        style.configure('TCombobox', fieldbackground=PANEL, background=CARD, foreground=INK, padding=4)
        style.map('TCombobox', fieldbackground=[('readonly', PANEL)], foreground=[('readonly', INK)])
        style.configure('TEntry', fieldbackground=PANEL, foreground=INK, insertcolor=INK)
        style.configure('TSpinbox', fieldbackground=PANEL, foreground=INK, insertcolor=INK)
        style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=INK, rowheight=30)
        style.configure('Treeview.Heading', background=CARD, foreground=INK, padding=7)
        style.map('Treeview', background=[('selected', '#2b4952')])
        style.configure('TLabelframe', background=BG, padding=10)
        self.root.option_add('*TCombobox*Listbox.background', PANEL)
        self.root.option_add('*TCombobox*Listbox.foreground', INK)

    def copy_launch(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(shlex.join([sys.executable, '-m', 'bencher_linux', 'run', '--']) + ' %command%')
        self.status.set('Launch option copied. Paste it into the game’s Steam launch options.')

    def build_live(self):
        row = ttk.Frame(self.live_page)
        row.pack(fill='x', pady=(0, 10))
        ttk.Label(row, text='Game session').pack(side='left', padx=(0,10))
        self.game_pick = ttk.Combobox(row, state='readonly')
        self.game_pick.pack(side='left', fill='x', expand=True)
        self.game_pick.bind('<<ComboboxSelected>>', lambda _event: self.refresh_live())
        self.launch_info = tk.StringVar(value='Launch with bencher run to start capturing.')
        ttk.Label(self.live_page, textvariable=self.launch_info, style='Muted.TLabel', wraplength=700).pack(fill='x', pady=(0,10))
        self.tile_area = ttk.Frame(self.live_page)
        self.tile_area.pack(fill='x')
        for i in range(4):
            self.tile_area.columnconfigure(i, weight=1, uniform='metrics')
        self.chart = tk.Canvas(self.live_page, bg=PANEL, height=126, highlightthickness=0)
        self.benchmark_bar = ttk.Frame(self.live_page)
        self.benchmark_bar.pack(side='bottom', fill='x', pady=(12,0))
        self.seconds = tk.StringVar(value=str(self.prefs['duration']))
        self.video = tk.BooleanVar(value=self.prefs['video'])
        ttk.Label(self.benchmark_bar, text='Duration').pack(side='left')
        ttk.Spinbox(self.benchmark_bar, from_=5, to=1800, width=6, textvariable=self.seconds).pack(side='left', padx=6)
        ttk.Label(self.benchmark_bar, text='seconds').pack(side='left')
        ttk.Checkbutton(self.benchmark_bar, text='Record video', variable=self.video).pack(side='left', padx=12)
        self.stop_button = ttk.Button(self.benchmark_bar, text='Stop', command=benchmark.stop, state='disabled')
        self.stop_button.pack(side='right')
        self.record_button = ttk.Button(self.benchmark_bar, text='Start benchmark', command=self.start_record)
        self.record_button.pack(side='right', padx=8)
        self.rebuild_tiles()

    def rebuild_tiles(self):
        for child in self.tile_area.winfo_children():
            child.destroy()
        self.tiles = {}
        for field in self.prefs['visible']:
            if field == 'chart':
                continue
            card = tk.Frame(self.tile_area, bg=CARD, padx=10, pady=7)
            idx = len(self.tiles)
            card.grid(column=idx % 4, row=idx // 4, sticky='nsew', padx=3, pady=3)
            label = tk.Label(card, text=FIELDS[field], anchor='w', bg=CARD, fg=MUTED,
                             font=('DejaVu Sans', 9))
            label.pack(fill='x')
            value = tk.Label(card, text='—', anchor='w', bg=CARD, fg=INK,
                             font=('DejaVu Sans', 13, 'bold'))
            value.pack(fill='x')
            self.tiles[field] = label, value
        self.chart.pack_forget()
        if 'chart' in self.prefs['visible']:
            self.chart.pack(fill='both', expand=True, pady=(12,0))

    def build_library(self):
        row = ttk.Frame(self.library_page)
        row.pack(fill='x', pady=(0,10))
        ttk.Label(row, text='Saved benchmarks', style='Title.TLabel').pack(side='left')
        ttk.Button(row, text='Delete', command=self.delete_report).pack(side='right')
        ttk.Button(row, text='Name', command=self.rename_report).pack(side='right',padx=8)
        ttk.Button(row, text='Open report', command=self.open_report).pack(side='right', padx=8)
        self.table = ttk.Treeview(self.library_page, columns=('game','started','duration','video'), show='headings', selectmode='browse')
        for key, title, width in (('game','Game',235),('started','Recorded',180),('duration','Duration',90),('video','Video',75)):
            self.table.heading(key, text=title)
            self.table.column(key, width=width)
        self.table.pack(fill='both', expand=True)
        self.table.bind('<Double-1>', lambda _event: self.open_report())

    def build_settings(self):
        hidden = ttk.LabelFrame(self.settings_page, text='Live View')
        hidden.pack(fill='x', pady=(0,12))
        self.visibility = {}
        for i, (key, label) in enumerate(FIELDS.items()):
            value = tk.BooleanVar(value=key in self.prefs['visible'])
            self.visibility[key] = value
            ttk.Checkbutton(hidden, text=label, variable=value).grid(row=i//3,column=i%3,sticky='w',pady=2,padx=8)
        form = ttk.LabelFrame(self.settings_page, text='Capture and recording')
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        self.inputs = {}
        for i,(key,label) in enumerate((('generation','Frame generation'),('profile','LSFG-VK profile'),
                             ('capture','Capture source'),('interval','Live refresh (seconds)'),
                             ('duration','Default recording (seconds)'),('delay','Time to return to game (seconds)'),
                             ('report_dir','Benchmark folder'))):
            ttk.Label(form, text=label).grid(row=i,column=0,sticky='w',padx=8,pady=5)
            choices = GENERATION if key=='generation' else CAPTURE if key=='capture' else None
            value = tk.StringVar(value=choices[self.prefs[key]] if choices else str(self.prefs[key]))
            self.inputs[key] = value
            control = ttk.Combobox(form,state='readonly',values=list(choices.values()),textvariable=value) if choices else ttk.Entry(form,textvariable=value)
            control.grid(row=i,column=1,sticky='ew',padx=8,pady=5)
            if key == 'report_dir':
                def browse():
                    if path := filedialog.askdirectory(initialdir=self.inputs['report_dir'].get()):
                        self.inputs['report_dir'].set(path)
                ttk.Button(form,text='Browse',command=browse).grid(row=i,column=2,padx=8)
        self.default_video = tk.BooleanVar(value=self.prefs['video'])
        ttk.Checkbutton(form,text='Enable video by default',variable=self.default_video).grid(row=7,column=1,sticky='w',padx=8,pady=4)
        row = ttk.Frame(self.settings_page)
        row.pack(fill='x', pady=(8,0))
        ttk.Label(row,text='Frame generation and capture settings apply on the next game launch.',style='Muted.TLabel').pack(side='left')
        ttk.Button(row,text='Save settings',command=self.save).pack(side='right')

    def save(self):
        raw = {name: variable.get() for name, variable in self.inputs.items()}
        for key, lower, upper in (('interval',1,10),('duration',5,1800),('delay',0,30)):
            try:
                raw[key] = int(raw[key])
                if not lower <= raw[key] <= upper:
                    raise ValueError
            except ValueError:
                messagebox.showerror('Bencher Linux', f'{key.capitalize()} must be between {lower} and {upper}.')
                return
        for key, choices in (('generation',GENERATION),('capture',CAPTURE)):
            raw[key] = next(value for value,label in choices.items() if label==raw[key])
        raw.update(visible=[key for key,value in self.visibility.items() if value.get()],
                   video=self.default_video.get())
        try:
            self.prefs = config.validate(raw)
            config.atomic(config.PREFERENCES, self.prefs)
        except OSError as error:
            messagebox.showerror('Bencher Linux',str(error))
            return
        self.seconds.set(str(self.prefs['duration']))
        self.video.set(self.prefs['video'])
        self.rebuild_tiles()
        self.refresh_library()
        self.status.set('Settings saved')

    def switch_page(self, _event):
        if self.tabs.select()==str(self.library_page):
            self.refresh_library()

    def refresh_library(self):
        self.table.delete(*self.table.get_children())
        self.report_paths = {}
        for i,path in enumerate(benchmark.saved_reports()):
            data = config.read_json(path.with_name('run.json'))
            if not data:
                data = config.read_json(path.with_name('telemetry.json'))
            row = str(i)
            self.table.insert('', 'end', iid=row, values=(data.get('name') or data.get('title') or path.parent.name,
                (data.get('started') or '').replace('T',' '),fmt(data.get('duration_s'),' s'),
                'Yes' if path.with_name('benchmark.mp4').is_file() else '—'))
            self.report_paths[row] = path

    def selected_report(self):
        selected = self.table.selection()
        return self.report_paths.get(selected[0]) if selected else None

    def open_report(self):
        if path := self.selected_report():
            try:
                subprocess.Popen(['xdg-open', str(path)], start_new_session=True)
            except OSError as error:
                messagebox.showerror('Bencher Linux', str(error))

    def rename_report(self):
        if path := self.selected_report():
            name = simpledialog.askstring('Name benchmark', 'Benchmark name', parent=self.root)
            if name is not None:
                try:
                    benchmark.rename_report(path, name)
                except (OSError, ValueError) as error:
                    messagebox.showerror('Bencher Linux', str(error))
                self.refresh_library()

    def delete_report(self):
        if (path := self.selected_report()) and messagebox.askyesno('Delete benchmark', 'Move this recording and its files to Trash?'):
            try:
                benchmark.trash(path)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                messagebox.showerror('Bencher Linux',str(error))
            self.refresh_library()

    def start_record(self):
        capture = self.games.get(self.game_pick.get())
        if not capture:
            self.status.set('Choose an active game first')
            return
        try:
            seconds = int(self.seconds.get())
            if not 5 <= seconds <= 1800:
                raise ValueError('Enter a duration from 5 to 1800 seconds')
            args = [sys.executable,'-m','bencher_linux','record','--session',capture['id'],
                    '--seconds',str(seconds),'--video' if self.video.get() else '--no-video']
            self.benchmark_child = subprocess.Popen(args, start_new_session=True)
            self.record_button.configure(state='disabled')
        except (ValueError,OSError) as error:
            self.status.set(str(error))

    def refresh_live(self):
        picked = self.game_pick.get()
        self.games = {f"{game['title']} · {game['id']}":game for game in capture_sessions() if game['running']}
        self.game_pick.configure(values=list(self.games))
        if picked not in self.games:
            self.game_pick.set(next(iter(self.games), ''))
        game = self.games.get(self.game_pick.get())
        if self.pending is None:
            self.pending = self.worker.submit(self.measure, game)

    def measure(self, game):
        hardware, frames = self.live.sample(game)
        key = 'presented' if frames.get('presented') else 'application'
        return game, hardware, frames, self.live.plot(key)

    def display(self, game, hw, frames, history):
        if game and game['id'] != (self.games.get(self.game_pick.get()) or {}).get('id'):
            return
        app = frames.get('application') or {}
        present = frames.get('presented') or {}
        sim = frames.get('simulation') or {}
        generation = game['generation'] if game else 'unknown'
        real = generation == 'off' or (generation == 'lsfg' and app.get('generated_fps',0)>0)
        if game:
            label = GENERATION[generation]
            if not app:
                label += ' · Waiting for application frames'
            self.launch_info.set(label)
        else:
            self.launch_info.set('Launch with bencher run to start capturing.')
        sampled = (game or {}).get('mango_stats', {})
        values = {
            'real_fps': ('Real FPS' if real else 'Application FPS',
                fmt(app.get('fps') or (sampled.get('fps') if sampled.get('age',99)<3 else None))),
            'presented_fps': ('Presented FPS', fmt(present.get('fps'))),
            'generated_fps': ('LSFG-generated FPS',fmt(app.get('generated_fps') if generation=='lsfg' and real else None)),
            'simulation_fps': ('Reflex simulation FPS',fmt(sim.get('fps'))),
            'low_1': ('1% low · app / presented',f"{fmt(app.get('low'))} / {fmt(present.get('low'))}"),
            'cpu': ('CPU load',fmt(hw.get('cpu'),'%')),
            'gpu': ('GPU load',fmt(hw.get('gpu'),'%')),
            'ram': ('RAM',fmt(hw.get('ram_used'),' GiB',1)),
            'vram': ('VRAM',fmt(hw.get('vram_used'),' GiB',1)),
            'temperatures': ('CPU / GPU',f"{fmt(hw.get('cpu_temp'),'°')} / {fmt(hw.get('gpu_temp'),'°')}"),
            'disk': ('Disk read / write',self.rates(hw,'disk_read','disk_write')),
            'network': ('Download / upload',self.rates(hw,'download','upload')),
        }
        for field,(label, value) in self.tiles.items():
            label.config(text=values[field][0])
            value.config(text=values[field][1])
        self.draw_chart(history, bool(present))

    @staticmethod
    def rates(hw, *fields):
        return ' / '.join(fmt(hw.get(key,0)/1048576,'',1) for key in fields) + ' MiB/s'

    def draw_chart(self, intervals, presented):
        self.chart.delete('all')
        w, h = self.chart.winfo_width(), self.chart.winfo_height()
        self.chart.create_text(14, 16, anchor='w', fill=MUTED, text='Presented frame time' if presented else 'Application frame time')
        if len(intervals)<2:
            return
        ceiling = max(24, max(intervals))
        for y in (h*.45,h*.7):
            self.chart.create_line(14,y,w-14,y,fill='#314751')
        points = [(14+(w-28)*i/(len(intervals)-1), h-12-v/ceiling*(h-46))
                  for i,v in enumerate(intervals)]
        self.chart.create_line(points,fill=ACCENT,width=2)
        self.chart.create_text(w-14,16,anchor='e',fill=MUTED,text=f'0–{ceiling:.0f} ms')

    def tick(self):
        status = config.read_json(benchmark.STATUS)
        if self.benchmark_child and self.benchmark_child.poll() is not None:
            self.benchmark_child = None
        active = benchmark.worker_running(status)
        if status != self.last_status:
            interrupted = status.get('phase') in ('armed','recording','saving') and not active
            self.status.set('Recording stopped unexpectedly' if interrupted else status.get('message','Ready'))
            self.last_status = status
            if status.get('phase')=='done' and self.tabs.select()==str(self.library_page):
                self.refresh_library()
        if not active:
            self.record_button.configure(state='normal')
        else:
            self.record_button.configure(state='disabled')
        self.stop_button.configure(state='normal' if active and status.get('phase')!='saving' else 'disabled')
        if self.pending and self.pending.done():
            try:
                self.display(*self.pending.result())
            except (OSError, ValueError) as error:
                self.status.set(str(error))
            self.pending = None
        if self.tabs.select()==str(self.live_page):
            self.refresh_live()
        self.root.after(self.prefs['interval']*1000,self.tick)

    def close(self):
        self.worker.shutdown(wait=True, cancel_futures=True)
        self.live.close()
        self.root.destroy()


def main():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise RuntimeError('Open Bencher from a graphical Linux session') from exc
    App(root)
    root.mainloop()
