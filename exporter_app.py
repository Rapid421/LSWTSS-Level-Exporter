"""LEGO Star Wars: The Skywalker Saga - Level Exporter (desktop app).

Reads the user's own installed game and builds a Blender .blend of a chosen
level. Modes of the same executable:
  (no args)            the window
  --prepare <args>     prepare_level.py (archive extraction, decoding, manifests)
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

APP_NAME = 'LSWTSS Level Exporter'
VERSION = '0.1.1'
FROZEN = getattr(sys, 'frozen', False)
# Bundled data (build_blend.py, lib/, ArchiveReader) lives beside the code.
DATA = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
GAME_DIR_NAME = 'LEGO Star Wars - The Skywalker Saga'
MIN_BLENDER = (4, 2)
SETTINGS = Path(os.environ.get('APPDATA', str(Path.home()))) / 'LSWTSS Level Exporter' / 'settings.json'
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def run_prepare(argv):
    sys.argv = ['prepare_level.py'] + argv
    sys.path.insert(0, str(DATA))
    import prepare_level
    prepare_level.main()


# ---------------------------------------------------------------- detection
def steam_libraries():
    roots = []
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam'),
                          (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam')):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ('SteamPath', 'InstallPath'):
                        try:
                            roots.append(Path(winreg.QueryValueEx(k, name)[0]))
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    roots += [Path(r'C:\Program Files (x86)\Steam'), Path(r'C:\Program Files\Steam')]
    libs = []
    for r in roots:
        vdf = r / 'steamapps' / 'libraryfolders.vdf'
        if vdf.exists():
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(encoding='utf-8', errors='ignore')):
                libs.append(Path(m.group(1).replace('\\\\', '\\')))
        libs.append(r)
    for letter in 'CDEFGHIJ':
        libs.append(Path(f'{letter}:/SteamLibrary'))
    seen, out = set(), []
    for l in libs:
        k = str(l).lower()
        if k not in seen:
            seen.add(k); out.append(l)
    return out


def detect_game():
    for lib in steam_libraries():
        p = lib / 'steamapps' / 'common' / GAME_DIR_NAME
        if (p / 'GAME.DAT').exists():
            return str(p)
    for base in (r'C:\Program Files\Epic Games', r'D:\Epic Games', r'C:\Program Files (x86)\Microsoft Games'):
        p = Path(base) / 'LEGOStarWarsSkywalkerSaga'
        if (p / 'GAME.DAT').exists():
            return str(p)
    return ''


def blender_version(path):
    m = re.search(r'Blender (\d+)\.(\d+)', str(path))
    return (int(m.group(1)), int(m.group(2))) if m else None


def detect_blender():
    found = []
    for base in (Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Blender Foundation',
                 Path(r'C:\Program Files\Blender Foundation')):
        if base.exists():
            for d in base.iterdir():
                exe = d / 'blender.exe'
                if exe.exists():
                    found.append(exe)
    for lib in steam_libraries():
        exe = lib / 'steamapps' / 'common' / 'Blender' / 'blender.exe'
        if exe.exists():
            found.append(exe)
    found.sort(key=lambda p: blender_version(p.parent) or (0, 0), reverse=True)
    return str(found[0]) if found else ''


def game_ok(p):
    return bool(p) and (Path(p) / 'GAME.DAT').exists() and (Path(p) / 'oo2core_8_win64.dll').exists()


# ---------------------------------------------------------------- GUI
def main_window():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    settings = {}
    try:
        settings = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except Exception:
        pass
    root = tk.Tk()
    root.title(f'{APP_NAME} {VERSION}')
    root.geometry('900x680'); root.minsize(760, 560)
    style = ttk.Style()
    try:
        style.theme_use('vista')
    except tk.TclError:
        pass

    game = tk.StringVar(value=settings.get('game') or detect_game())
    blender = tk.StringVar(value=settings.get('blender') or detect_blender())
    output = tk.StringVar(value=settings.get('output') or str(Path.home() / 'Documents' / 'LSWTSS Exports'))
    search = tk.StringVar()
    status = tk.StringVar(value='Choose your game folder and Blender, then load the level list.')
    events = queue.Queue()
    state = {'levels': [], 'busy': False, 'proc': None, 'cancel': False}

    frm = ttk.Frame(root, padding=12); frm.pack(fill='both', expand=True)
    frm.columnconfigure(1, weight=1)

    def row(r, label, var, browse):
        ttk.Label(frm, text=label).grid(row=r, column=0, sticky='w', pady=3)
        ttk.Entry(frm, textvariable=var).grid(row=r, column=1, sticky='ew', padx=6)
        ttk.Button(frm, text='Browse…', command=browse).grid(row=r, column=2)

    row(0, 'Game folder', game, lambda: game.set(filedialog.askdirectory(title='LEGO Star Wars: The Skywalker Saga folder (contains GAME.DAT)') or game.get()))
    row(1, 'Blender', blender, lambda: blender.set(filedialog.askopenfilename(title='blender.exe', filetypes=[('Blender', 'blender.exe')]) or blender.get()))
    row(2, 'Output folder', output, lambda: output.set(filedialog.askdirectory(title='Where to save exports') or output.get()))

    bar = ttk.Frame(frm); bar.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(10, 4)); bar.columnconfigure(2, weight=1)
    load_btn = ttk.Button(bar, text='Load level list'); load_btn.grid(row=0, column=0)
    ttk.Label(bar, text='  Search').grid(row=0, column=1)
    ttk.Entry(bar, textvariable=search).grid(row=0, column=2, sticky='ew', padx=6)

    tree = ttk.Treeview(frm, columns=('path',), show='tree', selectmode='browse', height=14)
    tree.grid(row=4, column=0, columnspan=3, sticky='nsew'); frm.rowconfigure(4, weight=3)
    sb = ttk.Scrollbar(frm, orient='vertical', command=tree.yview); sb.grid(row=4, column=3, sticky='ns'); tree.configure(yscrollcommand=sb.set)

    act = ttk.Frame(frm); act.grid(row=5, column=0, columnspan=3, sticky='ew', pady=6); act.columnconfigure(2, weight=1)
    export_btn = ttk.Button(act, text='Export selected level'); export_btn.grid(row=0, column=0)
    cancel_btn = ttk.Button(act, text='Cancel', state='disabled'); cancel_btn.grid(row=0, column=1, padx=6)
    progress = ttk.Progressbar(act, mode='indeterminate'); progress.grid(row=0, column=2, sticky='ew')
    open_btn = ttk.Button(act, text='Open output folder', command=lambda: os.startfile(output.get()) if Path(output.get()).exists() else None)
    open_btn.grid(row=0, column=3, padx=(6, 0))
    ttk.Label(frm, textvariable=status, foreground='#555').grid(row=6, column=0, columnspan=3, sticky='w')
    log = tk.Text(frm, height=10, wrap='none', font=('Consolas', 9)); log.grid(row=7, column=0, columnspan=3, sticky='nsew'); frm.rowconfigure(7, weight=2)

    def save_settings():
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps({'game': game.get(), 'blender': blender.get(), 'output': output.get()}), encoding='utf-8')
        except Exception:
            pass

    def cache_dir():
        return Path(output.get()) / '_cache'

    def prepare_cmd(args):
        exe = [sys.executable] if FROZEN else [sys.executable, str(Path(__file__).resolve())]
        return exe + ['--prepare', '--game', game.get(), '--archive-reader', str(DATA / 'ArchiveReader' / 'ArchiveReader.exe'),
                      '--cache-dir', str(cache_dir())] + args

    def check_paths(need_blender):
        if not game_ok(game.get()):
            messagebox.showerror(APP_NAME, 'The game folder must contain GAME.DAT and oo2core_8_win64.dll.\n'
                                 'Pick the "LEGO Star Wars - The Skywalker Saga" install folder.'); return False
        if need_blender:
            exe = Path(blender.get())
            if not exe.exists():
                messagebox.showerror(APP_NAME, 'Please choose blender.exe (Blender 4.2 or newer, free from blender.org).'); return False
            v = blender_version(exe.parent)
            if v and v < MIN_BLENDER:
                messagebox.showerror(APP_NAME, f'Blender {v[0]}.{v[1]} is too old; version 4.2 or newer is needed.'); return False
        try:
            Path(output.get()).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f'Cannot create the output folder:\n{e}'); return False
        return True

    def stream(cmd, label, cwd=None, env=None):
        """Run a step, sending its output lines to the log; returns the exit code."""
        events.put(('log', f'--- {label} ---'))
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd, env=env,
                             creationflags=NO_WINDOW, text=True, encoding='utf-8', errors='replace')
        state['proc'] = p
        last = []
        for line in p.stdout:
            line = line.rstrip()
            last = (last + [line])[-40:]
            if line and not line.startswith(('  "', '}', '{', ']')):
                events.put(('log', line[:400]))
        p.wait(); state['proc'] = None
        return p.returncode, last

    def worker(kind, level=None):
        try:
            if kind == 'list':
                events.put(('status', 'Reading the game archive index (the first time takes a few minutes)…'))
                tmp = cache_dir() / 'listing'; tmp.mkdir(parents=True, exist_ok=True)
                p = subprocess.run(prepare_cmd(['--level', 'levels', '--list-levels', '--output', str(tmp)]), capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
                line = next((l for l in reversed(p.stdout.splitlines()) if l.startswith('[')), None)
                if p.returncode or not line:
                    events.put(('error', 'Could not read the level list.\n\n' + (p.stdout + p.stderr)[-1500:])); return
                events.put(('levels', json.loads(line)))
                return
            name = level.strip('/').split('/')[-1]
            stamp = time.strftime('%Y%m%d_%H%M%S')
            out = Path(output.get()) / f'{name}_{stamp}'
            out.mkdir(parents=True, exist_ok=True)
            events.put(('status', f'Exporting {level} — step 1 of 2: reading and decoding the game data…'))
            code, last = stream(prepare_cmd(['--level', level, '--output', str(out)]), 'Prepare')
            if state['cancel']:
                events.put(('done', 'Cancelled.')); return
            if code:
                events.put(('error', 'Preparing the level failed:\n\n' + '\n'.join(last[-15:]))); return
            events.put(('status', f'Exporting {level} — step 2 of 2: building the Blender scene (large maps take 10–20 minutes)…'))
            blend = out / f'{name}.blend'
            env = dict(os.environ, PYTHONNOUSERSITE='1')
            code, last = stream([blender.get(), '-b', '--factory-startup', '--python', str(DATA / 'build_blend.py'), '--',
                                 str(out / 'hierarchy_manifest.json'), str(out / 'material_texture_manifest.json'), str(blend)], 'Blender', env=env)
            if state['cancel']:
                events.put(('done', 'Cancelled.')); return
            if code or not blend.exists():
                events.put(('error', 'Blender could not build the scene:\n\n' + '\n'.join(last[-15:]))); return
            # The manifests are only build inputs; keep the reports and the .blend.
            for f in ('hierarchy_manifest.json', 'light_manifest.json', 'material_texture_manifest.json'):
                (out / f).unlink(missing_ok=True)
            events.put(('done', f'Done: {blend}'))
            events.put(('open', str(out)))
        except Exception as e:
            events.put(('error', f'{type(e).__name__}: {e}'))

    def start(kind, level=None):
        state['busy'] = True; state['cancel'] = False
        for b in (load_btn, export_btn): b.configure(state='disabled')
        cancel_btn.configure(state='normal' if kind == 'export' else 'disabled')
        progress.start(12)
        threading.Thread(target=worker, args=(kind, level), daemon=True).start()

    def finish():
        state['busy'] = False; progress.stop()
        for b in (load_btn, export_btn): b.configure(state='normal')
        cancel_btn.configure(state='disabled')

    def fill_tree(*_):
        q = search.get().lower().strip()
        tree.delete(*tree.get_children())
        groups = {}
        for lv in state['levels']:
            if q and q not in lv.lower():
                continue
            parts = lv.split('/')
            group = '/'.join(parts[1:3]) if len(parts) > 3 else '/'.join(parts[1:2]) or 'levels'
            if group not in groups:
                groups[group] = tree.insert('', 'end', text=group.replace('_', ' ').replace('/', ' › '), open=bool(q))
            tree.insert(groups[group], 'end', text='/'.join(parts[3:]) or lv, values=(lv,))
    search.trace_add('write', fill_tree)

    def on_load():
        if check_paths(False):
            save_settings(); start('list')

    def on_export():
        sel = tree.selection()
        vals = tree.item(sel[0], 'values') if sel else None
        if not vals:
            messagebox.showinfo(APP_NAME, 'Select a level in the list first.'); return
        if check_paths(True):
            save_settings(); start('export', vals[0])

    def on_cancel():
        state['cancel'] = True
        p = state.get('proc')
        if p:
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], capture_output=True, creationflags=NO_WINDOW)

    load_btn.configure(command=on_load); export_btn.configure(command=on_export); cancel_btn.configure(command=on_cancel)
    tree.bind('<Double-1>', lambda e: on_export())

    def pump():
        try:
            while True:
                kind, val = events.get_nowait()
                if kind == 'log':
                    log.insert('end', val + '\n'); log.see('end')
                elif kind == 'status':
                    status.set(val)
                elif kind == 'levels':
                    state['levels'] = [l for l in val if not l.startswith(('levels/hub/events', 'levels/test')) and l != 'levels']
                    fill_tree(); finish(); status.set(f'{len(state["levels"])} levels found. Select one and press Export.')
                elif kind == 'done':
                    finish(); status.set(val); log.insert('end', val + '\n'); log.see('end')
                elif kind == 'open':
                    if messagebox.askyesno(APP_NAME, 'Export finished. Open the output folder?'):
                        os.startfile(val)
                elif kind == 'error':
                    finish(); status.set('Failed — see the message.'); log.insert('end', val + '\n'); messagebox.showerror(APP_NAME, val[-2000:])
        except queue.Empty:
            pass
        root.after(150, pump)
    pump()
    root.protocol('WM_DELETE_WINDOW', lambda: (on_cancel(), root.destroy()))
    root.mainloop()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--prepare':
        run_prepare(sys.argv[2:])
    else:
        main_window()
