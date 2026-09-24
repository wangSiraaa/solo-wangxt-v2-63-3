"""
基于 Pillow 的感知哈希(pHash)实现——仅依赖 Pillow，无需 numpy/OpenCV。

流程：灰度 -> 缩放到 64x64(hash_size*highfreq) -> 二维 DCT-II ->
取左上 16x16 低频块 -> 与中位数比较 -> 256 位整数。
"""
import math
from statistics import median

from PIL import Image

HASH_SIZE = 8
HIGH_FREQ_FACTOR = 4


def _dct_1d(vector):
    """一维正交归一化 DCT-II。"""
    n = len(vector)
    out = []
    for k in range(n):
        total = 0.0
        for i in range(n):
            total += vector[i] * math.cos(math.pi * (2 * i + 1) * k / (2.0 * n))
        scale = math.sqrt(1.0 / n) if k == 0 else math.sqrt(2.0 / n)
        out.append(scale * total)
    return out


def _dct_2d(matrix):
    """二维 DCT：对行、列分别做一维 DCT（可分离）。"""
    rows = [_dct_1d(row) for row in matrix]
    transposed = [list(col) for col in zip(*rows)]
    transposed = [_dct_1d(col) for col in transposed]
    return [list(row) for row in zip(*transposed)]


def compute_phash_bits(image_file, hash_size: int = HASH_SIZE, highfreq: int = HIGH_FREQ_FACTOR) -> int:
    """从类文件对象读取图片，返回 pHash 整数（256 bit）。"""
    image_file.seek(0)
    with Image.open(image_file) as img:
        img = img.convert("L").resize((hash_size * highfreq, hash_size * highfreq), Image.LANCZOS)
        side = hash_size * highfreq
        pixels = list(img.getdata())
    matrix = [list(pixels[r * side:(r + 1) * side]) for r in range(side)]

    dct = _dct_2d(matrix)
    low = [row[:hash_size] for row in dct[:hash_size]]
    flat = [value for row in low for value in row]
    mid = median(flat)

    bits = 0
    for value in flat:
        bits = (bits << 1) | (1 if value > mid else 0)
    return bits


def phash_to_hex(bits: int, hash_size: int = HASH_SIZE) -> str:
    return f"{bits:0{hash_size * hash_size // 4}x}"


def compute_phash_hex(image_file, hash_size: int = HASH_SIZE, highfreq: int = HIGH_FREQ_FACTOR) -> str:
    return phash_to_hex(compute_phash_bits(image_file, hash_size, highfreq), hash_size)


def hamming_distance(hex_a: str, hex_b: str) -> int:
    """两个等长 hex pHash 的汉明距离（不同位数）。"""
    return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")
