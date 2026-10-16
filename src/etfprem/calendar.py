"""A 股交易日（净值日）与美股交易日的对齐。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date
from typing import Iterable


class Calendar:
    def __init__(self, cn_days: Iterable[date], us_days: Iterable[date]):
        self.cn = sorted(set(cn_days))
        self.us = sorted(set(us_days))

    def prev_cn(self, d: date) -> date | None:
        i = bisect_left(self.cn, d)
        return self.cn[i - 1] if i > 0 else None

    def next_cn(self, d: date) -> date | None:
        i = bisect_right(self.cn, d)
        return self.cn[i] if i < len(self.cn) else None

    def us_on_or_before(self, d: date) -> date | None:
        i = bisect_right(self.us, d)
        return self.us[i - 1] if i > 0 else None

    def us_between(self, a: date, b: date) -> list[date]:
        """美股交易日 s，满足 a < s <= b。"""
        return self.us[bisect_right(self.us, a) : bisect_right(self.us, b)]
