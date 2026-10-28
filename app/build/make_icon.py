"""生成应用图标 app.ico（逐尺寸单独绘制，小尺寸专门优化）。

为什么不用「画 256 再缩」：任务栏/标题栏用的是 16~32px，硬缩放会把文档里的
横线糊成一片，看起来就是个模糊色块。这里对每个尺寸单独设定比例与细节：
  · 16/20px：只留 2 条粗线、加大对勾徽标，保证一眼能认出「文档+对勾」
  · 24~48px：3 条线，线宽按比例加粗
  · 64px 以上：完整细节 + 轻微投影
ICO 内部小尺寸用 32 位 BMP（兼容性最好），256/128 用 PNG 压缩。
"""
import os
import struct

from PIL import Image, ImageDraw

SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

BLUE_TOP = (92, 124, 250)      # #5C7CFA
BLUE_BOT = (73, 88, 232)       # #4958E8
LINE_BLUE = (72, 88, 226)
GREEN = (34, 197, 94)          # #22C55E
WHITE = (255, 255, 255)


def rounded_gradient(size: int, radius_ratio: float = 0.235) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    span = max(1, size - 1)
    for y in range(size):
        for x in range(size):
            t = (x * 0.35 + y * 0.65) / span          # 斜向渐变，纵向更明显
            px[x, y] = (round(BLUE_TOP[0] + (BLUE_BOT[0] - BLUE_TOP[0]) * t),
                        round(BLUE_TOP[1] + (BLUE_BOT[1] - BLUE_TOP[1]) * t),
                        round(BLUE_TOP[2] + (BLUE_BOT[2] - BLUE_TOP[2]) * t))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=max(2, round(size * radius_ratio)), fill=255)
    im.paste(grad, (0, 0), mask)
    return im


def draw_glyph(im: Image.Image) -> Image.Image:
    """文档 + 对勾。坐标全按边长比例，细节条数随尺寸变化。"""
    s = im.size[0]
    d = ImageDraw.Draw(im)
    tiny = s <= 20
    small = s <= 48

    # ---- 纸张：实心白纸比细线框在小尺寸下可读性高得多
    x0, y0 = (s * 0.165, s * 0.125) if tiny else (s * 0.215, s * 0.165)
    x1, y1 = (s * 0.720, s * 0.815) if tiny else (s * 0.705, s * 0.775)
    sheet_r = max(1, round(s * (0.09 if tiny else 0.055)))
    if not tiny:                                       # 大尺寸给一点点落影
        d.rounded_rectangle([x0, y0 + s * 0.012, x1, y1 + s * 0.012],
                            radius=sheet_r, fill=(20, 28, 60, 70))
    d.rounded_rectangle([x0, y0, x1, y1], radius=sheet_r, fill=WHITE)

    # ---- 纸上的文字线
    line_h = max(1, round(s * (0.095 if tiny else 0.052)))
    ys = (0.295, 0.490) if tiny else (0.315, 0.415, 0.515)
    widths = (0.62, 0.42) if tiny else (0.66, 0.66, 0.42)
    lx = x0 + (x1 - x0) * (0.14 if tiny else 0.155)
    for i, fy in enumerate(ys):
        w = (x1 - x0) * widths[i] - (x1 - x0) * (0.14 if tiny else 0.155)
        d.rounded_rectangle([lx, s * fy, lx + w, s * fy + line_h],
                            radius=max(1, line_h // 2), fill=LINE_BLUE)

    # ---- 绿色对勾徽标（白圈隔开，压住纸张右下角）
    r = s * (0.235 if tiny else 0.205)
    cx, cy = s * (0.750 if tiny else 0.735), s * (0.775 if tiny else 0.745)
    ring = max(1, round(s * (0.062 if tiny else 0.055)))
    d.ellipse([cx - r - ring, cy - r - ring, cx + r + ring, cy + r + ring], fill=WHITE)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GREEN)
    cw = max(2, round(s * (0.115 if tiny else 0.082)))
    pts = [(cx - r * 0.46, cy + r * 0.02),
           (cx - r * 0.10, cy + r * 0.40),
           (cx + r * 0.52, cy - r * 0.38)]
    d.line(pts, fill=WHITE, width=cw, joint="curve")
    for pt in (pts[0], pts[-1]):                       # 圆头，避免尖角糊边
        d.ellipse([pt[0] - cw / 2, pt[1] - cw / 2, pt[0] + cw / 2, pt[1] + cw / 2], fill=WHITE)
    return im


def render(size: int, supersample: int = 4) -> Image.Image:
    big = size * supersample
    im = draw_glyph(rounded_gradient(big))
    return im.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- ICO 写出
def _dib(img: Image.Image) -> bytes:
    """32 位 BMP（ICO 内的 DIB）＋ AND 掩码。"""
    w, h = img.size
    px = img.convert("RGBA").load()
    rows = []
    for y in range(h - 1, -1, -1):                     # BMP 自下而上
        row = bytearray()
        for x in range(w):
            r, g, b, a = px[x, y]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    xor = b"".join(rows)
    row_bytes = ((w + 31) // 32) * 4                   # 1bpp 按 4 字节对齐
    andmask = bytearray()
    for y in range(h - 1, -1, -1):
        bits = bytearray(row_bytes)
        for x in range(w):
            if px[x, y][3] < 128:                      # 透明像素 = 掩码 1
                bits[x // 8] |= 0x80 >> (x % 8)
        andmask += bits
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(xor) + len(andmask), 0, 0, 0, 0)
    return header + xor + bytes(andmask)


def write_ico(path: str, images):
    entries, blobs, offset = [], [], 6 + 16 * len(images)
    for img in images:
        w = img.size[0]
        if w >= 128:                                   # 大图走 PNG 压缩
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
        else:
            data = _dib(img)
        entries.append(struct.pack("<BBBBHHII", w if w < 256 else 0, w if w < 256 else 0,
                                   0, 0, 1, 32, len(data), offset))
        blobs.append(data)
        offset += len(data)
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(images)))
        for e in entries:
            f.write(e)
        for b in blobs:
            f.write(b)


if __name__ == "__main__":
    here = os.path.abspath(__file__)
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))   # app/build/x.py -> 项目根
    out = os.path.join(root, "app", "frontend", "app.ico")
    images = [render(s) for s in SIZES]
    write_ico(out, images)
    images[-1].save(out.replace(".ico", ".png"))       # 256 版本存 png 备用
    print("icon ->", out, "sizes:", SIZES)
