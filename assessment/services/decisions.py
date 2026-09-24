"""
疑似重复候选的人工判定。

三种处置（系统不自动合并事件）：
* attach      —— 确认同一问题不同角度：新照片挂到已有事件，不产生新扣分；
* create_new  —— 另立新事件：被匹配事件已整改 => 复发(recurrence)，
                 否则只是不同地点/不同问题(different)；
* different   —— 仅标记不合并（之后仍可对照片单独立案）。
"""
from django.db import transaction

from assessment.exceptions import InvalidDecision
from assessment.models import DuplicateCandidate
from assessment.services.clock import Clock, SystemClock
from assessment.services.events import create_event_from_photo
from assessment.models import ProblemEvent


def _assert_pending(candidate: DuplicateCandidate) -> None:
    if candidate.status != DuplicateCandidate.Status.PENDING:
        raise InvalidDecision(f"候选已判定为 {candidate.get_status_display()}，不能重复判定")


@transaction.atomic
def decide_candidate(
    candidate: DuplicateCandidate,
    *,
    action: str,
    actor: str = "system",
    category: str | None = None,
    description: str = "",
    dedup_key: str | None = None,
    note: str = "",
    clock: Clock | None = None,
) -> DuplicateCandidate:
    clock = clock or SystemClock()
    candidate = DuplicateCandidate.objects.select_for_update().get(pk=candidate.pk)
    _assert_pending(candidate)

    photo = candidate.photo
    matched = candidate.matched_photo

    if action == "attach":
        if matched.event_id is None:
            raise InvalidDecision("被匹配照片尚未关联事件，无法挂接")
        if photo.event_id is not None:
            raise InvalidDecision("新照片已关联事件，不能重复挂接")
        if matched.event.status == ProblemEvent.Status.RECTIFIED:
            raise InvalidDecision("被匹配事件已整改；同一位置复发属于新事件，应使用 create_new")
        photo.event = matched.event
        photo.save(update_fields=["event"])
        candidate.status = DuplicateCandidate.Status.CONFIRMED_DUPLICATE

    elif action == "create_new":
        if photo.event_id is not None:
            raise InvalidDecision("新照片已关联事件")
        create_category = category or (
            matched.event.category if matched.event_id else ProblemEvent.Category.OTHER
        )
        create_event_from_photo(
            photo,
            category=create_category,
            description=description,
            actor=actor,
            dedup_key=dedup_key,
        )
        if matched.event_id and matched.event.status == ProblemEvent.Status.RECTIFIED:
            candidate.status = DuplicateCandidate.Status.RECURRENCE
        else:
            candidate.status = DuplicateCandidate.Status.DIFFERENT

    elif action == "different":
        candidate.status = DuplicateCandidate.Status.DIFFERENT

    elif action == "rejected":
        candidate.status = DuplicateCandidate.Status.REJECTED

    else:
        raise InvalidDecision(f"未知判定动作 {action}")

    candidate.decision_note = note
    candidate.decided_by = actor
    candidate.decided_at = clock.now()
    candidate.save()
    return candidate
