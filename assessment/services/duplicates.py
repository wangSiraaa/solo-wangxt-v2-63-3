"""
疑似重复候选生成：**只用 pHash 相似度**筛选候选。

刻意不在这里使用位置/时间——位置与时间是人工判定阶段的依据，
避免“相似照片中的不同地点”被系统自动合并。
"""
from django.conf import settings

from assessment.models import DuplicateCandidate, EvidencePhoto
from assessment.services.phash import hamming_distance


def default_threshold() -> int:
    return int(settings.ASSESSMENT["PHASH_THRESHOLD"])


def generate_candidates_for_photo(
    photo: EvidencePhoto,
    *,
    threshold: int | None = None,
    limit: int = 50,
) -> list[DuplicateCandidate]:
    """
    新照片入库后调用：与全部历史照片比对 pHash 汉明距离，
    <= threshold 的生成 pending 候选（幂等，可重复调用）。
    """
    if threshold is None:
        threshold = default_threshold()

    target_bits = int(photo.phash, 16)
    scored = []
    for other in EvidencePhoto.objects.exclude(pk=photo.pk).only("id", "phash"):
        distance = bin(target_bits ^ int(other.phash, 16)).count("1")
        if distance <= threshold:
            scored.append((distance, other))

    scored.sort(key=lambda item: item[0])
    candidates = []
    for distance, other in scored[:limit]:
        candidate, created = DuplicateCandidate.objects.get_or_create(
            photo=photo,
            matched_photo=other,
            defaults={"hamming_distance": distance},
        )
        if not created and candidate.hamming_distance != distance:
            candidate.hamming_distance = distance
            candidate.save(update_fields=["hamming_distance"])
        candidates.append(candidate)
    return candidates
