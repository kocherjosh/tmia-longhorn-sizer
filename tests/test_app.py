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


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(prices, "get_series", lambda t, b="SPY": _fake_series())
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
