"""
Виджет загрузки CPU / RAM / GPU для рабочего стола Windows.

Источники данных:
  CPU, RAM          -> psutil
  GPU (NVIDIA)      -> NVML (nvidia-ml-py): загрузка, видеопамять, температура
  GPU (AMD/Intel)   -> счётчики производительности Windows (те же, что в Диспетчере задач)
  Температура CPU   -> LibreHardwareMonitor (если запущен с включённым веб-сервером)
Если какой-то показатель недоступен, показывается «н/д».

Запуск:          python widget.py
Самопроверка:    python widget.py --selftest
"""

import ctypes
import json
import math
import os
import sys
import threading
import time
import urllib.request
from ctypes import wintypes

import tkinter as tk

import psutil

APP_NAME = "SysMonWidget"
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
DEFAULT_SETTINGS = {"x": None, "y": None, "topmost": True, "alpha": 0.9,
                    "scale": 1.0, "view": "bars", "theme": "graphite", "transparent": False,
                    "net_units": "bits", "show": None}
# Какие показатели показывать (галочки в меню «Показатели»)
DEFAULT_SHOW = {"cpu": True, "ram": True, "gpu": True, "net": True, "disks": True}

UPDATE_MS = 1000          # обновление экрана раз в секунду
LHM_URL = "http://127.0.0.1:8085/data.json"
NA = "н/д"

# ---------- оформление ----------
FONT = "Segoe UI"

# Цветовые темы. accent — цвет нормальной нагрузки, warn/danger — высокой и опасной.
THEMES = {
    "graphite": {"name": "Графит", "bg": "#16181d", "fg": "#e8e9ec", "muted": "#8b919c",
                 "track": "#2a2e36", "accent": "#43c38a", "warn": "#e5b84b",
                 "danger": "#ff4d4d", "needle": "#ff6a4d"},
    "ocean": {"name": "Океан", "bg": "#0d1b2a", "fg": "#e6f1ff", "muted": "#8aa4c2",
              "track": "#1d3149", "accent": "#38bdf8", "warn": "#facc15",
              "danger": "#ff5a5a", "needle": "#fb923c"},
    "lavender": {"name": "Лаванда", "bg": "#1a1428", "fg": "#f1eaff", "muted": "#a596c6",
                 "track": "#2f2645", "accent": "#a78bfa", "warn": "#fcd34d",
                 "danger": "#ff4f6d", "needle": "#f472b6"},
    "teal": {"name": "Бирюза", "bg": "#0c1f1f", "fg": "#e0fbf7", "muted": "#7fb0aa",
             "track": "#1b3936", "accent": "#2dd4bf", "warn": "#fbbf24",
             "danger": "#ff5c5c", "needle": "#fde047"},
    "sakura": {"name": "Сакура", "bg": "#241520", "fg": "#ffe9f4", "muted": "#c792ad",
               "track": "#3d2434", "accent": "#f9a8d4", "warn": "#fcd34d",
               "danger": "#ff3b3b", "needle": "#fb7185"},
    "light": {"name": "Светлая", "bg": "#f4f5f7", "fg": "#16181d", "muted": "#5f6672",
              "track": "#d6d9df", "accent": "#16a34a", "warn": "#c48a00",
              "danger": "#e11d1d", "needle": "#e2552f"},
}


def blend(c1, c2, t):
    """Смешать два цвета: t=1 -> c1, t=0 -> c2."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * t + y * (1 - t)):02x}" for x, y in zip(a, b))


def load_level(p):
    """0 — норма, 1 — высокая нагрузка, 2 — очень высокая."""
    if p is None or p < 60:
        return 0
    return 1 if p < 85 else 2


def temp_level(t, limit):
    """0 — норма, 1 — горячо (до предела <=12 °C), 2 — опасно (до предела <=4 °C)."""
    if t is None or not limit:
        return 0
    if t >= limit - 4:
        return 2
    return 1 if t >= limit - 12 else 0


def cpu_name():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except Exception:
        return ""


def cpu_tjmax(name):
    """Максимальная допустимая температура процессора (Tjmax) по его модели.
    Для Intel точное значение берётся из LibreHardwareMonitor (Distance to TjMax)."""
    n = name.lower()
    if "x3d" in n:
        return 89                      # Ryzen X3D: 89–90 °C
    if "amd fx" in n or "phenom" in n or "athlon ii" in n:
        return 70                      # старые AMD
    if "amd" in n or "ryzen" in n:
        return 95                      # Ryzen / Threadripper / Athlon на Zen
    if "intel" in n:
        return 100
    return 95


def gb(b):
    return b / (1024 ** 3)


# =====================================================================
#                           ИСТОЧНИКИ ДАННЫХ
# =====================================================================

class NvidiaGpu:
    """Видеокарты NVIDIA через официальную библиотеку NVML."""

    def __init__(self):
        import pynvml
        self.nv = pynvml
        pynvml.nvmlInit()
        if pynvml.nvmlDeviceGetCount() < 1:
            raise RuntimeError("Нет устройств NVIDIA")
        self.h = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(self.h)
        self.name = name.decode() if isinstance(name, bytes) else name
        self.temp_limit = None
        # Предел из драйвера: максимальная рабочая температура или порог сброса частот
        for th, delta in (("NVML_TEMPERATURE_THRESHOLD_GPU_MAX", 0),
                          ("NVML_TEMPERATURE_THRESHOLD_SLOWDOWN", -2)):
            try:
                self.temp_limit = pynvml.nvmlDeviceGetTemperatureThreshold(
                    self.h, getattr(pynvml, th)) + delta
                break
            except Exception:
                continue

    def read(self):
        nv, h = self.nv, self.h
        out = {"util": None, "mem_used": None, "mem_total": None, "temp": None,
               "temp_limit": self.temp_limit}
        try:
            out["util"] = float(nv.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:
            pass
        try:
            m = nv.nvmlDeviceGetMemoryInfo(h)
            out["mem_used"], out["mem_total"] = m.used, m.total
        except Exception:
            pass
        try:
            out["temp"] = float(nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU))
        except Exception:
            pass
        return out


class _PdhValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class _PdhItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _PdhValue)]


class WindowsGpu:
    """Любая видеокарта (AMD, Intel, NVIDIA) через счётчики производительности Windows."""

    PDH_FMT_DOUBLE = 0x00000200
    PDH_MORE_DATA = 0x800007D2

    def __init__(self):
        self.pdh = ctypes.WinDLL("pdh")
        self.pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_long
        self.q = ctypes.c_void_p()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.q)) != 0:
            raise OSError("PdhOpenQuery failed")
        self.c_util = self._add(r"\GPU Engine(*engtype_3D)\Utilization Percentage")
        self.c_mem = self._add(r"\GPU Adapter Memory(*)\Dedicated Usage")
        self.pdh.PdhCollectQueryData(self.q)
        self.name, self.mem_total = self._adapter_info()

    def _add(self, path):
        c = ctypes.c_void_p()
        if self.pdh.PdhAddEnglishCounterW(self.q, path, None, ctypes.byref(c)) != 0:
            raise OSError("Счётчик недоступен: " + path)
        return c

    def _values(self, counter):
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        r = self.pdh.PdhGetFormattedCounterArrayW(
            counter, self.PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None)
        if (r & 0xFFFFFFFF) != self.PDH_MORE_DATA:
            return []
        buf = ctypes.create_string_buffer(size.value)
        r = self.pdh.PdhGetFormattedCounterArrayW(
            counter, self.PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), buf)
        if r != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_PdhItem))
        return [(items[i].szName, items[i].FmtValue.doubleValue)
                for i in range(count.value) if items[i].FmtValue.CStatus in (0, 1)]

    @staticmethod
    def _adapter_info():
        """Название и объём видеопамяти — из реестра драйвера."""
        import winreg
        best_name, best_mem = None, None
        key = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key)
        except OSError:
            return None, None
        for i in range(32):
            try:
                sub = winreg.OpenKey(root, "%04d" % i)
            except OSError:
                continue
            try:
                mem = None
                for vname in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                    try:
                        v, _ = winreg.QueryValueEx(sub, vname)
                        mem = int.from_bytes(v, "little") if isinstance(v, bytes) else int(v)
                        break
                    except OSError:
                        pass
                try:
                    name, _ = winreg.QueryValueEx(sub, "DriverDesc")
                except OSError:
                    name = None
                if mem and (best_mem is None or mem > best_mem):
                    best_name, best_mem = name, mem
            finally:
                winreg.CloseKey(sub)
        return best_name, best_mem

    def read(self):
        out = {"util": None, "mem_used": None, "mem_total": self.mem_total, "temp": None}
        if self.pdh.PdhCollectQueryData(self.q) != 0:
            return out
        util = self._values(self.c_util)
        if util:
            out["util"] = min(100.0, sum(v for _, v in util))
        mem = self._values(self.c_mem)
        if mem:
            out["mem_used"] = max(v for _, v in mem)
        return out


def _num(text):
    try:
        return float(str(text).split()[0].replace(",", "."))
    except Exception:
        return None


def read_lhm_temps():
    """Температуры из LibreHardwareMonitor (веб-сервер на порту 8085)."""
    with urllib.request.urlopen(LHM_URL, timeout=0.5) as r:
        data = json.load(r)

    cpu_temps, gpu_temps = [], []

    def walk(node, kind):
        img = str(node.get("ImageURL", "")).lower()
        if "cpu" in img:
            kind = "cpu"
        elif any(k in img for k in ("nvidia", "ati", "amd", "intel", "gpu")):
            kind = "gpu"
        if node.get("Text") == "Temperatures" and kind:
            for s in node.get("Children", []):
                v = _num(s.get("Value"))
                if v is not None:
                    (cpu_temps if kind == "cpu" else gpu_temps).append((s.get("Text", ""), v))
        for ch in node.get("Children", []):
            walk(ch, kind)

    walk(data, None)

    def pick(temps, preferred):
        temps = [(n, v) for n, v in temps if "distance" not in n.lower()]
        for p in preferred:
            for name, v in temps:
                if p in name.lower():
                    return v
        return max((v for _, v in temps), default=None)

    # Intel: Tjmax = температура ядра + «Distance to TjMax»
    tjmax = None
    names = dict(cpu_temps)
    for name, dist in cpu_temps:
        if "distance to tjmax" in name.lower():
            base = names.get(name[:name.lower().index(" distance")])
            if base is not None:
                tjmax = round(base + dist)
                break

    return {"cpu": pick(cpu_temps, ("tctl", "tdie", "package", "core average")),
            "gpu": pick(gpu_temps, ("gpu core", "core")),
            "cpu_tjmax": tjmax}


class Collector(threading.Thread):
    """Собирает данные в фоне, чтобы окно никогда не «подвисало»."""

    def __init__(self):
        super().__init__(daemon=True)
        self.data = {}
        self.gpu = None
        self.gpu_kind = None
        self._gpu_retry = 0
        self._lhm_next = 0
        self._lhm = {}
        self.cpu_tjmax = cpu_tjmax(cpu_name())
        self._nics, self._nics_next = set(), 0
        self._net_prev = None
        self.show = DEFAULT_SHOW                  # окно подменяет своими галочками
        self._parts, self._parts_next, self._disks_next = [], 0, 0
        self._disks = self.read_disks(time.monotonic())
        self.data = {"disks": self._disks}       # диски известны сразу — для высоты окна
        psutil.cpu_percent(None)  # первый вызов всегда 0 — «разогреваем»

    def init_gpu(self):
        for cls, kind in ((NvidiaGpu, "NVIDIA (NVML)"), (WindowsGpu, "Windows PDH")):
            try:
                self.gpu, self.gpu_kind = cls(), kind
                return
            except Exception:
                continue
        self.gpu, self.gpu_kind = None, None

    def sample(self):
        d = {}
        try:
            d["cpu"] = psutil.cpu_percent(None)
        except Exception:
            d["cpu"] = None
        try:
            vm = psutil.virtual_memory()
            d["ram"], d["ram_used"], d["ram_total"] = vm.percent, vm.total - vm.available, vm.total
        except Exception:
            d["ram"] = d["ram_used"] = d["ram_total"] = None

        now = time.monotonic()
        if self.gpu is None and now >= self._gpu_retry:
            self.init_gpu()
            self._gpu_retry = now + 30
        g = {}
        if self.gpu is not None:
            try:
                g = self.gpu.read()
            except Exception:
                self.gpu = None
        d.update({"gpu": g.get("util"), "vram_used": g.get("mem_used"),
                  "vram_total": g.get("mem_total"), "gpu_temp": g.get("temp"),
                  "gpu_temp_limit": g.get("temp_limit")})

        # LibreHardwareMonitor: опрашиваем раз в 2 с; если не запущен — раз в 5 с
        if now >= self._lhm_next:
            try:
                self._lhm = read_lhm_temps()
                self._lhm_next = now + 2
            except Exception:
                self._lhm = {}
                self._lhm_next = now + 5
        d["cpu_temp"] = self._lhm.get("cpu")
        d["cpu_temp_limit"] = self._lhm.get("cpu_tjmax") or self.cpu_tjmax
        if d["gpu_temp"] is None:
            d["gpu_temp"] = self._lhm.get("gpu")
        if not d["gpu_temp_limit"]:
            d["gpu_temp_limit"] = 90 if d["gpu_temp"] is not None else None

        if self.show.get("net", True):
            d["net_down"], d["net_up"] = self.net_speed(now)
        else:                                       # выключено в меню — не опрашиваем
            self._net_prev = None
            d["net_down"] = d["net_up"] = None

        if now >= self._disks_next:                 # место на дисках — раз в 5 с
            self._disks = self.read_disks(now)
            self._disks_next = now + 5
        d["disks"] = self._disks
        return d

    def read_disks(self, now):
        """Локальные диски (C:, D:, …): объём, занято, свободно. Флешки и DVD не показываем."""
        try:
            if now >= self._parts_next:             # список дисков — раз в 30 с
                self._parts = [p.mountpoint for p in psutil.disk_partitions(all=False)
                               if "fixed" in p.opts.split(",")]
                self._parts_next = now + 30
        except Exception:
            pass
        disks = []
        for mp in self._parts:
            try:
                u = psutil.disk_usage(mp)
            except Exception:
                continue
            disks.append({"name": mp.rstrip("\\/"), "total": u.total, "used": u.used,
                          "free": u.free, "percent": u.percent})
        return disks

    # Виртуальные адаптеры дублируют трафик реального — их не считаем
    VIRTUAL_NICS = ("loopback", "vethernet", "vmware", "virtualbox", "hyper-v", "wsl",
                    "bluetooth", "pseudo")

    def net_speed(self, now):
        """Скорость загрузки и выгрузки (байт/с) по всем реальным сетевым адаптерам."""
        try:
            if now >= self._nics_next:              # список адаптеров — раз в 30 с
                stats = psutil.net_if_stats()
                self._nics = {n for n, st in stats.items() if st.isup and
                              not any(v in n.lower() for v in self.VIRTUAL_NICS)}
                self._nics_next = now + 30
            counters = psutil.net_io_counters(pernic=True)
            recv = sum(c.bytes_recv for n, c in counters.items() if n in self._nics)
            sent = sum(c.bytes_sent for n, c in counters.items() if n in self._nics)
        except Exception:
            self._net_prev = None
            return None, None
        prev, self._net_prev = self._net_prev, (now, recv, sent)
        if prev is None or now - prev[0] <= 0:
            return None, None
        dt = now - prev[0]
        # счётчики сбрасываются при переподключении адаптера — отрицательное считаем нулём
        return max(0, recv - prev[1]) / dt, max(0, sent - prev[2]) / dt

    def run(self):
        while True:
            self.data = self.sample()
            time.sleep(1)


# =====================================================================
#                                 ОКНО
# =====================================================================

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    show = dict(DEFAULT_SHOW)
    if isinstance(s.get("show"), dict):
        show.update({k: bool(v) for k, v in s["show"].items() if k in show})
    if "show_disks" in s:                         # настройка из версии 1.2
        show["disks"] = bool(s.pop("show_disks"))
    if not any(show.values()):
        show = dict(DEFAULT_SHOW)
    s["show"] = show
    return s


def save_settings(s):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# Виды. Размеры окна считаются в layout() по включённым показателям.
VIEWS = ("bars", "gauges")
DISK_ROW = {"bars": 28, "gauges": 20}     # высота строки одного диска (базовые пиксели)
MIN_SCALE, MAX_SCALE = 0.6, 3.0
METRICS = (("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU"))
SHOW_ITEMS = (("cpu", "CPU"), ("ram", "RAM (оперативная память)"), ("gpu", "GPU (видеокарта)"),
              ("net", "Скорость интернета"), ("disks", "Диски"))


# ---------- автозапуск вместе с Windows ----------
# Запись в реестре текущего пользователя (как у Steam, Discord и т.п.): права администратора
# не нужны, запись видна в Диспетчере задач на вкладке «Автозагрузка».
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup", APP_NAME + ".lnk")


def autostart_command():
    """Команда запуска: сам .exe или pythonw.exe + widget.py (если запущено из исходников)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")   # без чёрного окна консоли
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_get():
    """Текущая команда автозапуска или None, если автозапуск выключен."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_NAME)[0]
    except OSError:
        return None


def autostart_enabled():
    # ярлык в папке «Автозагрузка» — от add_to_autostart.bat из прошлых версий
    return autostart_get() is not None or os.path.exists(STARTUP_LNK)


def autostart_set(on):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass
    if not on and os.path.exists(STARTUP_LNK):
        try:
            os.remove(STARTUP_LNK)
        except OSError:
            pass


def autostart_refresh():
    """Если автозапуск включён, а .exe перенесли в другую папку — обновляем путь."""
    cur = autostart_get()
    if cur is not None and getattr(sys, "frozen", False) and cur != autostart_command():
        try:
            autostart_set(True)
        except OSError:
            pass


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class Widget:
    def __init__(self):
        self.s = load_settings()
        if self.s.get("view") not in VIEWS:
            self.s["view"] = "bars"
        if self.s.get("theme") not in THEMES:
            self.s["theme"] = "graphite"
        self.scale = clamp(float(self.s.get("scale") or 1.0), MIN_SCALE, MAX_SCALE)

        self.root = tk.Tk()
        self.root.title("Системный монитор")
        self.root.overrideredirect(True)          # без рамки
        self.root.attributes("-topmost", bool(self.s["topmost"]))
        self.root.attributes("-alpha", float(self.s["alpha"]))
        self.dpi = self.root.winfo_fpixels("1i") / 96.0

        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self.target, self.shown, self.lines = {}, {}, {}
        self.hover = False
        self._anim_id = None
        self._press = None

        self.collector = Collector()              # заранее: высота окна зависит от числа дисков
        self.collector.show = self.s["show"]      # выключенное не опрашиваем
        self._disk_n = self.disk_count()
        self.W, self.H = self.size_for(self.scale)
        self.place_window()

        self.topmost_var = tk.BooleanVar(value=bool(self.s["topmost"]))
        autostart_refresh()
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self.alpha_var = tk.DoubleVar(value=float(self.s["alpha"]))
        self.view_var = tk.StringVar(value=self.s["view"])
        self.theme_var = tk.StringVar(value=self.s["theme"])
        self.transparent_var = tk.BooleanVar(value=bool(self.s["transparent"]))
        self.net_units_var = tk.StringVar(value=self.s.get("net_units") or "bits")
        self.show_vars = {key: tk.BooleanVar(value=self.s["show"][key]) for key, _ in SHOW_ITEMS}
        self.menu = self.build_menu()

        c = self.canvas
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Motion>", self.on_motion)
        c.bind("<Button-3>", self.show_menu)

        self.collector.start()
        self.apply_style()
        self.root.after(300, self.refresh)
        self.root.after(500, self.poll_hover)
        self.root.after(400, self.keep_on_screen)

    # ----- оформление: тема и прозрачный фон -----
    def apply_style(self):
        t = self.t = THEMES[self.s["theme"]]
        self.transparent = bool(self.s["transparent"])
        bg = t["bg"]
        # обводка текста в прозрачном режиме: почти цвет фона темы, но не точно он
        # (точный цвет фона становится «дыркой» в окне)
        r, g, b = (int(bg[i:i + 2], 16) for i in (1, 3, 5))
        self.outline = f"#{r:02x}{g:02x}{b + 1 if b < 255 else b - 1:02x}"
        self.zones = ((0, 60, blend(t["accent"], bg, 0.45)),
                      (60, 85, blend(t["warn"], bg, 0.45)),
                      (85, 100, blend(t["danger"], bg, 0.45)))
        self.root.configure(bg=bg)
        self.canvas.configure(bg=bg)
        # Цвет фона темы становится полностью прозрачным (видно рабочий стол)
        self.root.attributes("-transparentcolor", bg if self.transparent else "")
        self.set_dwm_frame(not self.transparent)
        self.refresh_lines()                       # подписи в цветах новой темы
        self.redraw()

    def set_dwm_frame(self, visible):
        """Windows 11: скруглённые углы и тонкая рамка — только для обычного фона."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            dwm = ctypes.windll.dwmapi
            corner = ctypes.c_int(2 if visible else 1)        # ROUND / DONOTROUND
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner), 4)
            border = ctypes.c_uint(0xFFFFFFFF if visible else 0xFFFFFFFE)  # DEFAULT / NONE
            dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border), 4)
        except Exception:
            pass

    def color_for_load(self, p):
        return (self.t["accent"], self.t["warn"], self.t["danger"])[load_level(p)]

    def temp_segment(self, temp, limit):
        """Температура: обычная, жёлтая (горячо) или красная жирная (опасно)."""
        lvl = temp_level(temp, limit)
        color = (self.t["muted"], self.t["warn"], self.t["danger"])[lvl]
        return (fmt_temp(temp), color, lvl == 2)

    # ----- размер и позиция -----
    def disk_count(self):
        if not self.s["show"]["disks"]:
            return 0
        return len(self.collector.data.get("disks") or [])

    def layout(self):
        """Раскладка по включённым показателям, в базовых пикселях (масштаб 100%).
        Возвращает ширину, высоту и словарь: где рисовать основные показатели,
        строку сети и блок дисков."""
        view, show = self.s["view"], self.s["show"]
        main = [(key, title) for key, title in METRICS if show[key]]
        n_disks = self.disk_count()
        L = {"main": [], "net": None, "disks": None}
        if view == "bars":
            bw, y = 230, 8
            for key, title in main:                    # строки CPU / RAM / GPU друг за другом
                L["main"].append((key, title, y))
                y += 56
            if show["net"]:
                L["net"] = y - 2 if main else 8
                y = L["net"] + 22
            if n_disks:
                L["disks"] = y + 2 if (main or show["net"]) else 12
                y = L["disks"] + n_disks * DISK_ROW[view] + 6
            elif not show["net"]:
                y += 6
            return bw, y, L

        # спидометры: столько шкал, сколько включено; ширина подстраивается
        n = len(main)
        bw = 120 * n
        if show["net"]:
            bw = max(bw, 240)
        if n_disks:
            bw = max(bw, 300)
        off = (bw - 120 * n) / 2
        for i, (key, title) in enumerate(main):
            L["main"].append((key, title, off + 60 + 120 * i))
        gpu_in = show["gpu"]
        y = (146 if gpu_in else 132) if n else 0
        net_row = False
        if show["net"]:
            cols = n - 1 if gpu_in else n              # свободные колонки в нижней строке
            if cols >= 2:
                # в нижней строке под шкалами, левее строки VRAM (GPU — всегда последняя шкала)
                L["net"] = (off + 60 * cols, 124)
                y = 146
            else:                                      # места нет — отдельной строкой
                ny = (y if n else 4) + 9
                L["net"] = (bw / 2, ny)
                y, net_row = ny + 13, True
        if n_disks:
            L["disks"] = (y + 4 if net_row else y - 4) if y else 10
            y = L["disks"] + n_disks * DISK_ROW[view] + 6
        return bw, y, L

    def size_for(self, scale):
        bw, bh, self.L = self.layout()
        self.bw = bw
        k = scale * self.dpi
        return round(bw * k), round(bh * k)

    def place_window(self):
        sw = self.root.winfo_screenwidth()
        x, y = self.s.get("x"), self.s.get("y")
        # «виртуальный экран» — все мониторы вместе
        u = ctypes.windll.user32
        vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        vw, vh = u.GetSystemMetrics(78) or sw, u.GetSystemMetrics(79) or 1000
        if (x is None or y is None or not (vx <= x <= vx + vw - 40)
                or not (vy <= y <= vy + vh - 40)):
            x, y = sw - self.W - 20, 20          # по умолчанию: правый верхний угол
        self.root.geometry(f"{self.W}x{self.H}+{int(x)}+{int(y)}")

    def save_geometry(self):
        self.s["x"], self.s["y"] = self.root.winfo_x(), self.root.winfo_y()
        self.s["scale"] = round(self.scale, 3)
        save_settings(self.s)

    # ----- мышь: перетаскивание и изменение размера за углы -----
    def corner_at(self, x, y):
        z = max(10, 16 * self.scale * self.dpi)
        v = "n" if y < z else "s" if y > self.H - z else ""
        h = "w" if x < z else "e" if x > self.W - z else ""
        return v + h if v and h else None

    def on_motion(self, e):
        corner = self.corner_at(e.x, e.y)
        cursor = {"nw": "size_nw_se", "se": "size_nw_se",
                  "ne": "size_ne_sw", "sw": "size_ne_sw"}.get(corner, "")
        if self.canvas.cget("cursor") != cursor:
            self.canvas.config(cursor=cursor)

    def poll_hover(self):
        """Показываем уголок, только пока курсор над виджетом.
        Положение курсора сверяем с прямоугольником окна (5 раз в секунду): на прозрачном
        фоне Windows не сообщает окну о курсоре над пустыми местами, а так уголок
        не пропадёт, пока к нему ведут мышь через прозрачную часть."""
        try:
            px, py = self.root.winfo_pointerxy()
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            inside = x <= px < x + self.W and y <= py < y + self.H
            if self._press is None and inside != self.hover:
                self.hover = inside
                self.canvas.delete("grip")
                if inside:
                    self.draw_grip()
        except Exception:
            pass
        self.root.after(200, self.poll_hover)

    def on_press(self, e):
        self._press = (self.corner_at(e.x, e.y), e.x_root, e.y_root,
                       self.root.winfo_x(), self.root.winfo_y(), self.W, self.H, self.scale)

    def on_drag(self, e):
        if not self._press:
            return
        corner, mx, my, x0, y0, w0, h0, scale0 = self._press
        dx, dy = e.x_root - mx, e.y_root - my
        if corner is None:                       # обычное перетаскивание
            self.root.geometry(f"+{x0 + dx}+{y0 + dy}")
            return
        fx = 1 if "e" in corner else -1          # в какую сторону растёт окно
        fy = 1 if "s" in corner else -1
        dw = (fx * dx + fy * dy * w0 / h0) / 2   # пропорции сохраняются
        self.scale = clamp(scale0 * (w0 + dw) / w0, MIN_SCALE, MAX_SCALE)
        w, h = self.size_for(self.scale)
        x = x0 if fx > 0 else x0 + w0 - w        # противоположный угол стоит на месте
        y = y0 if fy > 0 else y0 + h0 - h
        self.W, self.H = w, h
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.redraw()

    def on_release(self, _e):
        self._press = None
        self.save_geometry()

    # ----- меню -----
    def build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        m.add_checkbutton(label="Всегда поверх окон", variable=self.topmost_var,
                          command=self.toggle_topmost)
        m.add_checkbutton(label="Запускать вместе с Windows", variable=self.autostart_var,
                          command=self.toggle_autostart)
        views = tk.Menu(m, tearoff=0)
        views.add_radiobutton(label="Полоски", value="bars", variable=self.view_var,
                              command=self.set_view)
        views.add_radiobutton(label="Спидометры", value="gauges", variable=self.view_var,
                              command=self.set_view)
        m.add_cascade(label="Вид", menu=views)
        items = tk.Menu(m, tearoff=0)
        for key, label in SHOW_ITEMS:
            items.add_checkbutton(label=label, variable=self.show_vars[key],
                                  command=lambda k=key: self.toggle_show(k))
        m.add_cascade(label="Показатели", menu=items)
        themes = tk.Menu(m, tearoff=0)
        for key, th in THEMES.items():
            themes.add_radiobutton(label=th["name"], value=key, variable=self.theme_var,
                                   command=self.set_theme)
        m.add_cascade(label="Цвет", menu=themes)
        m.add_checkbutton(label="Прозрачный фон", variable=self.transparent_var,
                          command=self.toggle_transparent)
        sub = tk.Menu(m, tearoff=0)
        for pct in (100, 90, 80, 70, 60, 50, 40):
            sub.add_radiobutton(label=f"{pct}%", value=pct / 100, variable=self.alpha_var,
                                command=self.set_alpha)
        m.add_cascade(label="Прозрачность", menu=sub)
        net = tk.Menu(m, tearoff=0)
        net.add_radiobutton(label="Мбит/с (как у провайдера)", value="bits",
                            variable=self.net_units_var, command=self.set_net_units)
        net.add_radiobutton(label="МБ/с (как при скачивании)", value="bytes",
                            variable=self.net_units_var, command=self.set_net_units)
        m.add_cascade(label="Скорость сети в", menu=net)
        m.add_command(label="Сбросить размер", command=self.reset_size)
        m.add_separator()
        m.add_command(label="Закрыть", command=self.close)
        return m

    def show_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def toggle_autostart(self):
        try:
            autostart_set(bool(self.autostart_var.get()))
        except OSError:
            pass
        self.autostart_var.set(autostart_enabled())   # показываем то, что реально вышло

    def toggle_topmost(self):
        self.s["topmost"] = bool(self.topmost_var.get())
        self.root.attributes("-topmost", self.s["topmost"])
        save_settings(self.s)

    def set_alpha(self):
        self.s["alpha"] = round(float(self.alpha_var.get()), 2)
        self.root.attributes("-alpha", self.s["alpha"])
        save_settings(self.s)

    def set_theme(self):
        self.s["theme"] = self.theme_var.get()
        save_settings(self.s)
        self.apply_style()

    def toggle_show(self, key):
        """Галочка в меню «Показатели». Последнюю галочку снять нельзя — окно стало бы пустым."""
        value = bool(self.show_vars[key].get())
        if not value and sum(self.s["show"].values()) <= 1:
            self.show_vars[key].set(True)
            return
        self.s["show"][key] = value
        self._disk_n = self.disk_count()
        self.resize_to(self.scale)

    def set_net_units(self):
        self.s["net_units"] = self.net_units_var.get()
        save_settings(self.s)
        self.refresh_lines()
        self.redraw("values")

    def toggle_transparent(self):
        self.s["transparent"] = bool(self.transparent_var.get())
        self.hover = False
        save_settings(self.s)
        self.apply_style()

    def resize_to(self, scale):
        self.scale = scale
        self.W, self.H = self.size_for(scale)
        self.root.geometry(f"{self.W}x{self.H}")
        self.redraw()
        self.keep_on_screen()
        self.save_geometry()

    def keep_on_screen(self):
        """Если окно (например, выросшее из-за дисков) вылезает за рабочую область
        своего монитора или под панель задач — сдвигаем его внутрь."""
        try:
            self.root.update_idletasks()
            u = ctypes.windll.user32
            hwnd = u.GetParent(self.root.winfo_id())
            mon = u.MonitorFromWindow(hwnd, 2)               # MONITOR_DEFAULTTONEAREST
            info = _MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if not u.GetMonitorInfoW(mon, ctypes.byref(info)):
                return
            wa = info.rcWork
            x, y = self.root.winfo_x(), self.root.winfo_y()
            nx = max(wa.left, min(x, wa.right - self.W))
            ny = max(wa.top, min(y, wa.bottom - self.H))
            if (nx, ny) != (x, y):
                self.root.geometry(f"+{nx}+{ny}")
                self.save_geometry()
        except Exception:
            pass

    def set_view(self):
        self.s["view"] = self.view_var.get()
        self.resize_to(self.scale)

    def reset_size(self):
        self.resize_to(1.0)

    def close(self):
        self.save_geometry()
        self.root.withdraw()
        # Настройки уже сохранены. Завершаем процесс сразу: обычный destroy() после
        # контекстного меню в Windows может «висеть» в памяти ещё несколько секунд.
        os._exit(0)

    # ----- данные и плавная анимация -----
    def refresh(self):
        try:
            d = self.collector.data
            self.target = {"cpu": d.get("cpu"), "ram": d.get("ram"), "gpu": d.get("gpu")}
            self.refresh_lines()
            n = self.disk_count()
            if n != self._disk_n:                 # подключили/отключили диск — меняем высоту
                self._disk_n = n
                self.resize_to(self.scale)
            self.animate(values=True)
        except Exception:
            pass  # никогда не падаем из-за отображения
        self.root.after(UPDATE_MS, self.refresh)

    def refresh_lines(self):
        """Подписи под показателями. Каждая — список кусочков (текст, цвет, жирный)."""
        d = self.collector.data
        m = self.t["muted"]
        cpu_t = self.temp_segment(d.get("cpu_temp"), d.get("cpu_temp_limit"))
        gpu_t = self.temp_segment(d.get("gpu_temp"), d.get("gpu_temp_limit"))
        ram = (fmt_mem(d.get("ram_used"), d.get("ram_total")), m, False)
        vram = fmt_mem(d.get("vram_used"), d.get("vram_total"))
        units = self.s.get("net_units") or "bits"
        net = [("▼ ", self.t["accent"], False), (fmt_speed(d.get("net_down"), units), m, False),
               ("    ▲ ", self.t["needle"], False), (fmt_speed(d.get("net_up"), units), m, False)]
        self.lines = {
            "net": net,
            "bars": {"cpu": [("Температура: ", m, False), cpu_t],
                     "ram": [ram],
                     "gpu": [gpu_t, (f"  ·  VRAM {vram}", m, False)]},
            "gauges": {"cpu": ([("Темп. ", m, False), cpu_t], []),
                       "ram": ([ram], []),
                       "gpu": ([gpu_t], [(f"VRAM {vram}", m, False)])},
        }

    def animate(self, values=False):
        """Стрелки и полосы плавно «доезжают» до нового значения (~0.3 с).
        Подписи (values=True) перерисовываются только раз в секунду."""
        if self._anim_id:
            self.root.after_cancel(self._anim_id)
            self._anim_id = None
        moving = False
        for key, t in self.target.items():
            cur = self.shown.get(key)
            if t is None or cur is None or abs(t - cur) < 1.5:
                self.shown[key] = t
            else:
                self.shown[key] = cur + (t - cur) * 0.6
                moving = True
        self.redraw("values" if values else "anim")
        if moving:
            self._anim_id = self.root.after(40, self.animate)

    # ----- рисование -----
    def font(self, px, bold=False):
        size = -max(6, round(px * self.k))      # минус = размер в пикселях
        return (FONT, size, "bold") if bold else (FONT, size)

    def text(self, x, y, text, fill, font, anchor="center", tags=None):
        """Текст; на прозрачном фоне — с тонкой обводкой, чтобы читался на любых обоях."""
        c, ids = self.canvas, []
        if self.transparent:
            o = max(1, round(self.k))
            for dx, dy in ((-o, 0), (o, 0), (0, -o), (0, o)):
                ids.append(c.create_text(x + dx, y + dy, text=text, fill=self.outline,
                                         font=font, anchor=anchor, tags=tags))
        ids.append(c.create_text(x, y, text=text, fill=fill, font=font, anchor=anchor,
                                 tags=tags))
        return ids

    def segments(self, x, y, segs, px, anchor="nw", align="left", tags="val"):
        """Строка из кусочков разного цвета (например, красная температура).
        align: "left" — начинается в x, "center" — по центру x, "right" — заканчивается в x."""
        c, items, cx = self.canvas, [], x
        for text, color, bold in segs:
            if not text:
                continue
            ids = self.text(cx, y, text, color, self.font(px, bold), anchor=anchor, tags=tags)
            items += ids
            bb = c.bbox(ids[-1])
            if bb:
                cx = bb[2]
        shift = {"center": -(cx - x) / 2, "right": -(cx - x)}.get(align, 0)
        if shift and items:
            for i in items:
                c.move(i, shift, 0)

    def redraw(self, part="all"):
        """Перерисовка по частям, чтобы виджет почти не нагружал процессор:
          "all"    — всё (смена размера, вида, темы);
          "values" — подписи и стрелки (раз в секунду);
          "anim"   — только стрелки и полосы (кадры плавной анимации).
        Неподвижные элементы (шкала, деления, названия) рисуются только при "all"."""
        if not hasattr(self, "t"):
            return
        c = self.canvas
        self.full = part == "all"
        self.vals = part != "anim"
        if part == "all":
            c.delete("all")
        elif part == "values":
            c.delete("val", "anim")
        else:
            c.delete("anim")
        self.k = self.W / self.bw
        try:
            if self.s["view"] == "gauges":
                self.draw_gauges()
            else:
                self.draw_bars()
            if self.disk_count():
                self.draw_disks()
        except Exception:
            pass
        if part == "anim":
            c.tag_raise("val")                     # подписи поверх стрелок
        if self.full and self.hover:
            self.draw_grip()

    def draw_bars(self):
        c, k, t = self.canvas, self.k, self.t
        lines = self.lines.get("bars", {})
        pad = 12 * k
        for key, title, y in self.L["main"]:
            y0 = y * k
            value, shown = self.target.get(key), self.shown.get(key)
            by, bh = y0 + 37 * k, max(2, 5 * k)
            if self.full:
                self.text(pad, y0, title, t["fg"], self.font(13, True), anchor="nw")
                c.create_rectangle(pad, by, self.W - pad, by + bh, fill=t["track"], width=0)
            if self.vals:
                self.text(self.W - pad, y0, fmt_pct(value), t["fg"], self.font(13, True),
                          anchor="ne", tags="val")
                self.segments(pad, y0 + 19 * k, lines.get(key, []), 11)
            if shown:
                p = clamp(shown, 0, 100) / 100
                c.create_rectangle(pad, by, pad + (self.W - 2 * pad) * p, by + bh,
                                   fill=self.color_for_load(value), width=0, tags="anim")
        # строка «Сеть» внизу
        if self.L["net"] is None:
            return
        ny = self.L["net"] * k
        if self.full:
            self.text(pad, ny, "Сеть", t["muted"], self.font(11, True), anchor="nw")
        if self.vals:
            self.segments(self.W - pad, ny, self.lines.get("net", []), 11, align="right")

    def draw_disks(self):
        """Диски: буква, полоса занятости и «занято / объём · свободно».
        Обновляются раз в секунду вместе с подписями (на кадрах анимации не трогаем)."""
        if not (self.full or self.vals):
            return
        c, k, t = self.canvas, self.k, self.t
        view = self.s["view"]
        disks = self.collector.data.get("disks") or []
        pad, top, row = 12 * k, self.L["disks"] * k, DISK_ROW[view] * k
        if self.full and self.L["disks"] > 20:              # тонкий разделитель
            c.create_line(pad, top - 5 * k, self.W - pad, top - 5 * k, fill=t["track"],
                          width=max(1, k))
        for i, dk in enumerate(disks):
            used, free = fmt_disk(dk)
            lvl = disk_level(dk["percent"])
            col = (t["accent"], t["warn"], t["danger"])[lvl]
            free_col = (t["muted"], t["warn"], t["danger"])[lvl]
            text = [(used, t["muted"], False), ("  ·  ", t["muted"], False),
                    (free, free_col, lvl == 2)]
            if view == "gauges":                            # одна строка: C:  ▬▬▬  315 / 465 ГБ · своб.
                y = top + i * row + row / 2
                x0, x1, bh = 38 * k, (self.bw - 170) * k, max(2, 5 * k)
                if self.full:
                    self.text(pad, y, dk["name"], t["fg"], self.font(11, True), anchor="w")
                    c.create_rectangle(x0, y - bh / 2, x1, y + bh / 2, fill=t["track"], width=0)
                if self.vals:
                    c.create_rectangle(x0, y - bh / 2, x0 + (x1 - x0) * dk["percent"] / 100,
                                       y + bh / 2, fill=col, width=0, tags="val")
                    self.segments(self.W - pad, y, text, 10, anchor="w", align="right")
            else:                                           # две строки: текст, под ним полоса
                y = top + i * row
                by, bh = y + 17 * k, max(2, 4 * k)
                if self.full:
                    self.text(pad, y, dk["name"], t["fg"], self.font(11, True), anchor="nw")
                    c.create_rectangle(pad, by, self.W - pad, by + bh, fill=t["track"], width=0)
                if self.vals:
                    c.create_rectangle(pad, by, pad + (self.W - 2 * pad) * dk["percent"] / 100,
                                       by + bh, fill=col, width=0, tags="val")
                    self.segments(self.W - pad, y + 1 * k, text, 10, align="right")

    def draw_gauges(self):
        k = self.k
        lines = self.lines.get("gauges", {})
        for key, title, x in self.L["main"]:
            cx, cy, r = x * k, 58 * k, 42 * k
            self.draw_gauge(cx, cy, r, title, self.target.get(key), self.shown.get(key),
                            lines.get(key, ([], [])))
        # скорость сети — в нижней строке левее VRAM, либо отдельной строкой
        if self.vals and self.L["net"]:
            nx, ny = self.L["net"]
            self.segments(nx * k, ny * k, self.lines.get("net", []), 10,
                          anchor="w", align="center")

    def draw_gauge(self, cx, cy, r, title, value, shown, lines):
        """Спидометр: шкала 240°, от 0% (слева внизу) до 100% (справа внизу)."""
        c, k, t = self.canvas, self.k, self.t
        th = max(2, 6 * k)                                   # толщина дуги

        def pt(angle_deg, radius):
            a = math.radians(angle_deg)
            return cx + radius * math.cos(a), cy - radius * math.sin(a)

        def angle(pct):
            return 210 - clamp(pct, 0, 100) * 2.4

        box = (cx - r, cy - r, cx + r, cy + r)
        if self.full:
            # цветные зоны снаружи
            zr = r + th / 2 + 3 * k
            for a, b, col in self.zones:
                c.create_arc(cx - zr, cy - zr, cx + zr, cy + zr, start=angle(b),
                             extent=(b - a) * 2.4, style="arc", width=max(1, 2 * k), outline=col)
            # дорожка
            c.create_arc(*box, start=-30, extent=240, style="arc", width=th, outline=t["track"])
            # деления: крупные через 20%, мелкие через 10%
            for i in range(11):
                major = i % 2 == 0
                r1 = r - th / 2 - 2 * k
                r2 = r1 - (6 if major else 3) * k
                c.create_line(*pt(angle(i * 10), r1), *pt(angle(i * 10), r2), fill=t["muted"],
                              width=max(1, (1.6 if major else 1) * k))
            self.text(cx, cy + r * 0.88, title, t["muted"], self.font(10, True))
        # заполнение дуги
        if shown and shown >= 0.5:
            c.create_arc(*box, start=210, extent=-clamp(shown, 0, 100) * 2.4, style="arc",
                         width=th, outline=self.color_for_load(value), tags="anim")
        # стрелка
        a = math.radians(angle(shown or 0))
        dx, dy = math.cos(a), -math.sin(a)                   # направление стрелки
        px, py = -dy, dx                                     # перпендикуляр
        L, tail, w = r * 0.78, r * 0.14, 2.4 * k
        needle_col = t["needle"] if shown is not None else t["muted"]
        c.create_polygon(cx + dx * L, cy + dy * L,
                         cx + px * w, cy + py * w,
                         cx - dx * tail, cy - dy * tail,
                         cx - px * w, cy - py * w,
                         fill=needle_col, outline="", tags="anim")
        hr = 4.5 * k
        c.create_oval(cx - hr, cy - hr, cx + hr, cy + hr, fill=t["track"],
                      outline=needle_col, width=max(1, 1.5 * k), tags="anim")
        # подписи (раз в секунду)
        if self.vals:
            self.text(cx, cy + r * 0.45, fmt_pct(value), t["fg"], self.font(15, True),
                      tags="val")
            self.segments(cx, cy + r + 10 * k, lines[0], 10, anchor="w", align="center")
            if lines[1]:
                self.segments(cx, cy + r + 24 * k, lines[1], 10, anchor="w", align="center")

    def draw_grip(self):
        """Уголок в правом нижнем углу (виден при наведении): «за угол можно тянуть».
        На прозрачном фоне это закрашенный треугольник — за него удобно ухватиться."""
        c, k = self.canvas, self.k
        if self.transparent:
            s = max(12, 16 * k)
            c.create_polygon(self.W, self.H, self.W - s, self.H, self.W, self.H - s,
                             fill=blend(self.t["muted"], self.t["bg"], 0.55), outline="",
                             tags="grip")
            return
        x, y = self.W - 4 * k, self.H - 4 * k
        col = blend(self.t["muted"], self.t["bg"], 0.5)
        for d in (4, 8, 12):
            c.create_line(x - d * k, y, x, y - d * k, fill=col, width=max(1, k), tags="grip")

    def run(self):
        self.root.mainloop()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def fmt_pct(p):
    return NA if p is None else f"{p:.0f}%"


def fmt_temp(t):
    return NA if t is None else f"{t:.0f} °C"


def fmt_speed(bps, units="bits"):
    """Скорость сети: Мбит/с (как у провайдера) или МБ/с (как при скачивании)."""
    if bps is None:
        return NA
    if units == "bits":
        v, small, big = bps * 8 / 1000, "Кбит/с", "Мбит/с"
    else:
        v, small, big = bps / 1024, "КБ/с", "МБ/с"
    if round(v) < 1000:
        return f"{v:.0f} {small}"
    v /= 1000 if units == "bits" else 1024
    return f"{v:.1f} {big}" if round(v, 1) < 100 else f"{v:.0f} {big}"


def fmt_disk(dk):
    """«315 / 465 ГБ · своб. 150 ГБ» (единицы — по общему объёму диска)."""
    unit = "ГБ" if round(dk["total"] / 2**30) < 1000 else "ТБ"
    conv = (lambda b: f"{b / 2**30:.0f}") if unit == "ГБ" else (lambda b: f"{b / 2**40:.1f}")
    free = dk["free"] / 2**30
    free_s = f"{free:.0f} ГБ" if round(free) < 1000 else f"{free / 1024:.1f} ТБ"
    return f"{conv(dk['used'])} / {conv(dk['total'])} {unit}", f"своб. {free_s}"


def disk_level(p):
    """0 — норма, 1 — занято от 85%, 2 — занято от 95% (места почти нет)."""
    if p is None or p < 85:
        return 0
    return 1 if p < 95 else 2


def fmt_mem(used, total):
    if used is None and total is None:
        return NA
    u = NA if used is None else f"{gb(used):.1f}"
    t = NA if total is None else f"{gb(total):.0f} ГБ"
    return f"{u} / {t}"


def already_running():
    """Не даём запустить второй экземпляр (например, из автозагрузки и вручную)."""
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = wintypes.HANDLE
    already_running.handle = k32.CreateMutexW(None, False, "Local\\" + APP_NAME + "_mutex")
    return k32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def selftest():
    print("Проверка источников данных...\n")
    c = Collector()
    for kind, cls in (("NVIDIA (NVML)", NvidiaGpu), ("Windows PDH", WindowsGpu)):
        try:
            g = cls()
            g.read()
            time.sleep(1)
            print(f"  [OK]  {kind}: {g.name} -> {g.read()}")
        except Exception as ex:
            print(f"  [--]  {kind}: недоступно ({ex})")
    try:
        print(f"  [OK]  LibreHardwareMonitor: {read_lhm_temps()}")
    except Exception as ex:
        print(f"  [--]  LibreHardwareMonitor: не запущен ({type(ex).__name__})")
    time.sleep(1)
    print(f"\n  Выбран GPU-источник и итоговые данные:")
    d = c.sample()
    print(f"  GPU-источник: {c.gpu_kind}")
    print(f"  Сетевые адаптеры: {', '.join(sorted(c._nics)) or 'нет'}")
    for dk in d["disks"]:
        print(f"  Диск {dk['name']}  {' · '.join(fmt_disk(dk))} ({dk['percent']:.0f}%)")
    print(f"  Процессор: {cpu_name()}  (Tjmax {d['cpu_temp_limit']} °C)")
    for k, v in d.items():
        print(f"    {k:10} = {v}")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # чёткий текст на 4K/масштабе 125%+
    except Exception:
        pass
    if "--selftest" in sys.argv:
        selftest()
        return
    if already_running():
        return
    Widget().run()


if __name__ == "__main__":
    main()
