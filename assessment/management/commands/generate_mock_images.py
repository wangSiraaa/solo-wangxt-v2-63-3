"""生成模拟现场图片到 media/mock_images/，并打印各场景间的 pHash 汉明距离。"""
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from assessment.mockimages import ALL_SCENES, scene_png_bytes
from assessment.services.phash import compute_phash_hex, hamming_distance


class Command(BaseCommand):
    help = "生成 5 张模拟现场照片（同角度/不同角度/复发/跨地点误传/不同现场）"

    def handle(self, *args, **options):
        import os

        from django.conf import settings

        out_dir = os.path.join(settings.MEDIA_ROOT, "mock_images")
        os.makedirs(out_dir, exist_ok=True)

        hashes = {}
        for name in ALL_SCENES:
            data = scene_png_bytes(name)
            path = os.path.join(out_dir, f"{name}.png")
            with open(path, "wb") as fh:
                fh.write(data)
            hashes[name] = compute_phash_hex(ContentFile(data, name=f"{name}.png"))
            self.stdout.write(self.style.SUCCESS(f"已生成 {path}"))

        self.stdout.write("\npHash 汉明距离矩阵（阈值默认 5）：")
        names = list(hashes)
        self.stdout.write(" " * 26 + "".join(f"{n[:14]:>16}" for n in names))
        for n1 in names:
            row = f"{n1:>26}"
            for n2 in names:
                row += f"{hamming_distance(hashes[n1], hashes[n2]):>16}"
            self.stdout.write(row)
