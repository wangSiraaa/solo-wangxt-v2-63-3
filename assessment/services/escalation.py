"""
逾期升级批处理。

时间一律取自注入时钟（Clock），不直接读 timezone.now()，
因此可用 FixedClock 对同一批数据做确定性重放：
* 超过 SLA 未整改：每满一个 SLA 周期升一级（L1, L2, ...）；
* 已整改事件不再升级；
* 已达到的等级不重复升级（按 处罚单+等级 幂等）。
"""
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction

from assessment.models import EscalationRecord, ProblemEvent
from assessment.services.clock import Clock, SystemClock
from assessment.services.penalties import MAX_ESCALATION_LEVEL, escalate_penalty


@dataclass
class EscalationRunResult:
    created: list[EscalationRecord] = field(default_factory=list)
    inspected: int = 0
    open_overdue: int = 0

    @property
    def created_count(self) -> int:
        return len(self.created)


@transaction.atomic
def run_escalation(
    *,
    clock: Clock | None = None,
    default_sla_hours: int | None = None,
    actor: str = "escalation-job",
) -> EscalationRunResult:
    clock = clock or SystemClock()
    if default_sla_hours is None:
        default_sla_hours = int(settings.ASSESSMENT["DEFAULT_SLA_HOURS"])

    now = clock.now()
    result = EscalationRunResult()

    events = (
        ProblemEvent.objects.filter(status=ProblemEvent.Status.OPEN)
        .select_related("penalty")
    )
    for event in events:
        result.inspected += 1
        sla_hours = event.sla_hours or default_sla_hours
        deadline = event.occurred_at + timedelta(hours=sla_hours)
        if now <= deadline:
            continue
        result.open_overdue += 1

        overdue_seconds = (now - deadline).total_seconds()
        sla_seconds = sla_hours * 3600
        level = min(int(overdue_seconds // sla_seconds) + 1, MAX_ESCALATION_LEVEL)

        penalty = getattr(event, "penalty", None)
        if penalty is None:
            continue
        if level <= penalty.escalation_level:
            # 同一注入时刻重放 / 已升过该等级 -> 幂等跳过
            continue

        reason = (
            f"事件 {event.event_no} 超过 {sla_hours}h 整改时限未整改，"
            f"逾期 {int(overdue_seconds // 3600)}h，升级至 L{level}"
        )
        version = escalate_penalty(penalty, level=level, reason=reason, actor=actor)
        record = EscalationRecord.objects.create(
            penalty=penalty,
            level=level,
            version=version,
            reason=reason,
            actor=actor,
            ran_at=now,
        )
        result.created.append(record)

    return result
