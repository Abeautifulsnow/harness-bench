"""最小 5 字段 cron 解析（V2 Scheduled Evaluation）。

只支持数字字段（不支持 SUN/MON 名称）：
    minute hour day-of-month month day-of-week
每个字段支持 ``*``、``*/n``、``a``、``a-b`` 与逗号组合。语义遵循经典 cron：
dom 与 dow **都被显式限制**时取并集（Vixie cron 规则），否则取交集。

时区：本地时间。DST 折返日可能出现"少跑/多跑一次"——文档 §58 有记录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_FIELD_RANGE = {"minute": (0, 59), "hour": (0, 23), "day": (1, 31), "month": (1, 12), "dow": (0, 6)}
_FIELD_RE = re.compile(r"^(?:(\d+)|(\*)|(?:\*(?:/(\d+))?))$")
_NO_OCCURRENCE = "cron expression has no occurrence within a year"


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    day_wildcard: bool
    dow_wildcard: bool

    @classmethod
    def parse(cls, expr: str) -> CronSpec:
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"cron expression must have 5 fields: {expr!r}")
        minutes, wild_m = _parse_field(fields[0], *_FIELD_RANGE["minute"])
        hours, wild_h = _parse_field(fields[1], *_FIELD_RANGE["hour"])
        days, wild_d = _parse_field(fields[2], *_FIELD_RANGE["day"])
        months, wild_mo = _parse_field(fields[3], *_FIELD_RANGE["month"])
        dows, wild_dw = _parse_field(fields[4], *_FIELD_RANGE["dow"])
        return cls(
            minutes=frozenset(minutes),
            hours=frozenset(hours),
            days=frozenset(days),
            months=frozenset(months),
            dows=frozenset(dows),
            day_wildcard=wild_d,
            dow_wildcard=wild_dw,
        )

    def next_after(self, start: datetime) -> datetime:
        """start 之后（严格大于）的下一个触发时刻（本地时间，秒位归零）。"""
        candidate = (start + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = start + timedelta(days=366)
        while candidate < limit:
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError(_NO_OCCURRENCE)

    def matches(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        dom_hit = moment.day in self.days
        # python 的 Monday=0；cron 的 Sunday=0 → (weekday + 1) % 7 对齐
        dow_hit = (moment.weekday() + 1) % 7 in self.dows
        # Vixie 规则：dom 与 dow 都被显式限制时命中其一即可，否则要求同时命中
        if self.day_wildcard and self.dow_wildcard:
            return True
        if self.day_wildcard:
            return dow_hit
        if self.dow_wildcard:
            return dom_hit
        return dom_hit or dow_hit


def _parse_field(field: str, lo: int, hi: int) -> tuple[set[int], bool]:
    values: set[int] = set()
    for part in field.split(","):
        values |= _parse_single(part.strip(), lo, hi)
    if not values:
        raise ValueError(f"empty cron field: {field!r}")
    return values, field.strip() == "*"


def _parse_single(part: str, lo: int, hi: int) -> set[int]:
    step = 1
    body = part
    if "/" in part:
        body, raw_step = part.split("/", 1)
        step = int(raw_step)
        if step < 1:
            raise ValueError(f"cron step must be >= 1: {part!r}")
        if body == "":
            body = "*"
    if body == "*":
        return set(range(lo, hi + 1, step))
    if "-" in body:
        first, last = (int(token) for token in body.split("-", 1))
    else:
        first = last = int(body)
    if not (lo <= first <= hi and lo <= last <= hi):
        raise ValueError(f"cron field value out of range [{lo}, {hi}]: {part!r}")
    if first > last:
        raise ValueError(f"cron range must be ascending: {part!r}")
    return set(range(first, last + 1, step))
