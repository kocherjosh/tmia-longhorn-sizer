"""
Tests for the benchmark weight lookup.

State Street is stubbed with a holdings file built the way it publishes one, so
these run offline. scripts/check_feed.py is the live check.

The point is not the arithmetic, which is one division. It is that a student
gets the right weight whichever way they type a share class, that a file which
does not look right is refused rather than misread, and that a failure says so
instead of quietly returning zero.
"""

import datetime as dt
import io
import sys
from pathlib import Path

import openpyxl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import benchmark_weights as bw

HOLDINGS = [
    ("NVIDIA CORP", "NVDA", 8.22),
    ("BERKSHIRE HATHAWAY INC CL B", "BRK.B", 1.41),
    ("EVERYTHING ELSE", "REST", 90.32),
    ("US DOLLAR", "-", 0.05),          # cash line, which carries no ticker
]


def _ssga_file(rows=HOLDINGS, as_of="09-Sep-2026", header=True):
    """A holdings file laid out the way State Street publishes SPY's."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Fund Name:", "SPDR S&P 500 ETF Trust"])
    ws.append(["Ticker Symbol:", "SPY"])
    ws.append(["Holdings:", f"As of {as_of}"])
    ws.append([])
    if header:
        ws.append(["Name", "Ticker", "Identifier", "SEDOL", "Weight", "Sector",
                   "Shares Held", "Local Currency"])
    for name, ticker, weight in rows:
        ws.append([name, ticker, "id", "sedol", weight, "sector", 1000, "USD"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class _Response:
    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """No test may read or write the real on-disk cache."""
    monkeypatch.setattr(bw, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(bw, "_memory", {})
    yield


def _stub_state_street(monkeypatch, response):
    import requests
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append(url)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


# --------------------------------------------------------------------------
# The answer
# --------------------------------------------------------------------------

def test_a_held_name_comes_back_as_a_decimal_weight_with_the_files_date(monkeypatch):
    _stub_state_street(monkeypatch, _Response(_ssga_file()))
    w = bw.lookup("NVDA", "SPY")
    assert w.held is True
    assert w.weight == pytest.approx(0.0822)
    assert w.as_of == "09-Sep-2026"
    assert w.stale is False


@pytest.mark.parametrize("typed", ["BRK.B", "BRK-B", " brk.b "])
def test_a_share_class_matches_whichever_way_it_is_typed(monkeypatch, typed):
    _stub_state_street(monkeypatch, _Response(_ssga_file()))
    assert bw.lookup(typed, "SPY").weight == pytest.approx(0.0141)


def test_a_name_outside_the_benchmark_is_zero_and_says_so(monkeypatch):
    _stub_state_street(monkeypatch, _Response(_ssga_file()))
    w = bw.lookup("CRWV", "SPY")
    assert w.held is False
    assert w.weight == 0.0


def test_the_url_names_the_benchmark_fund(monkeypatch):
    calls = _stub_state_street(monkeypatch, _Response(_ssga_file()))
    bw.lookup("NVDA", "SPY")
    assert calls[0].endswith("holdings-daily-us-en-spy.xlsx")


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------

def test_the_file_is_fetched_once_a_day_not_once_a_student(monkeypatch, tmp_path):
    calls = _stub_state_street(monkeypatch, _Response(_ssga_file()))
    bw.lookup("NVDA", "SPY")
    monkeypatch.setattr(bw, "_memory", {})            # a fresh worker process
    bw.lookup("BRK.B", "SPY")
    assert len(calls) == 1
    assert (tmp_path / "HOLDINGS__SPY.json").exists()


def test_yesterdays_file_is_served_and_flagged_when_state_street_is_down(monkeypatch):
    _stub_state_street(monkeypatch, _Response(_ssga_file()))
    bw.lookup("NVDA", "SPY")

    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    bw._write("SPY", dict(bw._read("SPY"), fetched=yesterday))
    monkeypatch.setattr(bw, "_memory", {})
    _stub_state_street(monkeypatch, ConnectionError("connection reset"))

    w = bw.lookup("NVDA", "SPY")
    assert w.stale is True
    assert w.weight == pytest.approx(0.0822)


def test_with_nothing_cached_a_failure_asks_for_the_weight(monkeypatch):
    _stub_state_street(monkeypatch, ConnectionError("connection reset"))
    with pytest.raises(bw.BenchmarkError) as exc:
        bw.lookup("NVDA", "SPY")
    assert "Type the benchmark weight" in str(exc.value)


# --------------------------------------------------------------------------
# Files that do not look right are refused, not misread
# --------------------------------------------------------------------------

def test_a_refused_request_is_readable(monkeypatch):
    _stub_state_street(monkeypatch, _Response(b"", status_code=403))
    with pytest.raises(bw.BenchmarkError) as exc:
        bw.lookup("NVDA", "SPY")
    assert "403" in str(exc.value)


def test_a_file_that_has_changed_layout_is_refused(monkeypatch):
    _stub_state_street(monkeypatch, _Response(_ssga_file(header=False)))
    with pytest.raises(bw.BenchmarkError) as exc:
        bw.lookup("NVDA", "SPY")
    assert "changed layout" in str(exc.value)


def test_weights_that_do_not_add_up_to_the_fund_are_refused(monkeypatch):
    """If the file switched from percent to decimals, 8.22 would read as 0.08%."""
    as_decimals = [(name, ticker, weight / 100) for name, ticker, weight in HOLDINGS]
    _stub_state_street(monkeypatch, _Response(_ssga_file(as_decimals)))
    with pytest.raises(bw.BenchmarkError) as exc:
        bw.lookup("NVDA", "SPY")
    assert "add up to" in str(exc.value)


def test_something_that_is_not_a_spreadsheet_is_refused(monkeypatch):
    _stub_state_street(monkeypatch, _Response(b"<html>down for maintenance</html>"))
    with pytest.raises(bw.BenchmarkError):
        bw.lookup("NVDA", "SPY")
