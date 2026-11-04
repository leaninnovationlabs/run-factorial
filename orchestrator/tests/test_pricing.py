"""`worker.pricing` — the list-price estimate a codex run, or a live session, gets (tech-stack
decisions 34, 56).

Every expectation below is hand-computed from the table's own rates, so a test fails for a
rate change as surely as for an arithmetic slip.
"""

from __future__ import annotations

from decimal import Decimal

from orchestrator.worker.pricing import PRICES, estimate_cost


def test_gpt_5_3_codex_bills_uncached_cached_and_output_at_their_own_rates() -> None:
    # 600k uncached x $1.75 + 400k cached x $0.175 + 100k output x $14.00 = 1.05 + 0.07 + 1.40
    assert estimate_cost(
        "gpt-5.3-codex",
        input_tokens=1_000_000,
        cached_input_tokens=400_000,
        cache_write_input_tokens=0,
        output_tokens=100_000,
    ) == Decimal("2.5200")


def test_gpt_5_3_codex_has_no_cache_write_charge() -> None:
    """A write count on a model that does not bill writes is neither charged nor subtracted."""
    assert estimate_cost(
        "gpt-5.3-codex",
        input_tokens=1_000_000,
        cached_input_tokens=0,
        cache_write_input_tokens=100_000,
        output_tokens=0,
    ) == Decimal("1.7500")


def test_gpt_5_6_terra_bills_cache_writes_at_a_quarter_over_input() -> None:
    # (1M - 300k - 100k) x $2.00 + 300k x $0.20 + 100k x $2.50 + 50k x $12.00
    # = 1.20 + 0.06 + 0.25 + 0.60
    assert estimate_cost(
        "gpt-5.6-terra",
        input_tokens=1_000_000,
        cached_input_tokens=300_000,
        cache_write_input_tokens=100_000,
        output_tokens=50_000,
    ) == Decimal("2.1100")


def test_gpt_5_6_sol_bills_cache_writes_at_a_quarter_over_input() -> None:
    # (500k - 200k - 100k) x $4.00 + 200k x $0.40 + 100k x $5.00 + 10k x $20.00
    # = 0.80 + 0.08 + 0.50 + 0.20
    assert estimate_cost(
        "gpt-5.6-sol",
        input_tokens=500_000,
        cached_input_tokens=200_000,
        cache_write_input_tokens=100_000,
        output_tokens=10_000,
    ) == Decimal("1.5800")


def test_the_figure_is_quantised_to_the_column_half_up() -> None:
    """`step_run.cost_usd` is Numeric(10, 4). 125 terra input tokens are $0.00025, which
    half-even would round DOWN to 0.0002; a bill rounds up."""
    assert estimate_cost(
        "gpt-5.6-terra",
        input_tokens=125,
        cached_input_tokens=0,
        cache_write_input_tokens=0,
        output_tokens=0,
    ) == Decimal("0.0003")


def test_a_cached_count_above_the_gross_input_never_prices_negative() -> None:
    """A substrate that misreports the split must not produce a refund: the uncached part is
    clamped at zero and the cached part still bills."""
    cost = estimate_cost(
        "gpt-5.3-codex",
        input_tokens=100,
        cached_input_tokens=500,
        cache_write_input_tokens=0,
        output_tokens=0,
    )
    assert cost == Decimal("0.0001")  # 500 x $0.175 / 1M = 0.0000875, rounded up
    assert cost is not None and cost >= 0


def test_an_absent_cache_split_prices_as_uncached() -> None:
    """Cache counts are a split of the input; a substrate that reports none reports zero
    cached, not an unknown total."""
    assert estimate_cost(
        "gpt-5.3-codex",
        input_tokens=1_000_000,
        cached_input_tokens=None,
        cache_write_input_tokens=None,
        output_tokens=0,
    ) == Decimal("1.7500")


def test_unknown_model_or_missing_totals_is_no_estimate() -> None:
    """None, never zero: a run the table cannot price has an empty cost column."""
    tokens = {"cached_input_tokens": 0, "cache_write_input_tokens": 0}
    assert estimate_cost("gpt-4o", input_tokens=10, output_tokens=10, **tokens) is None
    assert estimate_cost("gpt-5.3-codex", input_tokens=None, output_tokens=10, **tokens) is None
    assert estimate_cost("gpt-5.3-codex", input_tokens=10, output_tokens=None, **tokens) is None


def test_the_table_carries_exactly_the_priced_models() -> None:
    assert set(PRICES) == {
        "gpt-5.3-codex",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    }
    assert PRICES["gpt-5.3-codex"].cache_write_multiplier is None
    assert PRICES["gpt-5.6-terra"].cache_write_multiplier == Decimal("1.25")
    assert PRICES["claude-haiku-4-5-20251001"] == PRICES["claude-haiku-4-5"]


def test_an_estimate_never_exceeds_the_column() -> None:
    """A live session's tokens come off a log the agent writes. A figure `step_run.cost_usd`
    cannot hold would fail every tick of the ticket."""
    cost = estimate_cost(
        "claude-opus-5-5",
        input_tokens=2**62,
        cached_input_tokens=0,
        cache_write_input_tokens=0,
        output_tokens=2**62,
    )
    assert cost == Decimal("999999.9999")
