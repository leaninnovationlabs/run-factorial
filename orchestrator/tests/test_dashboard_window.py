"""The dashboard's window (tech-stack decision 61): whole UTC weeks, clipped to the first ticket."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator.api.services.dashboard import WindowBounds, window_bounds

#: A Wednesday afternoon; its week began on Monday 12 October.
AS_OF = datetime(2026, 10, 14, 15, 30, tzinfo=UTC)
LONG_AGO = datetime(2025, 1, 1, tzinfo=UTC)


def test_a_window_starts_on_a_monday_whole_weeks_back() -> None:
    """Week-aligned, so the weekly table is four whole rows and the current week, never a padding
    week or five rows for four weeks."""
    assert window_bounds("4w", AS_OF, LONG_AGO).since == datetime(2026, 9, 14, tzinfo=UTC)
    assert window_bounds("12w", AS_OF, LONG_AGO).since == datetime(2026, 7, 20, tzinfo=UTC)


def test_the_previous_period_is_as_long_and_ends_where_the_window_starts() -> None:
    assert window_bounds("4w", AS_OF, LONG_AGO) == WindowBounds(
        since=datetime(2026, 9, 14, tzinfo=UTC),
        previous_since=datetime(2026, 8, 14, 8, 30, tzinfo=UTC),
        clipped=False,
    )


def test_no_previous_period_when_it_would_start_before_the_first_ticket() -> None:
    """A change line against a part-period would read as growth."""
    assert window_bounds("4w", AS_OF, datetime(2026, 9, 1, tzinfo=UTC)) == WindowBounds(
        since=datetime(2026, 9, 14, tzinfo=UTC), previous_since=None, clipped=False
    )


def test_a_window_reaching_back_before_the_first_ticket_is_clipped_to_it() -> None:
    first = datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert window_bounds("4w", AS_OF, first) == WindowBounds(
        since=first, previous_since=None, clipped=True
    )


def test_all_time_starts_at_the_first_ticket_with_no_previous_period() -> None:
    assert window_bounds("all", AS_OF, LONG_AGO) == WindowBounds(
        since=LONG_AGO, previous_since=None, clipped=False
    )


def test_a_project_with_no_tickets_starts_its_window_now() -> None:
    for span in ("4w", "12w", "all"):
        assert window_bounds(span, AS_OF, None) == WindowBounds(
            since=AS_OF, previous_since=None, clipped=False
        )


def test_on_monday_at_midnight_the_window_is_whole_weeks_exactly() -> None:
    """The current week has no length yet, so 4 weeks is exactly 28 days and so is the period
    before it: velocity divides by four. Off by a day here, and every change line compares
    unequal periods."""
    monday = datetime(2026, 10, 12, tzinfo=UTC)
    bounds = window_bounds("4w", monday, LONG_AGO)
    assert bounds == WindowBounds(
        since=monday - timedelta(weeks=4),
        previous_since=monday - timedelta(weeks=8),
        clipped=False,
    )
    assert bounds.previous_since is not None
    assert bounds.since - bounds.previous_since == timedelta(weeks=4)
