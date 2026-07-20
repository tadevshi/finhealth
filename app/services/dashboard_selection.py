"""Pure dashboard selection and date-window policy."""

from __future__ import annotations

import calendar
import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.schemas.dashboard import CardFilter

_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


@dataclass(frozen=True, slots=True)
class YearMonth:
    """A calendar month represented as a year and month."""

    year: int
    month: int

    @classmethod
    def parse(cls, value: str) -> YearMonth:
        match = _PERIOD_RE.match(value)
        if match is None:
            raise ValueError(f"period must be an ISO YYYY-MM month label (got {value!r})")
        return cls(year=int(match.group(1)), month=int(match.group(2)))

    @classmethod
    def from_date(cls, value: date) -> YearMonth:
        return cls(year=value.year, month=value.month)

    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    def last_day(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    def iso(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True, slots=True)
class RangeMode:
    """Typed dashboard range mode."""

    kind: Literal["current", "rolling", "ytd", "all_time"]
    months: int | None = None

    @classmethod
    def current(cls) -> RangeMode:
        return cls(kind="current")

    @classmethod
    def rolling(cls, months: int) -> RangeMode:
        if months not in {3, 6, 12}:
            raise ValueError("rolling range must be one of 3, 6, or 12 months")
        return cls(kind="rolling", months=months)

    @classmethod
    def ytd(cls) -> RangeMode:
        return cls(kind="ytd")

    @classmethod
    def all_time(cls) -> RangeMode:
        return cls(kind="all_time")

    def wire_value(self) -> str:
        if self.kind == "rolling":
            return f"rolling_{self.months}"
        return self.kind

    def api_range(self) -> int:
        if self.kind == "all_time":
            return 0
        if self.kind == "rolling":
            assert self.months is not None
            return self.months
        if self.kind == "current":
            return 1
        return 0


@dataclass(frozen=True, slots=True)
class DashboardLabels:
    period_label: str
    card_label: str
    range_label: str


@dataclass(frozen=True, slots=True)
class DashboardSelection:
    period: YearMonth
    card_id: CardFilter
    range_mode: RangeMode

    def labels(self, *, card_name: str) -> DashboardLabels:
        month_name = calendar.month_name[self.period.month]
        return DashboardLabels(
            period_label=f"{month_name} {self.period.year}",
            card_label=card_name,
            range_label=range_mode_label(self.range_mode),
        )


def parse_card_filter(value: str) -> CardFilter:
    if value == "all" or value == "":
        return "all"
    return uuid.UUID(value)


def parse_range_mode(value: str) -> RangeMode:
    normalized = value.strip().lower()
    if normalized in {"", "current"}:
        return RangeMode.current()
    if normalized == "ytd":
        return RangeMode.ytd()
    if normalized == "all_time":
        return RangeMode.all_time()
    if normalized.startswith("rolling_"):
        return RangeMode.rolling(int(normalized.removeprefix("rolling_")))
    # Backward-compatible web values from the previous hidden range select.
    if normalized in {"3", "6", "12"}:
        return RangeMode.rolling(int(normalized))
    if normalized == "0":
        return RangeMode.ytd()
    raise ValueError(
        f"range_mode must be current, rolling_3, rolling_6, rolling_12, ytd, or all_time (got {value!r})"
    )


def parse_selection(*, period: str, card_id: str, range_mode: str) -> DashboardSelection:
    return DashboardSelection(
        period=YearMonth.parse(period),
        card_id=parse_card_filter(card_id),
        range_mode=parse_range_mode(range_mode),
    )


def from_api_range(range_months: int) -> RangeMode:
    if range_months == 0:
        return RangeMode.all_time()
    if range_months in {3, 6, 12}:
        return RangeMode.rolling(range_months)
    raise ValueError(f"range must be one of {{0, 3, 6, 12}} (got {range_months!r})")


def resolve_window(
    selection: DashboardSelection,
    *,
    today: date,
    earliest: date | None,
) -> tuple[date, date]:
    del today  # Window policy is anchored to the selected period, not wall-clock today.
    end = selection.period.last_day()
    mode = selection.range_mode
    if mode.kind == "current":
        return selection.period.first_day(), end
    if mode.kind == "ytd":
        return date(selection.period.year, 1, 1), end
    if mode.kind == "all_time":
        if earliest is None:
            return selection.period.first_day(), end
        return date(earliest.year, earliest.month, 1), end
    assert mode.months is not None
    start_year = selection.period.year
    start_month = selection.period.month - mode.months + 1
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    return date(start_year, start_month, 1), end


def range_mode_label(mode: RangeMode) -> str:
    if mode.kind == "current":
        return "Current month"
    if mode.kind == "ytd":
        return "Year to date"
    if mode.kind == "all_time":
        return "All time"
    return f"Last {mode.months} months"


def range_mode_options() -> tuple[tuple[str, str], ...]:
    return (
        ("current", "Current month"),
        ("rolling_3", "Last 3 months"),
        ("rolling_6", "Last 6 months"),
        ("rolling_12", "Last 12 months"),
        ("ytd", "YTD"),
        ("all_time", "All time"),
    )


__all__ = [
    "DashboardLabels",
    "DashboardSelection",
    "RangeMode",
    "YearMonth",
    "from_api_range",
    "parse_card_filter",
    "parse_range_mode",
    "parse_selection",
    "range_mode_label",
    "range_mode_options",
    "resolve_window",
]
