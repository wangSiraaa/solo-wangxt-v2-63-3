"""
演示数据：一个道路网格 + 两个连续责任区间的承包商，便于手工演练 API。
"""
import json
from datetime import timedelta

from django.contrib.gis.geos import GEOSGeometry, Point
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone

from assessment.mockimages import scene_png_bytes
from assessment.models import CleaningContract, EvidencePhoto, RoadGrid
from assessment.services.duplicates import generate_candidates_for_photo
from assessment.services.phash import compute_phash_hex


class Command(BaseCommand):
    help = "写入演示网格/合同与一批模拟照片（不自动立案）"

    def handle(self, *args, **options):
        # 人民东路某段网格（矩形，EPSG:4326）
        polygon = GEOSGeometry(
            json.dumps({
                "type": "Polygon",
                "coordinates": [[
                    [121.4700, 31.2300],
                    [121.4800, 31.2300],
                    [121.4800, 31.2400],
                    [121.4700, 31.2400],
                    [121.4700, 31.2300],
                ]],
            }),
            srid=4326,
        )
        grid, _ = RoadGrid.objects.update_or_create(
            code="GRID-RMDL-01", defaults={"name": "人民东路一段", "geom": polygon},
        )

        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        CleaningContract.objects.update_or_create(
            code="C-2025-A",
            defaults={
                "grid": grid,
                "contractor_name": "甲保洁公司",
                "valid_from": now - timedelta(days=400),
                "valid_to": now - timedelta(days=30),
            },
        )
        CleaningContract.objects.update_or_create(
            code="C-2026-B",
            defaults={
                "grid": grid,
                "contractor_name": "乙保洁公司",
                "valid_from": now - timedelta(days=30),
                "valid_to": now + timedelta(days=335),
            },
        )

        samples = [
            ("scene_a_angle1.png", "scene_a_angle1", 31.2351, 121.4750, -36),
            ("scene_a_angle2.png", "scene_a_angle2", 31.2352, 121.4751, -36),
            ("scene_a_repost.png", "scene_a_repost", 31.2351, 121.4750, -1),
            ("scene_a_elsewhere_copy.png", "scene_a_elsewhere_copy", 31.2601, 121.5100, -2),
            ("scene_c_bins.png", "scene_c_bins", 31.2360, 121.4770, -10),
        ]
        for filename, scene, lat, lng, hours_ago in samples:
            data = scene_png_bytes(scene)
            photo = EvidencePhoto.objects.create(
                image=ContentFile(data, name=filename),
                phash=compute_phash_hex(ContentFile(data, name=filename)),
                captured_at=now + timedelta(hours=hours_ago),
                location=Point(lng, lat, srid=4326),
                note=f"演示照片 {scene}",
            )
            generate_candidates_for_photo(photo)

        self.stdout.write(self.style.SUCCESS(
            "演示数据已写入：网格 GRID-RMDL-01；合同 C-2025-A（甲）/ C-2026-B（乙）；5 张照片与候选。"
        ))
