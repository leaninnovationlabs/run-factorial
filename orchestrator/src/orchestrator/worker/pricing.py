"""List prices for a run whose substrate reports tokens and no dollar figure.

Codex is the case (tech-stack decision 34): `turn.completed` carries a token split and nothing
else, so the worker prices the run itself and labels the row `cost_source = 'estimated'`. The
figure is notional on a subscription — usage already paid for in a plan, priced as if it were
API credits — which is the same claim the Claude CLI's own `total_cost_usd` makes, and the
operator chose one rule for every codex run over an empty column. A live session
of either CLI is the other case (decision 56): Claude's CLI writes its cost only at exit, so
the figure while it runs is priced here too, and the end's report replaces it.

OpenAI rates fetched 2026-09-09 from the model pages under
https://developers.openai.com/api/docs/models (gpt-5.3-codex, gpt-5.6-terra, gpt-5.6-sol) and
https://developers.openai.com/api/docs/pricing; gpt-5.6-sol's rate is promotional, at least
through 2026-11-21. Anthropic rates checked against
https://platform.claude.com/docs/en/about-claude/pricing on 2026-10-03; a model whose API id
is not certain is left out, and so is priced as unknown.
Rates are USD per million tokens. Re-fetch and re-date when a rate moves; nothing here is live.

The billing rule, per both pricing pages: input is billed net of cached reads, cached reads at
their own rate, and where a cache WRITE is billed (the 5.6 family, every Claude model) it bills
at 1.25x input and is not part of the uncached input either. Claude's 1.25x is the 5-minute
cache's; a 1-hour write bills 2x, so a session that writes those is priced low. Output already
includes reasoning tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Final

from orchestrator.db.models import CostSource, StepRun

__all__ = ["PRICES", "Price", "estimate_cost", "record_usage"]

_PER_MILLION: Final = Decimal(1_000_000)
#: `step_run.cost_usd` is Numeric(10, 4); the estimate is rounded to the column, up on a half —
#: a bill rounds up, and half-even would make a run's tenth-of-a-cent depend on its neighbour.
_CENTS: Final = Decimal("0.0001")
#: The most that column holds. Tokens counted off an agent-written log can claim any number,
#: and a figure the column refuses would fail every tick of the ticket.
_COLUMN_MAX: Final = Decimal("999999.9999")
#: The most a `step_run` token column (bigint) holds.
_COUNT_MAX: Final = 2**63 - 1


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens. `cache_write_multiplier` is the premium over `input` a cache
    write bills at, or None for a model that does not bill writes at all."""

    input: Decimal
    cached_input: Decimal
    output: Decimal
    cache_write_multiplier: Decimal | None = None


def _claude(fresh: str, read: str, out: str) -> Price:
    return Price(
        input=Decimal(fresh),
        cached_input=Decimal(read),
        output=Decimal(out),
        cache_write_multiplier=Decimal("1.25"),
    )


#: Input, cache read, output. A model id is matched exactly, so a dated one is listed too.
_CLAUDE: Final = {
    "claude-fable-5-1": ("10.00", "0.25", "50.00"),
    "claude-fable-5": ("10.00", "1.00", "50.00"),
    "claude-opus-5-5": ("4.00", "0.20", "20.00"),
    "claude-opus-5": ("5.00", "0.50", "25.00"),
    "claude-opus-4-8": ("5.00", "0.50", "25.00"),
    "claude-opus-4-7": ("5.00", "0.50", "25.00"),
    "claude-opus-4-6": ("5.00", "0.50", "25.00"),
    "claude-sonnet-5-5": ("2.00", "0.20", "10.00"),
    "claude-sonnet-5": ("2.00", "0.20", "10.00"),
    "claude-sonnet-4-6": ("3.00", "0.30", "15.00"),
    "claude-haiku-4-5": ("1.00", "0.10", "5.00"),
    "claude-haiku-4-5-20251001": ("1.00", "0.10", "5.00"),
}

PRICES: Final[MappingProxyType[str, Price]] = MappingProxyType(
    {
        "gpt-5.3-codex": Price(
            input=Decimal("1.75"), cached_input=Decimal("0.175"), output=Decimal("14.00")
        ),
        "gpt-5.6-terra": Price(
            input=Decimal("2.00"),
            cached_input=Decimal("0.20"),
            output=Decimal("12.00"),
            cache_write_multiplier=Decimal("1.25"),
        ),
        "gpt-5.6-sol": Price(
            input=Decimal("4.00"),
            cached_input=Decimal("0.40"),
            output=Decimal("20.00"),
            cache_write_multiplier=Decimal("1.25"),
        ),
        **{model: _claude(*rates) for model, rates in _CLAUDE.items()},
    }
)


def estimate_cost(
    model: str | None,
    *,
    input_tokens: int | None,
    cached_input_tokens: int | None,
    cache_write_input_tokens: int | None,
    output_tokens: int | None,
) -> Decimal | None:
    """The run's list price, or None for a model the table does not know or totals it lacks.

    `input_tokens` is GROSS of the cache split (codex's shape); the cache counts default to
    zero because they are parts of a reported total, not totals of their own. The uncached
    part is clamped at zero so a misreported split can never price a run negative.
    """
    if model is None or input_tokens is None or output_tokens is None:
        return None
    price = PRICES.get(model)
    if price is None:
        return None
    cached = cached_input_tokens or 0
    # A model that does not bill writes leaves them inside the uncached input.
    written = (cache_write_input_tokens or 0) if price.cache_write_multiplier is not None else 0
    uncached = max(0, input_tokens - cached - written)

    cost = (
        Decimal(uncached) * price.input
        + Decimal(cached) * price.cached_input
        + Decimal(written) * price.input * (price.cache_write_multiplier or 0)
        + Decimal(output_tokens) * price.output
    ) / _PER_MILLION
    return min(cost.quantize(_CENTS, rounding=ROUND_HALF_UP), _COLUMN_MAX)


def record_usage(
    run: StepRun,
    *,
    input_tokens: int | None,
    cached_input_tokens: int | None,
    cache_write_input_tokens: int | None,
    output_tokens: int | None,
    reported: Decimal | None,
) -> None:
    """A run's tokens and cost onto its row: the substrate's own figure when it reported one,
    else the list price for the model stamped at spawn, and `cost_source` says which.

    Both a live session's figures and the end's are read off a log the agent writes, so each
    is held to what its column takes: one the column refuses fails every tick of the ticket.
    """
    input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens = (
        _count(input_tokens),
        _count(cached_input_tokens),
        _count(cache_write_input_tokens),
        _count(output_tokens),
    )
    run.input_tokens = input_tokens
    run.cached_input_tokens = cached_input_tokens
    run.output_tokens = output_tokens
    if reported is not None:
        run.cost_usd = min(max(reported, Decimal(0)), _COLUMN_MAX)
        run.cost_source = CostSource.REPORTED
        return
    run.cost_usd = estimate_cost(
        run.model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        output_tokens=output_tokens,
    )
    run.cost_source = None if run.cost_usd is None else CostSource.ESTIMATED


def _count(value: int | None) -> int | None:
    return None if value is None else min(max(value, 0), _COUNT_MAX)
