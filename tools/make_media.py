"""
Создаёт картинки и GIF для README (демо-значения, не реальная нагрузка).
Нужен Pillow:  .venv/Scripts/python.exe -m pip install pillow
Запуск:        .venv/Scripts/python.exe tools/make_media.py . docs
Настройки виджета (%APPDATA%/SysMonWidget/settings.json) сохраняются и восстанавливаются.
"""
import sys, os, math, ctypes, shutil
ctypes.windll.shcore.SetProcessDpiAwareness(1)
sys.path.insert(0, sys.argv[1])
OUT = sys.argv[2]
os.makedirs(OUT, exist_ok=True)
import widget
import tkinter as tk
from PIL import Image, ImageGrab, ImageDraw, ImageFont, ImageTk, ImageFilter

BACKUP = widget.SETTINGS_FILE + ".media-backup"
if os.path.exists(widget.SETTINGS_FILE):
    shutil.copy(widget.SETTINGS_FILE, BACKUP)
ctypes.windll.user32.SetCursorPos(5, 5)        # курсор подальше: уголок не показывается


def disk(name, total, used):
    return {"name": name, "total": total * 2**30, "used": used * 2**30,
            "free": (total - used) * 2**30, "percent": used / total * 100}


DISKS = [disk("C:", 465, 315), disk("D:", 931, 540), disk("E:", 1863, 1640)]


def data(cpu, ram, gpu, gpu_t=46, cpu_t=52, down=48.6e6, up=2.1e6):
    return {"cpu": cpu, "ram": ram, "ram_used": 32 * 2**30 * ram / 100, "ram_total": 32 * 2**30,
            "gpu": gpu, "vram_used": 16 * 2**30 * (0.1 + gpu / 140), "vram_total": 16 * 2**30,
            "gpu_temp": gpu_t, "gpu_temp_limit": 87, "cpu_temp": cpu_t, "cpu_temp_limit": 95,
            "net_down": down / 8, "net_up": up / 8, "disks": DISKS}


class Fake:
    show = widget.DEFAULT_SHOW
    data = data(34, 41, 22)


w = widget.Widget()
w.root.attributes("-alpha", 1.0)      # непрозрачно — чтобы на снимок не попал рабочий стол
w.collector = Fake()
w.s["show"].update(widget.DEFAULT_SHOW)   # на картинках — все показатели
w.s["net_units"] = "bits"
w.root.geometry("+160+160")


def style(theme="graphite", transparent=False):
    w.s["theme"], w.s["transparent"] = theme, transparent
    w.apply_style()


def grab(rounded=True):
    w.root.update()
    x, y = w.root.winfo_rootx(), w.root.winfo_rooty()
    img = ImageGrab.grab(bbox=(x, y, x + w.W, y + w.H), all_screens=True).convert("RGBA")
    if rounded:                                   # скруглённые углы, как в Windows 11
        m = Image.new("L", img.size, 0)
        rad = max(6, round(8 * w.scale * w.dpi))
        ImageDraw.Draw(m).rounded_rectangle((0, 0, img.width - 1, img.height - 1), rad, fill=255)
        img.putalpha(m)
    return img


def set_view(view, scale=1.0):
    w.view_var.set(view); w.s["view"] = view
    w.scale = scale; w.W, w.H = w.size_for(scale)
    w.root.geometry(f"{w.W}x{w.H}"); w.redraw()


def settle(n=15):
    w.refresh_lines()
    w.target = {k: Fake.data.get(k) for k in ("cpu", "ram", "gpu")}
    w.animate(values=True)
    for _ in range(n):
        w.animate(); w.root.update(); w.root.after(30)


def snapshot(view, d, name, scale=1.2):
    Fake.data = d
    set_view(view, scale)
    settle()
    grab().save(os.path.join(OUT, name))


def font(px, bold=False):
    try:
        return ImageFont.truetype("segoeuib.ttf" if bold else "segoeui.ttf", px)
    except OSError:
        return ImageFont.load_default()


def themes_sheet():
    """Все темы: спидометры, сетка 3×2 с подписями."""
    Fake.data = data(34, 72, 91, gpu_t=85, cpu_t=85)
    set_view("gauges", 1.0)
    tiles = []
    for key, th in widget.THEMES.items():
        style(key)
        settle()
        tiles.append((th["name"], grab()))
    iw, ih = tiles[0][1].size
    gap, cap = 16, 26
    out = Image.new("RGBA", (3 * iw + 2 * gap, 2 * (ih + cap) + gap), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    for i, (name, im) in enumerate(tiles):
        x, y = (i % 3) * (iw + gap), (i // 3) * (ih + cap + gap)
        d.text((x + iw / 2, y + cap / 2 - 2), name, fill=(140, 146, 156, 255), font=font(15, True),
               anchor="mm")
        out.paste(im, (x, y + cap), im)
    out.save(os.path.join(OUT, "themes.png"))
    style("graphite")


def backdrop_image(wd, ht):
    """Абстрактные «обои» для примера прозрачного фона."""
    img = Image.new("RGB", (wd, ht))
    px = img.load()
    for yy in range(ht):
        for xx in range(wd):
            t = xx / wd
            s = yy / ht
            px[xx, yy] = (int(40 + 180 * t * (1 - s * 0.4)), int(60 + 70 * s), int(150 + 80 * (1 - t)))
    d = ImageDraw.Draw(img)
    for cx, cy, r, col in ((0.15, 0.3, 0.35, (255, 196, 120)), (0.8, 0.75, 0.4, (90, 220, 230)),
                           (0.55, 0.1, 0.25, (240, 240, 255))):
        d.ellipse(((cx - r) * wd, (cy - r) * ht * 1.6, (cx + r) * wd, (cy + r) * ht * 1.6),
                  fill=col)
    return img.filter(ImageFilter.GaussianBlur(40))


def transparent_example():
    """Виджет с прозрачным фоном поверх «обоев»: оба вида рядом."""
    shots = []
    for view in ("gauges", "bars"):
        Fake.data = data(34, 41, 22)
        set_view(view, 1.1)
        style("graphite", transparent=True)
        margin = 28
        bw, bh = w.W + 2 * margin, w.H + 2 * margin
        bg = backdrop_image(bw, bh)
        top = tk.Toplevel(w.root)
        top.overrideredirect(True)
        x, y = w.root.winfo_rootx() - margin, w.root.winfo_rooty() - margin
        top.geometry(f"{bw}x{bh}+{x}+{y}")
        photo = ImageTk.PhotoImage(bg)
        tk.Label(top, image=photo, bd=0).pack()
        # подложка поверх всех окон (иначе сквозь прозрачный виджет виден рабочий стол),
        # а виджет — поверх подложки
        top.attributes("-topmost", True)
        top.update()
        w.root.attributes("-topmost", False)
        w.root.attributes("-topmost", True)
        w.root.lift()
        for _ in range(10):
            w.root.update(); w.root.after(50)
        settle(20); w.root.update(); w.root.after(300); w.root.update()
        shots.append(ImageGrab.grab(bbox=(x, y, x + bw, y + bh), all_screens=True))
        top.destroy()
    gap = 16
    out = Image.new("RGBA", (sum(s.width for s in shots) + gap, max(s.height for s in shots)),
                    (0, 0, 0, 0))
    xx = 0
    for s in shots:
        m = Image.new("L", s.size, 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, s.width - 1, s.height - 1), 12, fill=255)
        out.paste(s, (xx, 0), m)
        xx += s.width + gap
    out.save(os.path.join(OUT, "transparent.png"))
    style("graphite")


def gif_gauges():
    set_view("gauges", 1.2)
    frames = []
    seq = [(15, 35, 5, 41, 48), (62, 38, 48, 55, 62), (91, 41, 97, 84, 85), (45, 44, 88, 70, 80),
           (23, 40, 30, 58, 60), (15, 35, 5, 41, 48)]
    for cpu, ram, gpu, ct, gt in seq:
        Fake.data = data(cpu, ram, gpu, gpu_t=gt, cpu_t=ct, down=gpu * 0.9e6 + 3e5,
                         up=cpu * 0.05e6 + 1e5)
        w.refresh_lines()
        w.target = {"cpu": cpu, "ram": ram, "gpu": gpu}
        w.animate(values=True)
        frames.append(grab())
        for _ in range(9):         # кадры анимации стрелок
            w.animate(); w.root.update()
            frames.append(grab())
        for _ in range(8):         # пауза
            frames.append(frames[-1])
    save_gif(frames, "demo_gauges.gif", 50)


def gif_resize():
    Fake.data = data(34, 41, 22)
    frames = []
    set_view("bars", 0.8)
    settle()
    scales = [0.8 + 0.6 * (1 - math.cos(math.pi * i / 24)) / 2 for i in range(25)]
    scales += scales[::-1]
    maxw, maxh = w.size_for(1.4)
    for s in scales:
        w.scale = s; w.W, w.H = w.size_for(s)
        w.root.geometry(f"{w.W}x{w.H}"); w.redraw(); w.root.update()
        canvas = Image.new("RGBA", (maxw, maxh), (0, 0, 0, 0))
        canvas.paste(grab(), (0, 0))
        frames.append(canvas)
    save_gif(frames, "demo_resize.gif", 50)


def save_gif(frames, name, ms):
    # прозрачный фон GIF: альфа -> индекс прозрачности
    pal = []
    for f in frames:
        p = f.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT)
        alpha = f.getchannel("A")
        mask = Image.eval(alpha, lambda a: 255 if a < 128 else 0)
        p.paste(255, mask=mask)
        p.info["transparency"] = 255
        pal.append(p)
    pal[0].save(os.path.join(OUT, name), save_all=True, append_images=pal[1:],
                duration=ms, loop=0, disposal=2, transparency=255)


def main():
    try:
        style("graphite")
        snapshot("bars", data(34, 41, 22), "bars.png")
        snapshot("gauges", data(34, 41, 22), "gauges.png")
        themes_sheet()
        transparent_example()
        gif_gauges()
        gif_resize()
    finally:
        if os.path.exists(BACKUP):
            shutil.move(BACKUP, widget.SETTINGS_FILE)
        os._exit(0)


w.root.after(800, main)
w.root.mainloop()
