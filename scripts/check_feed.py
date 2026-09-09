#!/usr/bin/env python3
"""Confirm the Yahoo feed works from wherever you run this.

    python scripts/check_feed.py NVDA

Run it locally before deploying, and again from a Render shell if the deployed
app cannot reach Yahoo. It prints the same numbers the app would compute.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import prices
import sizer

ticker = (sys.argv[1] if len(sys.argv) > 1 else "NVDA").upper()
lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 126

try:
    series = prices.get_series(ticker, "SPY")
except prices.PriceError as exc:
    print(f"FEED DOWN: {exc}")
    raise SystemExit(1)

sec, ben = prices.window(series, lookback)
r = sizer.size(
    security_returns=sec, benchmark_returns=ben,
    current_portfolio_weight=0.0, benchmark_weight=0.0,
    incremental_weight=0.0, portfolio_value=14_000_000, last_price=series.last_price,
)

print(f"{ticker} vs SPY, {lookback} day lookback")
print(f"  prices through {series.last_date}, last ${series.last_price:,.2f}"
      f"{'  (STALE CACHE)' if series.stale else ''}")
print(f"  vol {r.vol_security:.1%}   bmk vol {r.vol_benchmark:.1%}   "
      f"corr {r.corr:.2f}   beta {r.beta:.2f}")
print(f"  active volatility {r.active_vol:.1%}")
print("  ceilings from a standing start:")
for g in r.ladder:
    print(f"    {g.tier:<7} {g.ceiling_bps:>4.0f} bps  ->  "
          f"{g.ceiling_active_weight:>7.2%}  {g.trade_shares:>9,} shares"
          + (f"  (capped by {g.capped_by})" if g.capped_by else ""))
