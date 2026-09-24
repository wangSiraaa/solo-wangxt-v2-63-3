"""整改服务：每事件至多一条整改记录，重复回调幂等拒绝（409）。"""
from django.db import transaction

from assessment.exceptions import AlreadyRectified
from assessment.models import EvidencePhoto, ProblemEvent, Rectification
from assessment.services.clock import Clock, SystemClock


@transaction.atomic
def submit_rectification(
    event: ProblemEvent,
    *,
    note: str = "",
    actor: str = "",
    photo: EvidencePhoto | None = None,
    clock: Clock | None = None,
) -> Rectification:
    clock = clock or SystemClock()

    existing = Rectification.objects.filter(event=event).first()
    if existing is not None:
        raise AlreadyRectified(f"事件 {event.event_no} 已整改（整改记录 id={existing.id}），重复回调已忽略")

    rectification = Rectification.objects.create(
        event=event,
        photo=photo,
        note=note,
        submitted_by=actor,
        submitted_at=clock.now(),
    )
    event.status = ProblemEvent.Status.RECTIFIED
    event.save(update_fields=["status", "updated_at"])
    return rectification
