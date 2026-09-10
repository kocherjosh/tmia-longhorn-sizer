"""
Regression tests against TMIA_Position_Sizing_Calculator_v7.xlsx.

The same constants hold in v6; the Longhorn Sizer tab is identical in both.

Every expected value here was read out of the recalculated workbook, not
computed by this code. If a test fails, the app and the workbook disagree and
one of them is wrong.

Workbook case: ticker Z against SPY, 126 trading day lookback, end date
2026-08-30, fund value $14,000,000, last price $35.66.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import sizer

# Risk inputs as the workbook computed them from the Bloomberg series.
VOL_S = 0.4736197585891236
VOL_B = 0.1400622700373325
CORR = 0.2622339341011068
BETA = 0.8867425362286215

FUND = 14_000_000.0
PRICE = 35.66


def run(port_w, bench_w, incr):
    return sizer.size_from_risk(
        vol_security=VOL_S,
        vol_benchmark=VOL_B,
        corr=CORR,
        beta_=BETA,
        observations=126,
        current_portfolio_weight=port_w,
        benchmark_weight=bench_w,
        incremental_weight=incr,
        portfolio_value=FUND,
        last_price=PRICE,
    )


def close(a, b, tol=1e-6):
    assert math.isclose(a, b, abs_tol=tol), f"{a} != {b}"


# --------------------------------------------------------------------------
# Risk inputs
# --------------------------------------------------------------------------

def test_active_volatility_matches_workbook():
    close(sizer.active_volatility(VOL_S, VOL_B, CORR), 0.4573203244140918)


def test_vol_correl_slope_match_excel_on_a_known_series():
    """Guards the statistics against Excel semantics, sample n-1 and SQRT(252)."""
    a = [0.01, -0.02, 0.015, 0.0, -0.005, 0.03, -0.01, 0.002]
    b = [0.008, -0.015, 0.02, 0.001, -0.004, 0.025, -0.012, 0.003]
    # values below are from Excel STDEV/CORREL/SLOPE evaluated on these series
    close(sizer.annualised_vol(a), 0.246912940122627, tol=1e-9)
    close(sizer.annualised_vol(b), 0.224459350440119, tol=1e-9)
    close(sizer.correlation(b, a), 0.977266789076897, tol=1e-9)
    close(sizer.beta(a, b), 1.07502679528403, tol=1e-9)


# --------------------------------------------------------------------------
# Base case: 0.33% held, not in the benchmark, proposing +0.30%
# --------------------------------------------------------------------------

def test_base_case_current_position():
    r = run(0.0033, 0.0, 0.003)
    close(r.current_active_weight, 0.0033)
    close(r.current_risk_bps, 15.091571, tol=1e-5)
    assert r.current_tier == "Medium"
    close(r.room_bps, 14.908429, tol=1e-5)


def test_base_case_ladder_ceilings():
    r = run(0.0033, 0.0, 0.003)
    close(r.ladder[0].ceiling_active_weight, 0.00328, tol=1e-5)
    close(r.ladder[1].ceiling_active_weight, 0.00656, tol=1e-5)
    close(r.ladder[2].ceiling_active_weight, 0.01312, tol=1e-5)


def test_base_case_ladder_trades():
    r = run(0.0033, 0.0, 0.003)
    close(r.ladder[0].trade_weight, -0.00002, tol=1e-5)
    close(r.ladder[1].trade_weight, 0.00326, tol=1e-5)
    close(r.ladder[2].trade_weight, 0.00982, tol=1e-5)
    close(r.ladder[0].trade_value, -280.326461, tol=1e-3)
    close(r.ladder[1].trade_value, 45639.347079, tol=1e-3)
    close(r.ladder[2].trade_value, 137478.694157, tol=1e-3)
    assert [g.trade_shares for g in r.ladder] == [-8, 1280, 3855]


def test_base_case_proposal():
    r = run(0.0033, 0.0, 0.003)
    close(r.new_active_weight, 0.0063)
    close(r.new_risk_bps, 28.811180, tol=1e-5)
    close(r.delta_risk_bps, 13.719610, tol=1e-5)
    assert r.required_tier == "Medium"
    assert r.required_votes == 8
    assert r.is_reduction is False
    assert r.within_cap is True


def test_base_case_trade_ticket():
    r = run(0.0033, 0.0, 0.003)
    close(r.trade_value, 42000.0, tol=1e-6)
    assert r.trade_shares == 1178
    close(r.target_value, 88200.0, tol=1e-6)
    assert r.target_shares == 2473


# --------------------------------------------------------------------------
# Edge cases, each verified in the workbook
# --------------------------------------------------------------------------

def test_underweight_ladder_floors_at_close_out():
    """0.10% held on a 0.50% benchmark name: an underweight, trimming 0.10%."""
    r = run(0.001, 0.005, -0.001)
    close(r.current_active_weight, -0.004)
    close(r.current_risk_bps, 18.292813, tol=1e-5)
    assert r.current_tier == "Medium"
    # Medium and High both floor at a full close-out, active weight -0.005
    close(r.ladder[0].ceiling_active_weight, -0.00328, tol=1e-5)
    close(r.ladder[1].ceiling_active_weight, -0.005, tol=1e-9)
    close(r.ladder[2].ceiling_active_weight, -0.005, tol=1e-9)
    close(r.ladder[1].ceiling_portfolio_weight, 0.0, tol=1e-9)
    assert r.ladder[1].capped_by == "close-out"
    assert [g.trade_shares for g in r.ladder] == [283, -393, -393]
    assert r.is_reduction is True
    assert r.required_votes == 4


def test_name_not_held_ladder_is_the_full_ceiling():
    r = run(0.0, 0.0, 0.005)
    close(r.current_risk_bps, 0.0)
    assert r.current_tier is None
    close(r.ladder[0].trade_weight, 0.00328, tol=1e-5)
    assert [g.trade_shares for g in r.ladder] == [1288, 2575, 5151]
    close(r.new_risk_bps, 22.866016, tol=1e-5)
    assert r.required_tier == "Medium"
    assert r.required_votes == 8


def test_position_above_high_shows_trims_on_every_rung():
    r = run(0.02, 0.0, -0.015)
    close(r.current_risk_bps, 91.464065, tol=1e-5)
    assert r.current_tier is None          # above High
    close(r.room_bps, 0.0)
    assert all(g.trade_weight < 0 for g in r.ladder)
    assert [g.trade_shares for g in r.ladder] == [-6564, -5277, -2701]
    close(r.delta_risk_bps, -68.598049, tol=1e-5)
    assert r.is_reduction is True
    assert r.required_votes == 12


def test_proposal_over_the_single_stock_cap():
    r = run(0.04, 0.0, 0.005)
    close(r.new_risk_bps, 205.794146, tol=1e-5)
    assert r.required_tier is None         # exceeds High, not permitted
    assert r.required_votes is None
    assert r.within_cap is False


def test_mandate_cap_binds_on_a_low_volatility_name():
    """A 5% active vol name would need more than 300 bps to reach High."""
    r = sizer.size_from_risk(
        vol_security=0.05, vol_benchmark=0.05, corr=0.99, beta_=1.0,
        observations=126, current_portfolio_weight=0.0, benchmark_weight=0.0,
        incremental_weight=0.0, portfolio_value=FUND, last_price=PRICE,
    )
    assert r.ladder[2].capped_by == "mandate"
    close(r.ladder[2].ceiling_active_weight, 0.03, tol=1e-9)


# --------------------------------------------------------------------------
# Tier boundaries
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "risk,tier",
    [(0.0, "Low"), (15.0, "Low"), (15.01, "Medium"), (30.0, "Medium"),
     (30.01, "High"), (60.0, "High"), (60.01, None)],
)
def test_tier_boundaries_are_inclusive_at_the_ceiling(risk, tier):
    assert sizer.tier_for_risk(risk) == tier


def test_excel_round_is_half_away_from_zero():
    assert sizer.excel_round(0.5) == 1
    assert sizer.excel_round(1.5) == 2      # Python's round() gives 2
    assert sizer.excel_round(2.5) == 3      # Python's round() gives 2
    assert sizer.excel_round(-2.5) == -3
