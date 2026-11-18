# -*- coding: utf-8 -*-
"""生成 exe 图标 assets/icon.ico（512 母版 → 多尺寸 ICO）。

设计：蓝色圆角底 + 白色公文包（求职）+ 包体上的上升折线（追踪/趋势）
+ 右上角橙点（新岗位提醒）。全部几何绘制，16x16 也能辨认。
"""
import os
from PIL import Image, ImageDraw

S = 512
BG_TOP = (37, 99, 235)
BG_BOT = (21, 58, 145)
WHITE = (255, 255, 255, 255)
BLUE = (28, 105, 212, 255)
ORANGE = (255, 138, 61, 255)

BASE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(BASE, "assets")
os.makedirs(ASSETS, exist_ok=True)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# 渐变底，用圆角蒙版裁切
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((16, 16, 496, 496), radius=112, fill=255)
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    c = tuple(int(BG_TOP[i] * (1 - t) + BG_BOT[i] * t) for i in range(3)) + (255,)
    gd.line([(0, y), (S, y)], fill=c)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)

# 提手：先画，下半部会被包体盖住
d.rounded_rectangle((196, 128, 316, 234), radius=46, outline=WHITE, width=26)

# 包体
d.rounded_rectangle((96, 206, 416, 398), radius=38, fill=WHITE)

# 上升折线：岗位趋势 / 追踪（两端圆点表示起止数据点，不加箭头避免小尺寸糊成一团）
pts = [(138, 320), (196, 292), (248, 312), (304, 252), (366, 268)]
d.line(pts, fill=BLUE, width=18, joint="curve")
for x, y in (pts[0], pts[-1]):
    d.ellipse((x - 11, y - 11, x + 11, y + 11), fill=BLUE)

# 右上角新岗位提醒点
d.ellipse((368, 88, 456, 176), fill=ORANGE, outline=WHITE, width=14)

png = os.path.join(ASSETS, "icon.png")
ico = os.path.join(ASSETS, "icon.ico")
img.save(png)
img.save(ico, format="ICO",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

print("已生成:", png)
print("已生成:", ico, "%.1f KB" % (os.path.getsize(ico) / 1024))
print("ICO 尺寸: 16/24/32/48/64/128/256")
