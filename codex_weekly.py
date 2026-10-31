import os
import sys
import json
import math
import queue
import tempfile
import threading
import traceback
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
import urllib.request
import urllib.error
import winreg
import win32api
import win32con
import win32gui
from PIL import Image, ImageDraw

AUTH = Path.home() / '.codex' / 'auth.json'
URL = 'https://chatgpt.com/backend-api/wham/usage'
TRAY_MESSAGE = win32con.WM_USER + 20
REFRESH_SECONDS = 900
ICON_PATH = Path(tempfile.gettempdir()) / ('codex-weekly-grid-%d.ico' % os.getpid())
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'Codex 周额度'


def startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
            return bool(value)
    except FileNotFoundError:
        return False


def set_startup(enabled):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            if getattr(sys, 'frozen', False):
                command = f'"{sys.executable}"'
            else:
                pythonw = Path(sys.executable).with_name('pythonw.exe')
                interpreter = pythonw if pythonw.exists() else Path(sys.executable)
                command = f'"{interpreter}" "{Path(__file__).resolve()}"'
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def get_usage():
    auth = json.loads(AUTH.read_text(encoding='utf-8-sig'))
    token = auth.get('tokens') or {}
    if not token.get('access_token') or not token.get('account_id'):
        raise ValueError('请先登录 Codex')
    req = urllib.request.Request(URL, headers={
        'Authorization': 'Bearer ' + token['access_token'],
        'ChatGPT-Account-Id': token['account_id'],
        'User-Agent': 'codex-weekly-grid/1.0',
        'Accept': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.load(response)
    limit = data.get('rate_limit') or {}
    candidates = (limit.get('primary_window'), limit.get('secondary_window'))
    weekly = next((x for x in candidates if isinstance(x, dict) and x.get('limit_window_seconds') == 604800), None)
    if weekly is None:
        raise ValueError('未找到 7 天额度')
    used = float(weekly['used_percent'])
    if not math.isfinite(used):
        raise ValueError('额度数据无效')
    return max(0, min(100, 100-used)), weekly.get('reset_at')


def reset_progress(reset_at, now=None):
    if reset_at is None:
        return None, '重置时间未知'
    now = time.time() if now is None else now
    seconds = max(0, reset_at - now)
    elapsed = max(0.0, min(1.0, 1.0 - seconds / 604800))
    if seconds <= 0:
        return elapsed, '等待额度重置更新'
    minutes = max(1, math.ceil(seconds / 60))
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        label = f'还有 {days} 天 {hours} 小时'
    elif hours:
        label = f'还有 {hours} 小时 {mins} 分钟'
    else:
        label = f'还有 {mins} 分钟'
    return elapsed, label


def grid_image(remaining, stale=False):
    im = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    # Each block represents 6.25% of the total. A partial block changes
    # color in six discrete stages as its remaining fraction decreases.
    colors = ('#d84545', '#e87932', '#e7aa2e', '#d1bf31', '#94bb3b', '#17a565')
    blocks = 0 if remaining is None else max(0.0, min(16.0, remaining * 16 / 100))
    for i in range(16):
        x, y = (i % 4) * 16, (i // 4) * 16
        fraction = max(0.0, min(1.0, blocks - i))
        if remaining is None:
            color = '#bcc4ce'
        elif stale:
            color = '#888e98'
        elif fraction <= 0:
            color = '#d84545'
        else:
            color = colors[min(5, max(0, math.ceil(fraction * 6) - 1))]
        draw.rectangle((x + (2 if i % 4 else 0), y + (2 if i // 4 else 0), x + 15 - (2 if i % 4 < 3 else 0), y + 15 - (2 if i // 4 < 3 else 0)), fill=color)
    return im


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title('Codex 周额度')
        self.root.protocol('WM_DELETE_WINDOW', self.hide)
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.configure(bg='#ffffff', highlightbackground='#d9dfe6', highlightthickness=1)
        self.remaining = None
        self.reset = None
        self.updated = None
        self.error = None
        self.loading = False
        self.timer = None
        self.jobs = queue.Queue()
        self.actions = queue.Queue()
        self.icon = None
        self.showing = False
        self.build_card()
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = 'CodexWeeklyGridWindow_' + str(os.getpid())
        wc.lpfnWndProc = {TRAY_MESSAGE: self.on_tray, win32con.WM_DESTROY: self.on_destroy}
        self.atom = win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindow(self.atom, 'Codex Weekly Grid', 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
        self.update_icon(add=True, stale=True)
        self.refresh()
        self.root.after(100, self.pump)
        self.root.after(60000, self.countdown_tick)

    def build_card(self):
        bg = '#ffffff'
        muted = '#6c7889'
        frame = tk.Frame(self.root, bg=bg, padx=20, pady=16)
        frame.pack(fill='both', expand=True)

        header = tk.Frame(frame, bg=bg)
        header.pack(fill='x')
        tk.Label(header, text='CODEX  /  周额度', font=('Microsoft YaHei UI', 10, 'bold'),
                 bg=bg, fg='#354256').pack(side='left')
        tk.Button(header, text='×', command=self.hide, font=('Segoe UI', 16),
                  bg=bg, fg='#8190a1', activebackground='#edf2f4', relief='flat',
                  bd=0, padx=7, cursor='hand2', takefocus=True).pack(side='right')

        tk.Label(frame, text='剩余额度', font=('Microsoft YaHei UI', 9), bg=bg,
                 fg=muted, anchor='w').pack(fill='x', pady=(14, 0))
        self.value_label = tk.Label(frame, text='--', font=('Segoe UI', 31, 'bold'),
                                    bg=bg, fg='#152536', anchor='w')
        self.value_label.pack(fill='x')

        info = tk.Frame(frame, bg='#f5f8f7', padx=12, pady=8)
        info.pack(fill='x', pady=(5, 0))
        self.detail_label = tk.Label(info, text='正在获取数据…', font=('Microsoft YaHei UI', 9),
                                     bg='#f5f8f7', fg='#536276', justify='left', anchor='w')
        self.detail_label.pack(fill='x')
        time_row = tk.Frame(info, bg='#f5f8f7')
        time_row.pack(fill='x', pady=(10, 3))
        tk.Label(time_row, text='本周进度', font=('Microsoft YaHei UI', 8),
                 bg='#f5f8f7', fg='#7a8e86').pack(side='left')
        self.countdown_label = tk.Label(time_row, text='重置时间未知', font=('Microsoft YaHei UI', 8),
                                        bg='#f5f8f7', fg='#44866b')
        self.countdown_label.pack(side='right')
        self.time_bar = tk.Canvas(info, height=8, bg='#f5f8f7', bd=0, highlightthickness=0)
        self.time_bar.pack(fill='x', pady=(0, 2))
        self.time_bar.bind('<Configure>', lambda event: self.update_countdown())

        self.status_label = tk.Label(frame, text='', font=('Microsoft YaHei UI', 8),
                                     bg=bg, fg='#83909d', anchor='w')
        self.status_label.pack(fill='x', pady=(8, 7))
        self.button = tk.Button(frame, text='刷新额度', command=self.refresh,
                                font=('Microsoft YaHei UI', 10, 'bold'), bg='#1c8764', fg='white',
                                activebackground='#167453', activeforeground='white',
                                disabledforeground='#cedbd4', relief='flat', bd=0, pady=7,
                                cursor='hand2', takefocus=True, padx=14)
        self.button.pack(anchor='e')
        tk.Frame(frame, bg='#e7ecec', height=1).pack(fill='x', pady=(14, 9))
        bottom = tk.Frame(frame, bg=bg)
        bottom.pack(fill='x')
        self.startup_var = tk.BooleanVar(master=self.root, value=startup_enabled())
        tk.Checkbutton(bottom, text='开机启动', variable=self.startup_var, command=self.toggle_startup,
                       bg=bg, activebackground=bg, fg='#526173', selectcolor=bg,
                       font=('Microsoft YaHei UI', 9), highlightthickness=0,
                       cursor='hand2').pack(side='left', ipady=4)
        tk.Button(bottom, text='退出程序', command=self.quit, relief='flat', bg=bg,
                  activebackground='#f1f3f5', fg='#687789', font=('Microsoft YaHei UI', 9),
                  cursor='hand2', padx=8, pady=4).pack(side='right')
        self.root.bind('<Escape>', lambda event: self.hide())
        self.root.bind('<FocusOut>', self.focus_out)

    def update_countdown(self):
        progress, text = reset_progress(self.reset)
        self.countdown_label.config(text=text)
        canvas = self.time_bar
        width = max(8, canvas.winfo_width())
        canvas.delete('all')
        canvas.create_line(4, 4, width - 4, 4, width=6, fill='#dce9e2', capstyle=tk.ROUND)
        if progress is not None and progress > 0:
            end = 4 + (width - 8) * progress
            canvas.create_line(4, 4, max(4.1, end), 4, width=6,
                               fill='#2c9a72', capstyle=tk.ROUND)

    def countdown_tick(self):
        self.update_countdown()
        self.root.after(60000, self.countdown_tick)

    def focus_out(self, event):
        self.root.after(150, self.check_focus)

    def check_focus(self):
        if self.showing and self.root.focus_displayof() is None:
            self.hide()

    def update_icon(self, add=False, stale=False):
        image = grid_image(self.remaining, stale)
        image.save(ICON_PATH, format='ICO', sizes=[(16,16), (32,32), (64,64)])
        new_icon = win32gui.LoadImage(0, str(ICON_PATH), win32con.IMAGE_ICON, 0, 0, win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
        tip = 'Codex 周额度' + ('（数据可能过期）' if stale else '')
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD if add else win32gui.NIM_MODIFY, (self.hwnd, 1, flags, TRAY_MESSAGE, new_icon, tip))
        if self.icon:
            win32gui.DestroyIcon(self.icon)
        self.icon = new_icon

    def on_tray(self, hwnd, msg, wp, lp):
        # The Windows callback only queues work. Tk actions run after the
        # callback returns, avoiding nested event loops in the window procedure.
        if lp in (win32con.WM_LBUTTONUP, win32con.WM_RBUTTONUP):
            self.actions.put('toggle')
        return 0

    def toggle_startup(self):
        try:
            set_startup(self.startup_var.get())
        except OSError:
            self.startup_var.set(startup_enabled())
            win32gui.MessageBox(self.hwnd, '无法修改开机启动设置，请检查 Windows 权限。', 'Codex 周额度', win32con.MB_OK | win32con.MB_ICONERROR)

    def on_destroy(self, hwnd, msg, wp, lp):
        return 0

    def show(self):
        self.startup_var.set(startup_enabled())
        self.paint()
        self.root.update_idletasks()
        width = max(300, self.root.winfo_reqwidth())
        height = self.root.winfo_reqheight()
        x, y = win32gui.GetCursorPos()
        work = win32api.GetMonitorInfo(win32api.MonitorFromPoint((x, y)))['Work']
        x = min(max(work[0], x - width // 2), work[2] - width)
        y = min(max(work[1], y - height - 12), work[3] - height)
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        self.showing = True
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.showing = False
        self.root.withdraw()

    def refresh(self):
        if self.loading: return
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.loading = True
        self.button.config(state='disabled')
        self.paint()
        def worker():
            try:
                self.jobs.put(('ok', get_usage()))
            except urllib.error.HTTPError as e:
                self.jobs.put(('error', '登录已失效，请重新登录 Codex' if e.code in (401,403) else f'请求失败（HTTP {e.code}）'))
            except FileNotFoundError:
                self.jobs.put(('error', '未找到 Codex 登录信息'))
            except (urllib.error.URLError, TimeoutError, OSError):
                self.jobs.put(('error', '网络连接失败'))
            except (ValueError, KeyError, TypeError):
                self.jobs.put(('error', '无法读取周额度'))
        threading.Thread(target=worker, daemon=True).start()

    def paint(self):
        self.update_countdown()
        if self.remaining is None:
            self.value_label.config(text='--')
            self.detail_label.config(text=self.error or '正在获取数据…')
            self.status_label.config(text='')
        else:
            self.value_label.config(text=f'{self.remaining:g}%')
            reset = datetime.fromtimestamp(self.reset).strftime('%m 月 %d 日  %H:%M') if self.reset else '未知'
            updated = self.updated.strftime('%H:%M:%S') if self.updated else '--'
            self.detail_label.config(text=f'已用  {100-self.remaining:g}%\n重置时间  {reset}')
            status = ('刷新中…' if self.loading else '更新于  ' + updated)
            if self.error:
                status = self.error + ' · 显示上次数据'
            self.status_label.config(text=status)

    def pump(self):
        win32gui.PumpWaitingMessages()
        try:
            while True:
                action = self.actions.get_nowait()
                if action == 'toggle':
                    self.hide() if self.showing else self.show()
        except queue.Empty:
            pass
        try:
            while True:
                kind, data = self.jobs.get_nowait()
                self.loading = False
                self.button.config(state='normal')
                if kind == 'ok':
                    self.remaining, self.reset = data
                    self.updated = datetime.now()
                    self.error = None
                else: self.error = data
                self.update_icon(stale=bool(self.error))
                self.paint()
                self.timer = self.root.after(REFRESH_SECONDS * 1000, self.refresh)
        except queue.Empty: pass
        self.root.after(100, self.pump)

    def quit(self):
        win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 1))
        if self.icon: win32gui.DestroyIcon(self.icon)
        win32gui.DestroyWindow(self.hwnd)
        try: ICON_PATH.unlink(missing_ok=True)
        except OSError: pass
        self.root.destroy()

    def run(self): self.root.mainloop()

if __name__ == '__main__':
    try:
        App().run()
    except Exception:
        log = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'CodexWeeklyGrid-error.log'
        with log.open('a', encoding='utf-8') as f:
            traceback.print_exc(file=f)
        raise
