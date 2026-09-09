"""
Price feed for the Longhorn sizer.

Pulls split and dividend adjusted daily closes from Yahoo via yfinance, aligns
the security against the benchmark on common trading days, and caches the
result server-side for the rest of the calendar day.

The cache is the reason this app exists rather than a static page. Sixty
students refreshing the same two tickers becomes one Yahoo request per ticker
per day, which is the difference between working and being rate limited.

Yahoo is an unofficial, undocumented source. Every failure path here surfaces a
readable message so a student can fall back to entering volatility and
correlation by hand rather than staring at an empty page.
"""

from __future__ import annotations

import datetime as dt
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

# Calendar days of history to request. 400 comfortably covers a 252 day lookback
# plus holidays and any leading NaNs.
HISTORY_DAYS = 400

_lock = threading.Lock()
_memory: dict[str, dict] = {}


class PriceError(Exception):
    """Raised when prices cannot be retrieved. The message is shown to students."""


@dataclass(frozen=True)
class Series:
    """Aligned closes for one security against one benchmark."""

    ticker: str
    benchmark: str
    dates: list[str]              # ISO dates, oldest first
    security: list[float]
    bench: list[float]
    as_of: str                    # date the cache entry was built
    stale: bool = False           # served from an earlier day after a failure

    @property
    def last_price(self) -> float:
        return self.security[-1]

    @property
    def last_date(self) -> str:
        return self.dates[-1]


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def _key(ticker: str, benchmark: str) -> str:
    return f"{ticker.upper()}__{benchmark.upper()}"


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.json")


def _read_cache(key: str) -> dict | None:
    if key in _memory:
        return _memory[key]
    path = _cache_path(key)
    try:
        with open(path) as fh:
            payload = json.load(fh)
        _memory[key] = payload
        return payload
    except (OSError, ValueError):
        return None


def _write_cache(key: str, payload: dict) -> None:
    _memory[key] = payload
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_path(key)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)       # atomic, so concurrent workers cannot tear
    except OSError as exc:
        log.warning("could not write price cache: %s", exc)


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def _download(ticker: str, benchmark: str) -> dict:
    """One Yahoo request. Raises PriceError with a student-readable message."""
    try:
        import yfinance as yf
    except ImportError as exc:                       # pragma: no cover
        raise PriceError("The price library is not installed on the server.") from exc

    symbols = [ticker.upper(), benchmark.upper()]
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=HISTORY_DAYS)

    try:
        frame = yf.download(
            symbols,
            start=start.isoformat(),
            end=end.isoformat(),
            interval="1d",
            auto_adjust=True,        # split and dividend adjusted closes
            progress=False,
            threads=False,
            group_by="column",
        )
    except Exception as exc:
        raise PriceError(
            "Yahoo did not answer. It rate limits heavily; wait a minute and "
            "try again, or enter volatility and correlation by hand below."
        ) from exc

    if frame is None or frame.empty:
        raise PriceError(
            f"Yahoo returned no data for {ticker.upper()}. Check the ticker, "
            "or enter volatility and correlation by hand below."
        )

    try:
        closes = frame["Close"][symbols].dropna()
    except KeyError as exc:
        raise PriceError(
            f"Yahoo did not return closing prices for both {ticker.upper()} and "
            f"{benchmark.upper()}."
        ) from exc

    if len(closes) < 30:
        raise PriceError(
            f"Only {len(closes)} overlapping trading days came back for "
            f"{ticker.upper()} and {benchmark.upper()}. Not enough to estimate "
            "volatility. A recent listing will do this."
        )

    return {
        "ticker": symbols[0],
        "benchmark": symbols[1],
        "dates": [d.strftime("%Y-%m-%d") for d in closes.index],
        "security": [float(v) for v in closes[symbols[0]]],
        "bench": [float(v) for v in closes[symbols[1]]],
        "as_of": dt.date.today().isoformat(),
    }


def get_series(ticker: str, benchmark: str = "SPY") -> Series:
    """Aligned adjusted closes, cached for the calendar day.

    On a fetch failure with a cached entry from an earlier day, the stale entry
    is returned with stale=True rather than failing outright. A day-old
    volatility beats no sizer on a Thursday night.
    """
    ticker = ticker.strip().upper()
    benchmark = benchmark.strip().upper()
    if not ticker:
        raise PriceError("Enter a ticker.")

    key = _key(ticker, benchmark)
    today = dt.date.today().isoformat()

    cached = _read_cache(key)
    if cached and cached.get("as_of") == today:
        return Series(**cached, stale=False)

    with _lock:
        # another worker may have filled it while we waited
        cached = _read_cache(key)
        if cached and cached.get("as_of") == today:
            return Series(**cached, stale=False)

        try:
            payload = _download(ticker, benchmark)
        except PriceError:
            if cached:
                log.warning("serving stale prices for %s from %s", key, cached["as_of"])
                return Series(**cached, stale=True)
            raise
        _write_cache(key, payload)
        return Series(**payload, stale=False)


def window(series: Series, lookback: int) -> tuple[list[float], list[float]]:
    """The most recent `lookback` daily returns for the security and benchmark.

    Mirrors the workbook, which offsets `lookback` rows down the return columns
    from the most recent date.
    """
    if lookback < 2:
        raise PriceError("The lookback must be at least two trading days.")

    need = lookback + 1
    if len(series.dates) < need:
        raise PriceError(
            f"Only {len(series.dates)} trading days are available for "
            f"{series.ticker}, and a {lookback} day lookback needs {need}."
        )

    from sizer import simple_returns

    sec = simple_returns(series.security[-need:])
    ben = simple_returns(series.bench[-need:])
    return sec, ben
