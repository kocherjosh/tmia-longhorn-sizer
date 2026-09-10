"""
Longhorn Fund position sizing model.

A direct port of the Longhorn Sizer tab of TMIA_Position_Sizing_Calculator_v7.xlsx.
Verified cell for cell against it by scripts/check_workbook.py.
Pure functions only: no network, no disk, no framework. Everything here is
testable against the workbook, and tests/test_sizer.py does exactly that.

Governing rules, from TMIA_Analytical_Canon PMC-9.3 and PMC-9.4 and
TMIA_Curriculum_Spine_v14:

  Tier    Boxes          Ceiling         YES votes
  Low     1              15 bps          4
  Medium  1 + 2          30 bps          8
  High    1 + 2 + 3      60 bps          12

Adds are tested on where the position LANDS, matching the workbook.
Reductions scale on the risk REMOVED, carry no pathway requirement, and use
the same 15 / 30 / 60 scale. See OPEN_CONFLICT below.

OPEN CONFLICT, unresolved as of 2026-09-09: PMC-9.4 is explicitly incremental
for reductions ("up to 15 bps standalone risk removed") while PMC-9.3 states
only "size cap" for adds. This module preserves v5, v6 and v7 workbook behaviour and
tests adds on the resulting position. A ruling is needed before the first live
vote cycle. Changing it is a one-line edit in required_tier().
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

TRADING_DAYS = 252

# Tier ceilings in basis points of standalone active risk.
TIERS = (
    ("Low", 15.0, "Box 1", 4),
    ("Medium", 30.0, "Boxes 1 + 2", 8),
    ("High", 60.0, "Boxes 1 + 2 + 3", 12),
)

# Single-stock active weight cap, in basis points. Investment Guidelines gate 2
# of the PMC-8.4 constraint stack. The workbook implements this as a flat
# absolute cap; the canon phrases it as "+/- 3% of benchmark weight,
# look-through". Carried forward from v5 pending confirmation.
MAX_ACTIVE_WEIGHT_BPS = 300.0


# --------------------------------------------------------------------------
# Risk inputs
# --------------------------------------------------------------------------

def annualised_vol(returns: list[float]) -> float:
    """Annualised standard deviation of daily simple returns.

    Matches Excel STDEV (sample, n-1 denominator) times SQRT(252).
    """
    n = len(returns)
    if n < 2:
        raise ValueError("need at least two returns to compute a volatility")
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def correlation(a: list[float], b: list[float]) -> float:
    """Pearson correlation. Matches Excel CORREL."""
    if len(a) != len(b):
        raise ValueError("series must be the same length")
    n = len(a)
    if n < 2:
        raise ValueError("need at least two observations")
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return 0.0
    return cov / math.sqrt(va * vb)


def beta(security: list[float], benchmark: list[float]) -> float:
    """Slope of security on benchmark. Matches Excel SLOPE(known_y, known_x)."""
    if len(security) != len(benchmark):
        raise ValueError("series must be the same length")
    n = len(benchmark)
    mb = sum(benchmark) / n
    ms = sum(security) / n
    denom = sum((x - mb) ** 2 for x in benchmark)
    if denom == 0:
        return 0.0
    return sum((x - mb) * (y - ms) for x, y in zip(benchmark, security)) / denom


def active_volatility(vol_security: float, vol_benchmark: float, corr: float) -> float:
    """Volatility of the security's return less the benchmark's.

    SQRT(vol_sec^2 + vol_bmk^2 - 2 * corr * vol_sec * vol_bmk).

    This is the only conversion in the model: active risk in bps equals
    |active weight| x active volatility x 10,000.
    """
    variance = (
        vol_security ** 2
        + vol_benchmark ** 2
        - 2.0 * corr * vol_security * vol_benchmark
    )
    return math.sqrt(max(variance, 0.0))


def simple_returns(prices: list[float]) -> list[float]:
    """Daily simple returns from a price series ordered oldest to newest."""
    return [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices))]


# --------------------------------------------------------------------------
# Tier lookups
# --------------------------------------------------------------------------

def tier_for_risk(risk_bps: float) -> str | None:
    """Which tier a given standalone active risk sits in. None if above High."""
    for name, ceiling, _boxes, _votes in TIERS:
        if risk_bps <= ceiling:
            return name
    return None


def room_in_tier(risk_bps: float) -> float:
    """Basis points of headroom before the position crosses into the next tier."""
    for _name, ceiling, _boxes, _votes in TIERS:
        if risk_bps <= ceiling:
            return ceiling - risk_bps
    return 0.0


def votes_for_tier(tier: str) -> int:
    for name, _ceiling, _boxes, votes in TIERS:
        if name == tier:
            return votes
    raise ValueError(f"unknown tier {tier!r}")


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rung:
    """One tier of the conviction ladder."""

    tier: str
    ceiling_bps: float
    boxes: str
    votes: int
    ceiling_active_weight: float   # decimal, signed in the position's direction
    ceiling_portfolio_weight: float
    trade_weight: float            # decimal, + to add, - to trim
    trade_value: float             # dollars
    trade_shares: int
    capped_by: str | None          # "mandate", "close-out", or None


@dataclass
class Result:
    # risk inputs
    vol_security: float
    vol_benchmark: float
    corr: float
    beta: float
    active_vol: float
    observations: int

    # where the position stands today
    current_portfolio_weight: float
    benchmark_weight: float
    current_active_weight: float
    current_risk_bps: float
    current_tier: str | None
    room_bps: float

    # the ladder
    ladder: list[Rung] = field(default_factory=list)

    # the proposed trade
    incremental_weight: float = 0.0
    new_portfolio_weight: float = 0.0
    new_active_weight: float = 0.0
    new_risk_bps: float = 0.0
    delta_risk_bps: float = 0.0
    required_tier: str | None = None
    required_votes: int | None = None
    is_reduction: bool = False
    within_cap: bool = True

    # trade ticket
    portfolio_value: float = 0.0
    last_price: float = 0.0
    trade_value: float = 0.0
    trade_shares: int = 0
    target_value: float = 0.0
    target_shares: int = 0


def excel_round(x: float) -> int:
    """Round half away from zero, matching Excel ROUND, not Python's round()."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _risk_bps(active_weight: float, active_vol: float) -> float:
    return abs(active_weight) * active_vol * 10_000.0


def _ceiling_weight(
    ceiling_bps: float,
    active_vol: float,
    benchmark_weight: float,
    direction: int,
) -> tuple[float, str | None]:
    """Signed active weight that consumes exactly `ceiling_bps` of risk.

    Two things can cap it. The mandate limits absolute active weight to
    MAX_ACTIVE_WEIGHT_BPS. On the underweight side the fund is long only, so the
    largest possible underweight is a full close-out, meaning an active weight of
    minus the benchmark weight.
    """
    if active_vol <= 0:
        return 0.0, None

    raw_bps = ceiling_bps / active_vol
    capped_by = None
    if raw_bps > MAX_ACTIVE_WEIGHT_BPS:
        raw_bps = MAX_ACTIVE_WEIGHT_BPS
        capped_by = "mandate"

    weight = direction * raw_bps / 10_000.0

    if direction < 0 and weight < -benchmark_weight:
        weight = -benchmark_weight
        capped_by = "close-out"

    return weight, capped_by


def required_tier(
    incremental_weight: float,
    new_risk_bps: float,
    delta_risk_bps: float,
) -> tuple[str | None, int | None, bool]:
    """Tier and YES votes a proposal requires.

    Returns (tier, votes, is_reduction). Tier is None when no trade is proposed
    or when the proposal exceeds the High ceiling and is therefore not permitted.

    Adds are tested on the resulting position. Reductions are tested on the
    magnitude of risk removed and need no pathway boxes. See OPEN_CONFLICT in the
    module docstring.
    """
    if incremental_weight == 0:
        return None, None, False

    if incremental_weight < 0:
        magnitude = abs(delta_risk_bps)
        tier = tier_for_risk(magnitude) or "High"   # a close-out is always 12 YES
        return tier, votes_for_tier(tier), True

    tier = tier_for_risk(new_risk_bps)
    if tier is None:
        return None, None, False
    return tier, votes_for_tier(tier), False


def size(
    *,
    security_returns: list[float],
    benchmark_returns: list[float],
    current_portfolio_weight: float,
    benchmark_weight: float,
    incremental_weight: float,
    portfolio_value: float,
    last_price: float,
) -> Result:
    """Run the full model from return series. Weights are decimals: 0.0033 is 33 bps."""
    return size_from_risk(
        vol_security=annualised_vol(security_returns),
        vol_benchmark=annualised_vol(benchmark_returns),
        corr=correlation(benchmark_returns, security_returns),
        beta_=beta(security_returns, benchmark_returns),
        observations=len(security_returns),
        current_portfolio_weight=current_portfolio_weight,
        benchmark_weight=benchmark_weight,
        incremental_weight=incremental_weight,
        portfolio_value=portfolio_value,
        last_price=last_price,
    )


def size_from_risk(
    *,
    vol_security: float,
    vol_benchmark: float,
    corr: float,
    beta_: float,
    observations: int,
    current_portfolio_weight: float,
    benchmark_weight: float,
    incremental_weight: float,
    portfolio_value: float,
    last_price: float,
) -> Result:
    """Run the model from pre-computed risk inputs.

    This is the entry point the app uses when the price feed is unavailable and
    the student supplies volatilities and correlation by hand.
    """
    vol_s, vol_b, bta = vol_security, vol_benchmark, beta_
    avol = active_volatility(vol_s, vol_b, corr)

    current_active = current_portfolio_weight - benchmark_weight
    current_risk = _risk_bps(current_active, avol)

    # The ladder runs in the direction of the current position. A benchmark name
    # held at zero is an underweight, so the ladder runs short of the benchmark.
    direction = -1 if current_active < 0 else 1

    ladder: list[Rung] = []
    for name, ceiling_bps, boxes, votes in TIERS:
        ceiling_weight, capped_by = _ceiling_weight(
            ceiling_bps, avol, benchmark_weight, direction
        )
        trade_weight = ceiling_weight - current_active
        trade_value = trade_weight * portfolio_value
        shares = excel_round(trade_value / last_price) if last_price > 0 else 0
        ladder.append(
            Rung(
                tier=name,
                ceiling_bps=ceiling_bps,
                boxes=boxes,
                votes=votes,
                ceiling_active_weight=ceiling_weight,
                ceiling_portfolio_weight=ceiling_weight + benchmark_weight,
                trade_weight=trade_weight,
                trade_value=trade_value,
                trade_shares=shares,
                capped_by=capped_by,
            )
        )

    new_portfolio = current_portfolio_weight + incremental_weight
    new_active = current_active + incremental_weight
    new_risk = _risk_bps(new_active, avol)
    delta_risk = new_risk - current_risk

    tier, votes, is_reduction = required_tier(
        incremental_weight, new_risk, delta_risk
    )

    trade_value = incremental_weight * portfolio_value
    target_value = new_portfolio * portfolio_value

    return Result(
        vol_security=vol_s,
        vol_benchmark=vol_b,
        corr=corr,
        beta=bta,
        active_vol=avol,
        observations=observations,
        current_portfolio_weight=current_portfolio_weight,
        benchmark_weight=benchmark_weight,
        current_active_weight=current_active,
        current_risk_bps=current_risk,
        current_tier=tier_for_risk(current_risk) if current_risk > 0 else None,
        room_bps=room_in_tier(current_risk),
        ladder=ladder,
        incremental_weight=incremental_weight,
        new_portfolio_weight=new_portfolio,
        new_active_weight=new_active,
        new_risk_bps=new_risk,
        delta_risk_bps=delta_risk,
        required_tier=tier,
        required_votes=votes,
        is_reduction=is_reduction,
        within_cap=abs(new_active) * 10_000.0 <= MAX_ACTIVE_WEIGHT_BPS,
        portfolio_value=portfolio_value,
        last_price=last_price,
        trade_value=trade_value,
        trade_shares=excel_round(trade_value / last_price) if last_price > 0 else 0,
        target_value=target_value,
        target_shares=excel_round(target_value / last_price) if last_price > 0 else 0,
    )
