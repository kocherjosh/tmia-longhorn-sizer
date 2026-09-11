"""
Benchmark weights for the Longhorn sizer.

Pulls the benchmark ETF's full daily holdings file from State Street and answers
one question: what weight does this ticker carry in the benchmark? Students used
to type it, and a name left at zero reads as outside the benchmark, which
understates the active risk of any large S&P name the fund does not hold. Not
holding NVDA at an 8% benchmark weight is an 8% underweight, not a flat book.

This is public data, the same category as the price cache in prices.py. Nothing
about the fund is fetched or stored: no holdings, weights, or value of its own.
The file is cached server-side for the calendar day, and on a failed fetch the
last good file is served and flagged. Failing both, the student types the
weight, exactly as before this module existed.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)

CACHE_DIR = os.environ.get("SIZER_CACHE_DIR") or os.path.join(
    tempfile.gettempdir(), "tmia-sizer-cache"
)

# State Street publishes every SPDR fund's full holdings at this address, daily.
URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/"
       "fund-data/etfs/us/holdings-daily-us-en-{fund}.xlsx")

SOURCE = "State Street"

_lock = threading.Lock()
_memory: dict[str, dict] = {}


class BenchmarkError(Exception):
    """Raised when weights cannot be retrieved. The message is shown to students."""


@dataclass(frozen=True)
class Weight:
    """One name's weight in the benchmark, and where it came from."""

    ticker: str
    weight: float         # decimal: 0.0822 is 8.22%. Zero when not a holding.
    held: bool            # False when the benchmark does not hold the name at all
    as_of: str            # the date printed in the holdings file, e.g. 09-Sep-2026
    source: str
    stale: bool = False   # served from an earlier day after a failed fetch


def normalise(ticker: str) -> str:
    """One spelling per name.

    The holdings file writes share classes as BRK.B. Students type BRK.B or
    BRK-B, and Yahoo only knows BRK-B, so both are folded to the file's form.
    """
    return ticker.strip().upper().replace("-", ".")


def _type_it(benchmark: str) -> str:
    return (f"Type the benchmark weight in instead, or zero if the name is not "
            f"in the {benchmark}.")


# --------------------------------------------------------------------------
# Parse
# --------------------------------------------------------------------------

def parse(content: bytes, benchmark: str) -> tuple[str, dict[str, float]]:
    """The holdings file as (as_of, {ticker: decimal weight}).

    Refuses anything that does not look like the file this was written
    against, rather than guessing: a misread weight is worse than no weight.
    """
    try:
        import openpyxl
        sheet = openpyxl.load_workbook(io.BytesIO(content), read_only=True,
                                       data_only=True).active
        rows = list(sheet.iter_rows(values_only=True))
    except Exception as exc:
        raise BenchmarkError(
            f"{SOURCE}'s {benchmark} holdings file could not be read. "
            + _type_it(benchmark)
        ) from exc

    as_of = ""
    for row in rows[:10]:
        cells = [c for c in (row or ()) if c is not None]
        if len(cells) >= 2 and str(cells[0]).strip().lower().startswith("holdings"):
            as_of = str(cells[1]).replace("As of", "").strip()

    header = next((i for i, row in enumerate(rows)
                   if row and "Ticker" in row and "Weight" in row), None)
    if header is None:
        raise BenchmarkError(
            f"{SOURCE}'s {benchmark} holdings file has changed layout, so its "
            "weights were not trusted. " + _type_it(benchmark)
        )
    columns = {name: j for j, name in enumerate(rows[header]) if name}
    t_col, w_col = columns["Ticker"], columns["Weight"]

    weights: dict[str, float] = {}
    for row in rows[header + 1:]:
        if not row or len(row) <= max(t_col, w_col):
            continue
        ticker, weight = row[t_col], row[w_col]
        if not isinstance(ticker, str) or not isinstance(weight, (int, float)):
            continue
        if not ticker.strip() or ticker.strip() == "-":     # cash lines carry no ticker
            continue
        key = normalise(ticker)
        weights[key] = weights.get(key, 0.0) + float(weight) / 100.0

    # The file states weights in percent. If that ever changes, 8.22 would read
    # as 0.08% and every answer would be quietly wrong, so check the whole adds up.
    total = sum(weights.values())
    if not weights or not 0.95 <= total <= 1.05:
        raise BenchmarkError(
            f"{SOURCE}'s {benchmark} weights add up to {total:.1%} rather than "
            "the whole fund, so they were not trusted. " + _type_it(benchmark)
        )
    return as_of, weights


# --------------------------------------------------------------------------
# Fetch and cache
# --------------------------------------------------------------------------

def _download(benchmark: str) -> dict:
    """One State Street request. Raises BenchmarkError with a readable message."""
    import requests

    try:
        resp = requests.get(URL.format(fund=benchmark.lower()), timeout=20,
                            headers={"User-Agent": "Mozilla/5.0"})
    except Exception as exc:
        raise BenchmarkError(
            f"{SOURCE} did not answer, so the {benchmark} weight could not be "
            "looked up. " + _type_it(benchmark)
        ) from exc
    if resp.status_code != 200:
        raise BenchmarkError(
            f"{SOURCE} refused the {benchmark} holdings request (HTTP "
            f"{resp.status_code}). " + _type_it(benchmark)
        )
    as_of, weights = parse(resp.content, benchmark)
    return {"benchmark": benchmark, "as_of": as_of, "weights": weights,
            "fetched": dt.date.today().isoformat()}


def _path(benchmark: str) -> str:
    return os.path.join(CACHE_DIR, f"HOLDINGS__{benchmark}.json")


def _read(benchmark: str) -> dict | None:
    if benchmark in _memory:
        return _memory[benchmark]
    try:
        with open(_path(benchmark)) as fh:
            payload = json.load(fh)
        _memory[benchmark] = payload
        return payload
    except (OSError, ValueError):
        return None


def _write(benchmark: str, payload: dict) -> None:
    _memory[benchmark] = payload
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _path(benchmark)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)       # atomic, so concurrent workers cannot tear
    except OSError as exc:
        log.warning("could not write holdings cache: %s", exc)


def holdings(benchmark: str = "SPY") -> tuple[dict, bool]:
    """The benchmark's weights for today, and whether they are stale.

    One State Street request per benchmark per calendar day, however many
    students ask. On a failed fetch with an earlier day's file cached, that
    file is returned flagged stale rather than failing outright.
    """
    benchmark = benchmark.strip().upper()
    today = dt.date.today().isoformat()

    cached = _read(benchmark)
    if cached and cached.get("fetched") == today:
        return cached, False

    with _lock:
        # another worker may have filled it while we waited
        cached = _read(benchmark)
        if cached and cached.get("fetched") == today:
            return cached, False
        try:
            payload = _download(benchmark)
        except BenchmarkError:
            if cached:
                log.warning("serving stale %s holdings from %s", benchmark, cached["fetched"])
                return cached, True
            raise
        _write(benchmark, payload)
        return payload, False


def lookup(ticker: str, benchmark: str = "SPY") -> Weight:
    """This ticker's weight in the benchmark. Zero, and held=False, if it is not in it."""
    key = normalise(ticker)
    if not key:
        raise BenchmarkError("Enter a ticker.")
    payload, stale = holdings(benchmark)
    weight = payload["weights"].get(key)
    return Weight(
        ticker=key,
        weight=weight if weight is not None else 0.0,
        held=weight is not None,
        as_of=payload["as_of"],
        source=SOURCE,
        stale=stale,
    )
