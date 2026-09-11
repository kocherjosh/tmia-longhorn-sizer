"""
End to end tests for the web app, with the price feed stubbed.

The feed is stubbed rather than called so these run offline and deterministically.
scripts/check_feed.py is the live check against Yahoo.
"""

import os
import sys
from base64 import b64encode
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["SIZER_PASSWORD"] = "testpw"
os.environ["SIZER_USERNAME"] = "longhorn"

import pytest

import app as webapp
import benchmark_weights
import prices

AUTH = {"Authorization": "Basic " + b64encode(b"longhorn:testpw").decode()}


def _fake_series(days=300):
    """Deterministic zigzag prices. Values do not matter, only that they parse."""
    sec, ben, dates = [], [], []
    for i in range(days):
        sec.append(100.0 * (1.0 + 0.004 * ((-1) ** i) + 0.0006 * i))
        ben.append(400.0 * (1.0 + 0.001 * ((-1) ** i) + 0.0003 * i))
        dates.append(f"2026-01-{(i % 28) + 1:02d}")
    return prices.Series(
        ticker="TEST", benchmark="SPY", dates=dates,
        security=sec, bench=ben, as_of="2026-09-09", stale=False,
    )


def _fake_weight(ticker, benchmark="SPY"):
    """A benchmark weight lookup that never leaves the machine."""
    return benchmark_weights.Weight(
        ticker=ticker.upper(), weight=0.0123, held=True,
        as_of="09-Sep-2026", source="State Street", stale=False,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(prices, "get_series", lambda t, b="SPY": _fake_series())
    monkeypatch.setattr(benchmark_weights, "lookup", _fake_weight)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        yield c


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def test_no_credentials_is_challenged(client):
    resp = client.get("/")
    assert resp.status_code == 401
    assert "Basic" in resp.headers["WWW-Authenticate"]


def test_wrong_password_is_refused(client):
    bad = {"Authorization": "Basic " + b64encode(b"longhorn:nope").decode()}
    assert client.get("/", headers=bad).status_code == 401


def test_correct_password_gets_the_form(client):
    resp = client.get("/", headers=AUTH)
    assert resp.status_code == 200
    assert b"Longhorn Fund Position Sizer" in resp.data


def test_server_refuses_to_serve_open_when_no_password_is_set(client, monkeypatch):
    monkeypatch.setattr(webapp, "PASSWORD", "")
    resp = client.get("/")
    assert resp.status_code == 503
    assert b"SIZER_PASSWORD" in resp.data


def test_healthz_needs_no_password(client):
    assert client.get("/healthz").status_code == 200


# --------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------

def _size(client, **params):
    q = {
        "ticker": "TEST", "lookback": "126", "portfolio_weight": "0.33",
        "benchmark_weight": "0", "fund_value": "14000000",
        "incremental_weight": "0.30",
    }
    q.update(params)
    return client.get("/", query_string=q, headers=AUTH)


def test_full_sizing_renders_every_section(client):
    resp = _size(client)
    assert resp.status_code == 200
    body = resp.data.decode()
    for heading in ("Risk inputs", "Where this position stands today",
                    "Conviction breakpoints", "The trade you are proposing",
                    "Trade ticket"):
        assert heading in body
    assert "Add / (trim) to get there" in body
    for tier in ("Low", "Medium", "High"):
        assert tier in body


def test_ladder_renders_without_a_proposed_trade(client):
    """A student reading the ladder should not have to invent a trade first."""
    resp = _size(client, incremental_weight="")
    body = resp.data.decode()
    assert "Conviction breakpoints" in body
    assert "The trade you are proposing" not in body


def test_percent_inputs_are_read_as_percent_not_decimal(client):
    """0.33 in the form means 33 bps of weight, not 33 percent."""
    resp = _size(client, incremental_weight="")
    body = resp.data.decode()
    assert "0.33%" in body


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "params,fragment",
    [
        ({"portfolio_weight": "abc"}, "must be a number"),
        ({"fund_value": "0"}, "greater than zero"),
        ({"portfolio_weight": "-1"}, "negative weight"),
        ({"portfolio_weight": "0.33", "incremental_weight": "-1"}, "below zero"),
        ({"lookback": "1"}, "at least two"),
    ],
)
def test_bad_input_shows_a_readable_error_not_a_stack_trace(client, params, fragment):
    resp = _size(client, **params)
    assert resp.status_code == 200
    assert fragment in resp.data.decode()
    assert "Traceback" not in resp.data.decode()


def test_currency_and_percent_symbols_are_tolerated(client):
    resp = _size(client, fund_value="$14,000,000", portfolio_weight="0.33%")
    assert "Cannot size this" not in resp.data.decode()


# --------------------------------------------------------------------------
# Feed failure paths
# --------------------------------------------------------------------------

def test_feed_failure_shows_the_message_and_the_manual_link(client, monkeypatch):
    def boom(t, b="SPY"):
        raise prices.PriceError("Yahoo did not answer.")

    monkeypatch.setattr(prices, "get_series", boom)
    body = _size(client).data.decode()
    assert "Yahoo did not answer." in body
    assert "Enter risk by hand" in body


def test_stale_cache_is_flagged_to_the_student(client, monkeypatch):
    stale = _fake_series()
    monkeypatch.setattr(
        prices, "get_series",
        lambda t, b="SPY": prices.Series(
            ticker=stale.ticker, benchmark=stale.benchmark, dates=stale.dates,
            security=stale.security, bench=stale.bench, as_of="2026-09-04",
            stale=True,
        ),
    )
    body = " ".join(_size(client).data.decode().split())
    assert "rather than today" in body
    assert "2026-09-04" in body


def test_manual_mode_needs_no_feed(client, monkeypatch):
    def boom(t, b="SPY"):
        raise AssertionError("manual mode must not touch the feed")

    monkeypatch.setattr(prices, "get_series", boom)
    resp = client.get(
        "/",
        query_string={
            "manual": "1", "lookback": "126", "vol_security": "47.4",
            "vol_benchmark": "14.0", "corr": "0.26", "last_price": "35.66",
            "portfolio_weight": "0.33", "benchmark_weight": "0",
            "fund_value": "14000000", "incremental_weight": "0.30",
        },
        headers=AUTH,
    )
    body = resp.data.decode()
    assert "Conviction breakpoints" in body
    # the workbook case, computed from inputs rounded to what a student would type
    assert "28.9" in body
    assert "45.8%" in body


def test_manual_mode_rejects_an_impossible_correlation(client):
    resp = client.get(
        "/",
        query_string={
            "manual": "1", "vol_security": "47.4", "vol_benchmark": "14.0",
            "corr": "1.4", "portfolio_weight": "0.33", "fund_value": "14000000",
        },
        headers=AUTH,
    )
    assert "between -1 and 1" in resp.data.decode()


# --------------------------------------------------------------------------
# Privacy posture
# --------------------------------------------------------------------------

def test_nothing_is_persisted(client, tmp_path, monkeypatch):
    """The app must not write student input anywhere."""
    monkeypatch.setattr(prices, "CACHE_DIR", str(tmp_path))
    _size(client)
    # only the price cache may ever appear here, and the stub never writes one
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# Starting values
# --------------------------------------------------------------------------

def test_empty_form_starts_at_zero_and_leaves_the_benchmark_to_be_looked_up(client):
    body = client.get("/", headers=AUTH).get_data(as_text=True)
    assert 'name="portfolio_weight" value="0"' in body
    assert 'name="benchmark_weight" value=""' in body
    assert 'name="incremental_weight" value="0"' in body
    assert 'name="fund_value" value="1000000"' in body


def test_the_starting_values_size_a_ticker_with_nothing_else_typed(client):
    resp = client.get(
        "/?ticker=TEST&lookback=126&portfolio_weight=0&benchmark_weight=0"
        "&incremental_weight=0&fund_value=1000000",
        headers=AUTH,
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Conviction breakpoints" in body
    assert "No active position" in body
    assert "The trade you are proposing" not in body     # zero means read the ladder only


# --------------------------------------------------------------------------
# Benchmark weight lookup
# --------------------------------------------------------------------------

BLANK_BENCHMARK = ("/?ticker=TEST&lookback=126&portfolio_weight=0&benchmark_weight="
                   "&incremental_weight=0&fund_value=1000000")


def test_a_blank_benchmark_weight_is_looked_up_and_dated(client):
    resp = client.get(BLANK_BENCHMARK, headers=AUTH)
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "1.23%" in body                      # the stubbed benchmark weight, used
    assert "09-Sep-2026" in body                # and dated, so its age is visible
    assert "underweight" in body                # and a zero holding is explained


def test_the_looked_up_weight_is_never_written_back_into_the_form(client):
    """Otherwise changing the ticker would carry the last name's weight across."""
    body = client.get(BLANK_BENCHMARK, headers=AUTH).get_data(as_text=True)
    assert 'name="benchmark_weight" value=""' in body


def test_a_typed_benchmark_weight_overrides_the_lookup(client, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("looked up despite a typed weight")
    monkeypatch.setattr(benchmark_weights, "lookup", must_not_run)
    resp = client.get(BLANK_BENCHMARK.replace("benchmark_weight=", "benchmark_weight=0.5"),
                      headers=AUTH)
    assert resp.status_code == 200
    assert "0.50%" in resp.get_data(as_text=True)


def test_a_failed_lookup_asks_for_the_weight_rather_than_assuming_zero(client, monkeypatch):
    def down(*args, **kwargs):
        raise benchmark_weights.BenchmarkError("State Street did not answer. Type it in.")
    monkeypatch.setattr(benchmark_weights, "lookup", down)
    body = client.get(BLANK_BENCHMARK, headers=AUTH).get_data(as_text=True)
    assert "State Street did not answer" in body
    assert "Conviction breakpoints" not in body


def test_manual_mode_asks_for_the_benchmark_weight_to_be_typed(client):
    body = client.get(
        "/?manual=1&vol_security=40&vol_benchmark=14&corr=0.3&lookback=126"
        "&portfolio_weight=0&benchmark_weight=&incremental_weight=0&fund_value=1000000",
        headers=AUTH,
    ).get_data(as_text=True)
    assert "Enter the benchmark weight" in body
    assert "Conviction breakpoints" not in body


def test_a_buy_into_an_underweight_says_it_lowers_risk_but_is_judged_where_it_lands(client):
    body = client.get(
        "/?ticker=TEST&lookback=126&portfolio_weight=0&benchmark_weight=5"
        "&incremental_weight=0.3&fund_value=1000000",
        headers=AUTH,
    ).get_data(as_text=True)
    assert "narrows an underweight" in body
    assert "applies to underweights too" in body

