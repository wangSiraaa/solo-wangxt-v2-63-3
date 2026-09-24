"""事件立案服务：照片 -> 事件 -> 处罚单元（含发生时合同归属）。"""
from django.db import transaction

from assessment.exceptions import DuplicateDedupKey, PhotoAlreadyLinked
from assessment.models import EvidencePhoto, ProblemEvent
from assessment.services.attribution import find_grid, responsible_contract
from assessment.services.penalties import create_penalty_for_event


@transaction.atomic
def create_event_from_photo(
    photo: EvidencePhoto,
    *,
    category: str = ProblemEvent.Category.OTHER,
    description: str = "",
    actor: str = "system",
    dedup_key: str | None = None,
    occurred_at=None,
) -> ProblemEvent:
    """
    用一张照片立案：
    * 发生时间默认取照片拍摄时间（因此承包商按拍摄时刻解析，而非录入时刻）；
    * 合同/承包商名称做快照存入事件与处罚单；
    * 同时创建唯一的处罚单元与初版扣分。
    """
    if photo.event_id is not None:
        raise PhotoAlreadyLinked(f"照片 {photo.id} 已关联事件 {photo.event_id}")

    occurred_at = occurred_at or photo.captured_at
    point = photo.location

    contract = responsible_contract(point=point, at=occurred_at)
    grid = find_grid(point)

    if dedup_key and ProblemEvent.objects.filter(dedup_key=dedup_key).exists():
        raise DuplicateDedupKey(f"去重键 {dedup_key} 已存在")

    event = ProblemEvent.objects.create(
        primary_photo=photo,
        grid=grid,
        contract=contract,
        contractor_name=contract.contractor_name,
        category=category,
        description=description,
        location=point,
        occurred_at=occurred_at,
        dedup_key=dedup_key or None,
    )

    photo.event = event
    photo.save(update_fields=["event"])

    create_penalty_for_event(event, actor=actor)
    return event
