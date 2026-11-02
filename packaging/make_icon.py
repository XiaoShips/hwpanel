# -*- coding: utf-8 -*-
"""生成 TempPeek 应用图标（packaging/icon.ico）。"""
import math
import os

from PIL import Image, ImageDraw

S = 256
BG = "#0b1018"
BORDER = "#253043"
TRACK = "#222d3e"
NEEDLE = "#e9f0fa"


def lerp(c1, c2, k):
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


def main():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, S - 6, S - 6], radius=52, fill=BG,
                        outline=BORDER, width=6)

    box = [52, 54, 204, 206]
    d.arc(box, 135, 405, fill=TRACK, width=18)

    # 绿 → 黄 → 红三段弧，模拟仪表
    stops = [("#3ddc97", "#ffc857"), ("#ffc857", "#ff5f6d")]
    start, end = 135, 405
    n = 72
    for i in range(n):
        a0 = start + (end - start) * i / float(n)
        a1 = start + (end - start) * (i + 1) / float(n) + 1.0
        k = i / float(n - 1)
        seg = 0 if k < 0.55 else 1
        local = k / 0.55 if seg == 0 else (k - 0.55) / 0.45
        d.arc(box, a0, a1, fill=lerp(stops[seg][0], stops[seg][1], local), width=18)

    cx = cy = 128.0
    ang = math.radians(322)
    d.line([cx, cy, cx + 62 * math.cos(ang), cy + 62 * math.sin(ang)],
           fill=NEEDLE, width=9)
    d.ellipse([cx - 13, cy - 13, cx + 13, cy + 13], fill="#5aa9ff")
    d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=BG)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    img.save(out, sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                         (32, 32), (16, 16)])
    print("icon ->", out)


if __name__ == "__main__":
    main()
