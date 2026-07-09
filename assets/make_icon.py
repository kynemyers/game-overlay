"""Generates icon.ico (app icon) and banner.png (README hero image).

Run once with Pillow installed: python assets/make_icon.py
Not needed at build time - the generated files are committed.
"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

BG = (16, 16, 20, 255)
GREEN = (0, 255, 136, 255)
CYAN = (0, 204, 255, 255)
RED = (255, 85, 85, 255)
YELLOW = (255, 204, 0, 255)
ORANGE = (255, 153, 51, 255)
PURPLE = (204, 102, 255, 255)
PINK = (255, 102, 153, 255)


def rounded_bg(size, radius_ratio=0.22):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * radius_ratio)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BG)
    return img, d


def draw_icon(size):
    """HUD corner-brackets (viewfinder look) + a row of metric dots."""
    img, d = rounded_bg(size)
    m = size * 0.14      # margin from edge
    L = size * 0.22      # bracket arm length
    w = max(2, int(size * 0.05))
    col = (235, 240, 245, 255)

    corners = [
        ((m, m), (1, 1)),               # top-left
        ((size - m, m), (-1, 1)),       # top-right
        ((m, size - m), (1, -1)),       # bottom-left
        ((size - m, size - m), (-1, -1)),  # bottom-right
    ]
    for (x, y), (sx, sy) in corners:
        d.line([(x, y), (x + sx * L, y)], fill=col, width=w)
        d.line([(x, y), (x, y + sy * L)], fill=col, width=w)

    # row of coloured metric dots through the centre, like the overlay itself
    dots = [GREEN, CYAN, YELLOW, PURPLE, RED]
    n = len(dots)
    dot_r = size * 0.052
    gap = size * 0.13
    total_w = gap * (n - 1)
    start_x = size / 2 - total_w / 2
    y = size / 2
    for i, c in enumerate(dots):
        x = start_x + i * gap
        d.ellipse([x - dot_r, y - dot_r, x + dot_r, y + dot_r], fill=c)
    return img


def draw_banner(w=1200, h=360):
    img = Image.new("RGB", (w, h), (10, 12, 16))
    d = ImageDraw.Draw(img)
    # subtle vertical gradient
    top = (14, 16, 22)
    bot = (24, 20, 30)
    for y in range(h):
        t = y / h
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        d.line([(0, y), (w, y)], fill=(r, g, b))
    # faint grid to suggest a game HUD backdrop
    grid = (255, 255, 255, 10)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for x in range(0, w, 60):
        od.line([(x, 0), (x, h)], fill=grid, width=1)
    for y in range(0, h, 60):
        od.line([(0, y), (w, y)], fill=grid, width=1)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    d = ImageDraw.Draw(img)

    def font(sz, bold=True):
        names = ["consolab.ttf", "consola.ttf"] if bold else ["consola.ttf"]
        for n in names:
            for base in (r"C:\Windows\Fonts",):
                p = os.path.join(base, n)
                if os.path.exists(p):
                    return ImageFont.truetype(p, sz)
        return ImageFont.load_default()

    title_font = font(46)
    sub_font = font(20, bold=False)
    metric_font = font(26)

    d.text((48, 44), "GameOverlay", font=title_font, fill=(240, 245, 250))
    d.text((48, 100), "FPS  ·  Ping  ·  Packet loss  ·  CPU/GPU usage & temps",
           font=sub_font, fill=(160, 168, 180))

    # mock overlay panel, top-right style, matching the real app's look
    panel_w, panel_h = 260, 300
    px, py = w - panel_w - 60, (h - panel_h) // 2 + 10
    panel = Image.new("RGBA", (panel_w, panel_h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle([0, 0, panel_w - 1, panel_h - 1], radius=14, fill=(16, 16, 20, 235))
    rows = [
        ("FPS", "184", GREEN[:3]),
        ("PING", "22 ms", CYAN[:3]),
        ("LOSS", "0.0 %", RED[:3]),
        ("CPU", "34 %", YELLOW[:3]),
        (u"CPU°", "57°C", ORANGE[:3]),
        ("GPU", "61 %", PURPLE[:3]),
        (u"GPU°", "58°C", PINK[:3]),
    ]
    ry = 22
    for label, val, col in rows:
        pd.text((22, ry), "%-5s %s" % (label, val), font=metric_font, fill=col)
        ry += 38
    img.paste(panel, (px, py), panel)

    return img


if __name__ == "__main__":
    os.makedirs(HERE, exist_ok=True)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [draw_icon(s) for s in sizes]
    ico_path = os.path.join(ROOT, "icon.ico")
    imgs[-1].save(ico_path, format="ICO",
                  sizes=[(s, s) for s in sizes])
    print("wrote", ico_path)

    banner = draw_banner()
    banner_path = os.path.join(HERE, "banner.png")
    banner.save(banner_path, format="PNG")
    print("wrote", banner_path)
