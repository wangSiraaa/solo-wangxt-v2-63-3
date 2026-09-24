"""可注入时钟。升级任务、判定/整改服务都通过它取当前时间，便于测试。"""
from datetime import datetime, timedelta

from django.utils import timezone


class Clock:
    """时钟协议：只要求实现 now()。"""

    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return timezone.now()


class FixedClock(Clock):
    """固定在某一时刻的时钟（测试/补跑用）。"""

    def __init__(self, instant: datetime):
        if timezone.is_naive(instant):
            instant = timezone.make_aware(instant)
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


class OffsetClock(Clock):
    """在系统时间基础上偏移（模拟“几天后”的逾期场景）。"""

    def __init__(self, offset: timedelta):
        self.offset = offset

    def now(self) -> datetime:
        return timezone.now() + self.offset


def resolve_clock(now: datetime | None = None) -> Clock:
    """API 传入显式时间时构造 FixedClock，否则用系统时钟。"""
    if now is None:
        return SystemClock()
    return FixedClock(now)
