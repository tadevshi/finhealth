"""Selection and range policy tests for dashboard hardening."""

from datetime import date

import pytest

from app.services.dashboard_selection import (
    DashboardSelection,
    RangeMode,
    YearMonth,
    from_api_range,
    parse_selection,
    resolve_window,
)


def test_resolver_ytd_all_time_and_api_zero_mapping() -> None:
    """Threat matrix: YTD, all-time and API range=0 semantics stay explicit."""
    period = YearMonth.parse("2026-07")

    ytd = DashboardSelection(period=period, card_id="all", range_mode=RangeMode.ytd())
    assert resolve_window(ytd, today=date(2026, 7, 15), earliest=None) == (
        date(2026, 1, 1),
        date(2026, 7, 31),
    )

    all_time = DashboardSelection(period=period, card_id="all", range_mode=RangeMode.all_time())
    assert resolve_window(all_time, today=date(2026, 7, 15), earliest=date(2025, 2, 10)) == (
        date(2025, 2, 1),
        date(2026, 7, 31),
    )

    assert from_api_range(0) == RangeMode.all_time()


@pytest.mark.parametrize("raw", ["2026-7", "2026-13", "bad"])
def test_selection_rejects_malformed_period(raw: str) -> None:
    """Threat matrix: malformed periods are rejected before route composition."""
    with pytest.raises(ValueError, match="period"):
        YearMonth.parse(raw)


def test_parse_selection_accepts_documented_web_modes() -> None:
    """Web selection supports current, rolling, YTD, and all-time modes."""
    assert (
        parse_selection(period="2026-07", card_id="all", range_mode="current").range_mode
        == RangeMode.current()
    )
    assert parse_selection(
        period="2026-07", card_id="all", range_mode="rolling_6"
    ).range_mode == RangeMode.rolling(6)
    assert (
        parse_selection(period="2026-07", card_id="all", range_mode="ytd").range_mode
        == RangeMode.ytd()
    )
    assert (
        parse_selection(period="2026-07", card_id="all", range_mode="all_time").range_mode
        == RangeMode.all_time()
    )


def test_labels_are_dynamic() -> None:
    """Selection labels come from the selected month, card and range mode."""
    selection = DashboardSelection(
        period=YearMonth.parse("2026-07"),
        card_id="all",
        range_mode=RangeMode.rolling(12),
    )

    labels = selection.labels(card_name="Todas")

    assert labels.period_label == "July 2026"
    assert labels.card_label == "Todas"
    assert labels.range_label == "Last 12 months"
