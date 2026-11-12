"""生成托盘图标（票 #10 原型；v34 T5 起多两档收句态）。

图形：圆角方块底 + 若干白色横线（大尺寸 3 条 / 16–20px 2 条），
语义 = 「速记」的文本行，16px 下仍可辨，且不依赖任何字体渲染。

v34 T5（票 #218 / spec §C25）的托盘三态：
- `tray.ico`      待命（蓝底）；
- `tray-rec.ico`  收句中（蓝底 + 右下红点）；
- `tray-alert.ico` 异常（蓝底 + 右上琥珀点：区域失效 / 引擎不可用 / 心跳断）；
- `tray-off.ico`  灰阶 —— **只表达「内核/中枢不在位」**（决策 D9，不复用）。

可重复生成、产物随仓库提交，所以换颜色/改配色只需改本文件后重跑：
    python make-icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ACCENT = (10, 99, 201, 255)  # #0A63C9  与 UI 强调色同色
WHITE = (255, 255, 255, 255)
GRAY_BG = (158, 158, 158, 255)  # #9E9E9E  内核挂掉时的灰阶底（spec §11）
GRAY_FG = (232, 232, 232, 255)  # #E8E8E8
RED = (214, 69, 54, 255)  # 收句中的录制点
AMBER = (232, 164, 20, 255)  # 异常提示点

SIZES = (16, 20, 24, 32, 48)
SUPERSAMPLE = 4


def render(size: int, bg, fg, dot=None) -> Image.Image:
    """按目标尺寸原生渲染（4× 超采样后缩回），不靠缩放母版得到小尺寸。"""
    s = size * SUPERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = round(s * 0.055)
    draw.rounded_rectangle(
        [margin, margin, s - margin, s - margin],
        radius=round(s * 0.22),
        fill=bg,
    )

    nx = round(s * 0.255)
    x_long = round(s * 0.745)
    x_short = round(s * 0.545)
    thickness = max(2, round(s * 0.105))
    bars = 3 if size >= 24 else 2
    ys = (
        [round(s * 0.285), round(s * 0.465), round(s * 0.645)]
        if bars == 3
        else [round(s * 0.345), round(s * 0.585)]
    )
    for i, y in enumerate(ys):
        x_end = x_long if i < len(ys) - 1 else x_short
        draw.rounded_rectangle(
            [nx, y, x_end, y + thickness], radius=thickness // 2, fill=fg
        )

    if dot is not None:
        # 状态点：右下角（收句中）/ 右上角（异常）。外圈先描一圈底色当间隔，
        # 16px 下蓝底上的红点也分得清。
        radius = round(s * 0.155)
        cx = round(s * 0.755)
        cy = round(s * 0.755) if dot == RED else round(s * 0.245)
        ring = radius + max(1, round(s * 0.032))
        draw.ellipse([cx - ring, cy - ring, cx + ring, cy + ring], fill=bg)
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=dot)

    return img.resize((size, size), Image.LANCZOS)


def save_ico(path: Path, bg, fg, dot=None) -> None:
    frames = [render(n, bg, fg, dot) for n in SIZES]
    base = frames[-1]
    base.save(
        path, format="ICO", append_images=frames[:-1], sizes=[(n, n) for n in SIZES]
    )
    print(f"{path.name}: " + ", ".join(f"{n}px" for n in SIZES))


if __name__ == "__main__":
    out = Path(__file__).parent
    save_ico(out / "tray.ico", ACCENT, WHITE)
    save_ico(out / "tray-off.ico", GRAY_BG, GRAY_FG)
    save_ico(out / "tray-rec.ico", ACCENT, WHITE, RED)
    save_ico(out / "tray-alert.ico", ACCENT, WHITE, AMBER)
