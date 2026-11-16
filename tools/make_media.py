"""
Создаёт картинки и GIF для README (демо-значения, не реальная нагрузка).
Нужен Pillow:  .venv/Scripts/python.exe -m pip install pillow
Запуск:        .venv/Scripts/python.exe tools/make_media.py . docs
"""
import sys, os, math, ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(1)
sys.path.insert(0, sys.argv[1])
OUT = sys.argv[2]
os.makedirs(OUT, exist_ok=True)
import widget
from PIL import Image, ImageGrab, ImageDraw


class Fake:
    data = {}


def data(cpu, ram, gpu, gpu_t=46):
    return {"cpu": cpu, "ram": ram, "ram_used": 32 * 2**30 * ram / 100, "ram_total": 32 * 2**30,
            "gpu": gpu, "vram_used": 16 * 2**30 * (0.1 + gpu / 140), "vram_total": 16 * 2**30,
            "gpu_temp": gpu_t, "cpu_temp": 52}


w = widget.Widget()
w.root.attributes("-alpha", 1.0)      # непрозрачно — чтобы на снимок не попал рабочий стол
w.collector = Fake()
w.root.geometry("+120+120")


def grab():
    w.root.update()
    x, y = w.root.winfo_rootx(), w.root.winfo_rooty()
    img = ImageGrab.grab(bbox=(x, y, x + w.W, y + w.H), all_screens=True).convert("RGBA")
    # скруглённые углы с прозрачностью (как в Windows 11)
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
    for _ in range(n):
        w.animate(); w.root.update(); w.root.after(40);


def snapshot(view, d, name, scale=1.2):
    set_view(view, scale)
    Fake.data = d
    w.refresh(); settle()
    grab().save(os.path.join(OUT, name))


def gif_gauges():
    set_view("gauges", 1.2)
    frames = []
    seq = [(15, 35, 5), (62, 38, 48), (91, 41, 97), (45, 44, 88), (23, 40, 30), (15, 35, 5)]
    for cpu, ram, gpu in seq:
        Fake.data = data(cpu, ram, gpu, 40 + gpu * 0.3)
        w.refresh()
        for _ in range(10):        # кадры анимации стрелок
            w.animate(); w.root.update()
            frames.append(grab())
        for _ in range(6):         # пауза
            frames.append(frames[-1])
    save_gif(frames, "demo_gauges.gif", 50)


def gif_resize():
    Fake.data = data(34, 41, 22)
    frames = []
    set_view("bars", 0.8)
    w.refresh(); settle()
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
    snapshot("bars", data(34, 41, 22), "bars.png")
    snapshot("gauges", data(34, 41, 22), "gauges.png")
    gif_gauges()
    gif_resize()
    os._exit(0)


w.root.after(800, main)
w.root.mainloop()
