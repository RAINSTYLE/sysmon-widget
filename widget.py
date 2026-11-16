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
                    "scale": 1.0, "view": "bars"}

UPDATE_MS = 1000          # обновление экрана раз в секунду
LHM_URL = "http://127.0.0.1:8085/data.json"
NA = "н/д"

# ---------- оформление ----------
BG = "#16181d"
FG = "#e8e9ec"
MUTED = "#8b919c"
BAR_BG = "#2a2e36"
GREEN = "#43c38a"
YELLOW = "#e5b84b"
RED = "#e5534b"
FONT = "Segoe UI"


def bar_color(p):
    if p is None:
        return BAR_BG
    if p < 60:
        return GREEN
    if p < 85:
        return YELLOW
    return RED


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

    def read(self):
        nv, h = self.nv, self.h
        out = {"util": None, "mem_used": None, "mem_total": None, "temp": None}
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
        for p in preferred:
            for name, v in temps:
                if p in name.lower():
                    return v
        return max((v for _, v in temps), default=None)

    return (pick(cpu_temps, ("tctl", "tdie", "package", "core average")),
            pick(gpu_temps, ("gpu core", "core")))


class Collector(threading.Thread):
    """Собирает данные в фоне, чтобы окно никогда не «подвисало»."""

    def __init__(self):
        super().__init__(daemon=True)
        self.data = {}
        self.gpu = None
        self.gpu_kind = None
        self._gpu_retry = 0
        self._lhm_next = 0
        self._lhm = (None, None)
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
                  "vram_total": g.get("mem_total"), "gpu_temp": g.get("temp")})

        # LibreHardwareMonitor: опрашиваем раз в 2 с; если не запущен — раз в 30 с
        if now >= self._lhm_next:
            try:
                self._lhm = read_lhm_temps()
                self._lhm_next = now + 2
            except Exception:
                self._lhm = (None, None)
                self._lhm_next = now + 30
        d["cpu_temp"] = self._lhm[0]
        if d["gpu_temp"] is None:
            d["gpu_temp"] = self._lhm[1]
        return d

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
    return s


def save_settings(s):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# Базовые размеры окна (при масштабе 100%) для каждого вида
VIEWS = {"bars": (230, 182), "gauges": (360, 146)}
MIN_SCALE, MAX_SCALE = 0.6, 3.0
NEEDLE = "#ff6a4d"
# Цветные зоны шкалы спидометра (тёмные оттенки зелёного/жёлтого/красного)
ZONES = ((0, 60, "#1f5a41"), (60, 85, "#6b5622"), (85, 100, "#6e2c28"))
METRICS = (("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU"))


class Widget:
    def __init__(self):
        self.s = load_settings()
        if self.s.get("view") not in VIEWS:
            self.s["view"] = "bars"
        self.scale = clamp(float(self.s.get("scale") or 1.0), MIN_SCALE, MAX_SCALE)

        self.root = tk.Tk()
        self.root.title("Системный монитор")
        self.root.overrideredirect(True)          # без рамки
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", bool(self.s["topmost"]))
        self.root.attributes("-alpha", float(self.s["alpha"]))
        self.dpi = self.root.winfo_fpixels("1i") / 96.0

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self.target, self.shown, self.lines = {}, {}, {}
        self.hover = False
        self._anim_id = None
        self._press = None

        self.W, self.H = self.size_for(self.scale)
        self.place_window()
        self.round_corners()

        self.topmost_var = tk.BooleanVar(value=bool(self.s["topmost"]))
        self.alpha_var = tk.DoubleVar(value=float(self.s["alpha"]))
        self.view_var = tk.StringVar(value=self.s["view"])
        self.menu = self.build_menu()

        c = self.canvas
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Motion>", self.on_motion)
        c.bind("<Enter>", lambda e: self.set_hover(True))
        c.bind("<Leave>", lambda e: self.set_hover(False))
        c.bind("<Button-3>", self.show_menu)

        self.collector = Collector()
        self.collector.start()
        self.redraw()
        self.root.after(300, self.refresh)

    # ----- размер и позиция -----
    def size_for(self, scale):
        bw, bh = VIEWS[self.s["view"]]
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

    def round_corners(self):
        """Скруглённые углы в Windows 11 (в Windows 10 просто ничего не произойдёт)."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        except Exception:
            pass

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

    def set_hover(self, value):
        if self._press is None and self.hover != value:
            self.hover = value
            self.redraw()

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
        views = tk.Menu(m, tearoff=0)
        views.add_radiobutton(label="Полоски", value="bars", variable=self.view_var,
                              command=self.set_view)
        views.add_radiobutton(label="Спидометры", value="gauges", variable=self.view_var,
                              command=self.set_view)
        m.add_cascade(label="Вид", menu=views)
        sub = tk.Menu(m, tearoff=0)
        for pct in (100, 90, 80, 70, 60, 50, 40):
            sub.add_radiobutton(label=f"{pct}%", value=pct / 100, variable=self.alpha_var,
                                command=self.set_alpha)
        m.add_cascade(label="Прозрачность", menu=sub)
        m.add_command(label="Сбросить размер", command=self.reset_size)
        m.add_separator()
        m.add_command(label="Закрыть", command=self.close)
        return m

    def show_menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def toggle_topmost(self):
        self.s["topmost"] = bool(self.topmost_var.get())
        self.root.attributes("-topmost", self.s["topmost"])
        save_settings(self.s)

    def set_alpha(self):
        self.s["alpha"] = round(float(self.alpha_var.get()), 2)
        self.root.attributes("-alpha", self.s["alpha"])
        save_settings(self.s)

    def resize_to(self, scale):
        self.scale = scale
        self.W, self.H = self.size_for(scale)
        self.root.geometry(f"{self.W}x{self.H}")
        self.redraw()
        self.save_geometry()

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
            gpu_t = fmt_temp(d.get("gpu_temp"))
            vram = fmt_mem(d.get("vram_used"), d.get("vram_total"))
            self.lines = {
                "bars": {"cpu": f"Температура: {fmt_temp(d.get('cpu_temp'))}",
                         "ram": fmt_mem(d.get("ram_used"), d.get("ram_total")),
                         "gpu": f"{gpu_t}  ·  VRAM {vram}"},
                "gauges": {"cpu": (f"Темп. {fmt_temp(d.get('cpu_temp'))}", ""),
                           "ram": (fmt_mem(d.get("ram_used"), d.get("ram_total")), ""),
                           "gpu": (gpu_t, f"VRAM {vram}")},
            }
            self.animate()
        except Exception:
            pass  # никогда не падаем из-за отображения
        self.root.after(UPDATE_MS, self.refresh)

    def animate(self):
        """Стрелки и полосы плавно «доезжают» до нового значения (~0.3 с)."""
        if self._anim_id:
            self.root.after_cancel(self._anim_id)
            self._anim_id = None
        moving = False
        for key, t in self.target.items():
            cur = self.shown.get(key)
            if t is None or cur is None or abs(t - cur) < 1:
                self.shown[key] = t
            else:
                self.shown[key] = cur + (t - cur) * 0.45
                moving = True
        self.redraw(dynamic_only=True)
        if moving:
            self._anim_id = self.root.after(40, self.animate)

    # ----- рисование -----
    def font(self, px, bold=False):
        size = -max(6, round(px * self.k))      # минус = размер в пикселях
        return (FONT, size, "bold") if bold else (FONT, size)

    def redraw(self, dynamic_only=False):
        """Полная перерисовка или (во время анимации) только меняющихся частей.
        Неподвижные элементы (шкала, деления, подписи) рисуются один раз —
        так виджет почти не нагружает процессор."""
        c = self.canvas
        self.full = not dynamic_only
        if dynamic_only:
            c.delete("dyn")
        else:
            c.delete("all")
        self.k = self.W / VIEWS[self.s["view"]][0]
        try:
            if self.s["view"] == "gauges":
                self.draw_gauges()
            else:
                self.draw_bars()
        except Exception:
            pass
        if self.hover and self.full:
            self.draw_grip()

    def draw_bars(self):
        c, k = self.canvas, self.k
        lines = self.lines.get("bars", {})
        pad = 12 * k
        for i, (key, title) in enumerate(METRICS):
            y0 = (8 + i * 56) * k
            value, shown = self.target.get(key), self.shown.get(key)
            by, bh = y0 + 37 * k, max(2, 5 * k)
            if self.full:
                c.create_text(pad, y0, text=title, anchor="nw", fill=FG, font=self.font(13, True))
                c.create_rectangle(pad, by, self.W - pad, by + bh, fill=BAR_BG, width=0)
            c.create_text(self.W - pad, y0, text=fmt_pct(value), anchor="ne", fill=FG,
                          font=self.font(13, True), tags="dyn")
            c.create_text(pad, y0 + 19 * k, text=lines.get(key, ""), anchor="nw", fill=MUTED,
                          font=self.font(11), tags="dyn")
            if shown:
                p = clamp(shown, 0, 100) / 100
                c.create_rectangle(pad, by, pad + (self.W - 2 * pad) * p, by + bh,
                                   fill=bar_color(value), width=0, tags="dyn")

    def draw_gauges(self):
        k = self.k
        lines = self.lines.get("gauges", {})
        for i, (key, title) in enumerate(METRICS):
            cx, cy, r = (60 + i * 120) * k, 58 * k, 42 * k
            self.draw_gauge(cx, cy, r, title, self.target.get(key), self.shown.get(key),
                            lines.get(key, (NA, "")))

    def draw_gauge(self, cx, cy, r, title, value, shown, lines):
        """Спидометр: шкала 240°, от 0% (слева внизу) до 100% (справа внизу)."""
        c, k = self.canvas, self.k
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
            for a, b, col in ZONES:
                c.create_arc(cx - zr, cy - zr, cx + zr, cy + zr, start=angle(b),
                             extent=(b - a) * 2.4, style="arc", width=max(1, 2 * k), outline=col)
            # дорожка
            c.create_arc(*box, start=-30, extent=240, style="arc", width=th, outline=BAR_BG)
            # деления: крупные через 20%, мелкие через 10%
            for i in range(11):
                major = i % 2 == 0
                r1 = r - th / 2 - 2 * k
                r2 = r1 - (6 if major else 3) * k
                c.create_line(*pt(angle(i * 10), r1), *pt(angle(i * 10), r2), fill=MUTED,
                              width=max(1, (1.6 if major else 1) * k))
            c.create_text(cx, cy + r * 0.88, text=title, fill=MUTED, font=self.font(10, True))
        # заполнение дуги
        if shown and shown >= 0.5:
            c.create_arc(*box, start=210, extent=-clamp(shown, 0, 100) * 2.4, style="arc",
                         width=th, outline=bar_color(value), tags="dyn")
        # стрелка
        a = math.radians(angle(shown or 0))
        dx, dy = math.cos(a), -math.sin(a)                   # направление стрелки
        px, py = -dy, dx                                     # перпендикуляр
        L, tail, w = r * 0.78, r * 0.14, 2.4 * k
        needle_col = NEEDLE if shown is not None else MUTED
        c.create_polygon(cx + dx * L, cy + dy * L,
                         cx + px * w, cy + py * w,
                         cx - dx * tail, cy - dy * tail,
                         cx - px * w, cy - py * w,
                         fill=needle_col, outline="", tags="dyn")
        hr = 4.5 * k
        c.create_oval(cx - hr, cy - hr, cx + hr, cy + hr, fill="#2a2e36",
                      outline=needle_col, width=max(1, 1.5 * k), tags="dyn")
        # подписи
        c.create_text(cx, cy + r * 0.45, text=fmt_pct(value), fill=FG, font=self.font(15, True),
                      tags="dyn")
        c.create_text(cx, cy + r + 10 * k, text=lines[0], fill=MUTED, font=self.font(10),
                      tags="dyn")
        if lines[1]:
            c.create_text(cx, cy + r + 24 * k, text=lines[1], fill=MUTED, font=self.font(10),
                          tags="dyn")

    def draw_grip(self):
        """Метка в правом нижнем углу: «за угол можно тянуть»."""
        c, k = self.canvas, self.k
        x, y = self.W - 4 * k, self.H - 4 * k
        for d in (4, 8, 12):
            c.create_line(x - d * k, y, x, y - d * k, fill="#4a4f59", width=max(1, k))

    def run(self):
        self.root.mainloop()


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def fmt_pct(p):
    return NA if p is None else f"{p:.0f}%"


def fmt_temp(t):
    return NA if t is None else f"{t:.0f} °C"


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
