"""
Tests for the price feed layer.

Yahoo is stubbed with pandas frames shaped the way yfinance actually returns
them, so these run offline. scripts/check_feed.py is the live check.

Everything here is about failure paths and the cache. The numbers do not
matter; what matters is that a student sees a sentence they can act on, and
that sixty students refreshing one ticker make one Yahoo request.
"""

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import prices


def _frame(symbols, days=200, blank=()):
    """A yfinance style frame: MultiIndex columns of (field, symbol).

    Symbols named in `blank` come back as a column of NaN, which is what Yahoo
    does for a ticker it does not recognise.
    """
    index = pd.bdate_range(end=dt.date.today(), periods=days)
    data = {}
    for field in ("Open", "High", "Low", "Close", "Volume"):
        for i, sym in enumerate(symbols):
            if sym in blank:
                data[(field, sym)] = [float("nan")] * days
            else:
                data[(field, sym)] = [100.0 + i * 10 + n * 0.1 for n in range(days)]
    return pd.DataFrame(data, index=index)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """No test may read or write the real on-disk cache."""
    monkeypatch.setattr(prices, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(prices, "_memory", {})
    yield


def _stub_yahoo(monkeypatch, frame, record=None):
    import yfinance as yf

    def fake_download(*args, **kwargs):
        if record is not None:
            record.append(kwargs.get("start"))
        if isinstance(frame, Exception):
            raise frame
        return frame

    monkeypatch.setattr(yf, "download", fake_download)


# --------------------------------------------------------------------------
# Messages a student can act on
# --------------------------------------------------------------------------

def test_the_benchmark_cannot_be_sized_against_itself(monkeypatch):
    """SPY vs SPY returns one column, not two, and used to raise ValueError."""
    _stub_yahoo(monkeypatch, _frame(["SPY"]))
    with pytest.raises(prices.PriceError) as exc:
        prices.get_series("SPY", "SPY")
    assert "benchmark" in str(exc.value)


def test_an_unrecognised_ticker_says_so(monkeypatch):
    _stub_yahoo(monkeypatch, _frame(["ZZZZFAKE", "SPY"], blank=["ZZZZFAKE"]))
    with pytest.raises(prices.PriceError) as exc:
        prices.get_series("ZZZZFAKE", "SPY")
    message = str(exc.value)
    assert "ZZZZFAKE" in message and "spelling" in message


def test_a_benchmark_outage_is_named_as_the_benchmark(monkeypatch):
    _stub_yahoo(monkeypatch, _frame(["NVDA", "SPY"], blank=["SPY"]))
    with pytest.raises(prices.PriceError) as exc:
        prices.get_series("NVDA", "SPY")
    assert "benchmark SPY" in str(exc.value)


def test_a_thin_overlap_reports_the_day_count(monkeypatch):
    _stub_yahoo(monkeypatch, _frame(["IPO", "SPY"], days=12))
    with pytest.raises(prices.PriceError) as exc:
        prices.get_series("IPO", "SPY")
    assert "12 overlapping trading days" in str(exc.value)


def test_yahoo_refusing_to_answer_is_not_a_stack_trace(monkeypatch):
    _stub_yahoo(monkeypatch, RuntimeError("connection reset"))
    with pytest.raises(prices.PriceError) as exc:
        prices.get_series("NVDA", "SPY")
    assert "rate limits" in str(exc.value)


def test_an_empty_ticker_is_refused_before_any_request(monkeypatch):
    calls = []
    _stub_yahoo(monkeypatch, _frame(["NVDA", "SPY"]), record=calls)
    with pytest.raises(prices.PriceError):
        prices.get_series("   ", "SPY")
    assert calls == []


# --------------------------------------------------------------------------
# The cache, which is the reason this is a server and not a static page
# --------------------------------------------------------------------------

def test_a_second_call_the_same_day_does_not_hit_the_network(monkeypatch, tmp_path):
    calls = []
    _stub_yahoo(monkeypatch, _frame(["NVDA", "SPY"]), record=calls)

    first = prices.get_series("NVDA", "SPY")
    monkeypatch.setattr(prices, "_memory", {})      # a fresh worker process
    second = prices.get_series("NVDA", "SPY")

    assert len(calls) == 1
    assert (tmp_path / "NVDA__SPY.json").exists()
    assert second.last_price == first.last_price
    assert second.stale is False


def test_a_stale_entry_is_served_and_flagged_when_yahoo_is_down(monkeypatch):
    _stub_yahoo(monkeypatch, _frame(["NVDA", "SPY"]))
    fresh = prices.get_series("NVDA", "SPY")

    # Age the cached entry, then take Yahoo away.
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    key = prices._key("NVDA", "SPY")
    payload = dict(prices._read_cache(key), as_of=yesterday)
    prices._write_cache(key, payload)
    monkeypatch.setattr(prices, "_memory", {})
    _stub_yahoo(monkeypatch, RuntimeError("connection reset"))

    stale = prices.get_series("NVDA", "SPY")
    assert stale.stale is True
    assert stale.as_of == yesterday
    assert stale.last_price == fresh.last_price


def test_with_no_cache_at_all_a_failure_is_raised_not_hidden(monkeypatch):
    _stub_yahoo(monkeypatch, RuntimeError("connection reset"))
    with pytest.raises(prices.PriceError):
        prices.get_series("NVDA", "SPY")


def test_the_ticker_is_normalised_before_it_reaches_the_cache_key(monkeypatch):
    _stub_yahoo(monkeypatch, _frame(["NVDA", "SPY"]))
    prices.get_series("  nvda  ", "spy")
    assert prices._read_cache("NVDA__SPY") is not None


# --------------------------------------------------------------------------
# The return window
# --------------------------------------------------------------------------

def test_the_window_returns_exactly_the_lookback_count(monkeypatch):
    _stub_yahoo(monkeypatch, _frame(["NVDA", "SPY"], days=200))
    series = prices.get_series("NVDA", "SPY")
    sec, ben = prices.window(series, 126)
    assert len(sec) == 126 and len(ben) == 126


def test_a_lookback_longer_than_the_history_says_what_is_available(monkeypatch):
    _stub_yahoo(monkeypatch, _frame(["NVDA", "SPY"], days=60))
    series = prices.get_series("NVDA", "SPY")
    with pytest.raises(prices.PriceError) as exc:
        prices.window(series, 252)
    assert "60 trading days" in str(exc.value)


# --------------------------------------------------------------------------
# Share classes: BRK.B to the world, BRK-B to Yahoo
# --------------------------------------------------------------------------

def _record_yahoo(monkeypatch, unknown=()):
    """Stub Yahoo, remember each symbol pair asked for, and return blanks for any
    symbol in `unknown`, which is how Yahoo answers a ticker it does not know."""
    import yfinance as yf
    asked = []

    def fake_download(symbols, *args, **kwargs):
        asked.append(list(symbols))
        return _frame(symbols, blank=[s for s in symbols if s in unknown])

    monkeypatch.setattr(yf, "download", fake_download)
    return asked


def test_a_share_class_typed_with_a_dot_is_fetched_under_yahoos_dash(monkeypatch):
    asked = _record_yahoo(monkeypatch, unknown={"BRK.B"})
    series = prices.get_series("BRK.B", "SPY")
    assert series.ticker == "BRK-B"
    assert asked == [["BRK-B", "SPY"]]


def test_a_foreign_listing_with_a_longer_suffix_is_left_alone(monkeypatch):
    asked = _record_yahoo(monkeypatch)
    prices.get_series("RY.TO", "SPY")
    assert asked == [["RY.TO", "SPY"]]


def test_a_one_letter_exchange_suffix_falls_back_to_the_ticker_as_typed(monkeypatch):
    """VOD.L looks like a share class. Its dash form is unknown, so the typed form is tried."""
    asked = _record_yahoo(monkeypatch, unknown={"VOD-L"})
    series = prices.get_series("VOD.L", "SPY")
    assert series.ticker == "VOD.L"
    assert asked == [["VOD-L", "SPY"], ["VOD.L", "SPY"]]


def test_an_unknown_share_class_is_reported_the_way_the_student_typed_it(monkeypatch):
    _record_yahoo(monkeypatch, unknown={"BRK.X", "BRK-X"})
    with pytest.raises(prices.PriceError) as exc:
        prices.get_series("BRK.X", "SPY")
    assert "BRK.X" in str(exc.value)

