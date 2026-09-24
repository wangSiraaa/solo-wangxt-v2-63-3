"""
模拟现场图片生成（纯 Pillow，确定性输出）。

场景设计对应业务测试（64 位 pHash，阈值 5）：
* scene_a_angle1 / scene_a_angle2 : 同一垃圾堆的不同角度（轻度透视+亮度，距离 0）
* scene_a_repost                  : 整改后同一位置复发（微位移+变暗，距离 2）
* scene_a_elsewhere_copy          : 跨地点误传——与 angle1 完全相同的像素（距离 0）
* scene_c_bins                    : 明显不同的另一处现场（距离 ~28，远超阈值）

大块低对比度结构 + 少量噪点，保证缩放/轻微透视后 DCT 低频稳定。
"""
import io
import random

from PIL import Image, ImageChops, ImageDraw, ImageEnhance

SIZE = 256
ASPHALT = (48, 50, 54)


def _asphalt(draw, rng):
    draw.rectangle([0, 0, SIZE, SIZE], fill=ASPHALT)
    for _ in range(120):
        x, y = rng.randint(0, SIZE - 1), rng.randint(0, SIZE - 1)
        shade = rng.randint(42, 62)
        draw.point((x, y), fill=(shade, shade, shade + 2))


def _garbage_pile(draw, rng, cx, cy, spread=70):
    # 大块暗底 + 若干大色块（缩放后低频分量稳定）
    draw.ellipse(
        [cx - spread, cy - spread // 3, cx + spread, cy + spread // 3],
        fill=(120, 105, 90),
    )
    for _ in range(14):
        x = cx + rng.randint(-int(spread * 0.7), int(spread * 0.7))
        y = cy + rng.randint(-spread // 4, spread // 4)
        r = rng.randint(10, 24)
        draw.ellipse(
            [x - r, y - r, x + r, y + r],
            fill=(rng.randint(150, 225), rng.randint(130, 200), rng.randint(90, 170)),
        )


def _scene_garbage_pile(seed: int = 7) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", (SIZE, SIZE))
    draw = ImageDraw.Draw(img)
    _asphalt(draw, rng)
    draw.line([(0, int(SIZE * 0.85)), (SIZE, int(SIZE * 0.80))], fill=(190, 190, 190), width=8)
    _garbage_pile(draw, rng, int(SIZE * 0.44), int(SIZE * 0.55))
    return img


def _scene_bins(seed: int = 21) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", (SIZE, SIZE))
    draw = ImageDraw.Draw(img)
    _asphalt(draw, rng)
    colors = [(42, 110, 60), (42, 90, 140), (130, 60, 50), (120, 110, 40)]
    for i, color in enumerate(colors):
        x0 = 20 + i * 58
        draw.rounded_rectangle([x0, 60, x0 + 50, 210], radius=8, fill=color)
        draw.rectangle(
            [x0 - 4, 46, x0 + 54, 64],
            fill=tuple(min(channel + 30, 255) for channel in color),
        )
    return img


def _variant(base: Image.Image, *, shear=0.0, translate=(0, 0), brightness=1.0, offset=(0, 0)):
    img = base
    if shear or translate != (0, 0):
        img = img.transform(
            (SIZE, SIZE),
            Image.AFFINE,
            (1.0, shear, translate[0], 0.0, 1.0, translate[1]),
            resample=Image.BICUBIC,
            fillcolor=ASPHALT,
        )
    if offset != (0, 0):
        img = ImageChops.offset(img, offset[0], offset[1])
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    return img


_BASE_A = _scene_garbage_pile()
_BASE_C = _scene_bins()

SCENES = {
    "scene_a_angle1": _BASE_A,
    # 不同拍摄角度：轻微斜切 + 亮度变化（pHash 距离 0，仍在阈值内）
    "scene_a_angle2": _variant(_BASE_A, shear=0.02, translate=(-2, 1), brightness=1.05),
    # 整改后复发：同一位置几乎同图，微位移 + 变暗（距离 2）
    "scene_a_repost": _variant(_BASE_A, offset=(2, 1), brightness=0.94),
    # 跨地点误传：文件像素与 angle1 完全一致（距离 0，但坐标在远处）
    "scene_a_elsewhere_copy": _BASE_A.copy(),
    # 另一处现场：明显不同（距离 ~28，远超阈值，不产生候选）
    "scene_c_bins": _BASE_C,
}

ALL_SCENES = list(SCENES)


def build_scene(name: str) -> Image.Image:
    if name not in SCENES:
        raise KeyError(f"未知场景 {name}，可选：{sorted(SCENES)}")
    return SCENES[name].copy()


def scene_png_bytes(name: str) -> bytes:
    buf = io.BytesIO()
    build_scene(name).save(buf, format="PNG")
    return buf.getvalue()
