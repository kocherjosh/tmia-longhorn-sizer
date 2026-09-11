"""
TMIA Longhorn Fund position sizer, web front end.

Stateless by design: nothing a student enters is stored, there is no database,
and no fund holdings or portfolio values live on this server. Every input comes
from the student and every response is computed fresh. The only server-side
state is a price cache keyed by ticker.

Access is a single shared password via HTTP Basic Auth. That is obfuscation,
not access control; assume the password circulates. It is adequate precisely
because nothing confidential is here.
"""

from __future__ import annotations

import hmac
import logging
import os
from functools import wraps

from flask import Flask, Response, render_template, request

import benchmark_weights
import prices
import sizer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)

BENCHMARK = os.environ.get("SIZER_BENCHMARK", "SPY")
USERNAME = os.environ.get("SIZER_USERNAME", "longhorn")
PASSWORD = os.environ.get("SIZER_PASSWORD", "")

# A round starting value so the empty form sizes something on the first click.
# It is a placeholder, not the fund's value. Replace it only with another round
# placeholder; the fund's actual value never belongs in this repo or on the server.
DEFAULT_FUND_VALUE = "1000000"

DEFAULTS = {
    "ticker": "",
    "lookback": "126",
    "portfolio_weight": "0",
    "benchmark_weight": "",            # blank means look it up
    "incremental_weight": "0",
    # `or`, not a get() default: Render can hand over an empty string.
    "fund_value": os.environ.get("SIZER_DEFAULT_FUND_VALUE") or DEFAULT_FUND_VALUE,
    "manual": "",
    "vol_security": "",
    "vol_benchmark": "",
    "corr": "",
}


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

def _authorised(auth) -> bool:
    if not PASSWORD:
        # No password configured. Refuse rather than silently serving open.
        return False
    if not auth or not auth.username or auth.password is None:
        return False
    return hmac.compare_digest(auth.username, USERNAME) and hmac.compare_digest(
        auth.password, PASSWORD
    )


def require_password(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not PASSWORD:
            return Response(
                "SIZER_PASSWORD is not set on this server, so the sizer is "
                "closed. Set it in the Render environment and redeploy.",
                503,
                {"Content-Type": "text/plain"},
            )
        if not _authorised(request.authorization):
            return Response(
                "Sign in with the class password.",
                401,
                {"WWW-Authenticate": 'Basic realm="TMIA Longhorn Sizer"'},
            )
        return view(*args, **kwargs)

    return wrapped


# --------------------------------------------------------------------------
# Input parsing
# --------------------------------------------------------------------------

class InputError(Exception):
    pass


def _number(raw: str, name: str, *, required: bool = True, default: float = 0.0) -> float:
    raw = (raw or "").strip().replace(",", "").replace("%", "").replace("$", "")
    if not raw:
        if required:
            raise InputError(f"Enter a value for {name}.")
        return default
    try:
        return float(raw)
    except ValueError:
        raise InputError(f"{name} must be a number. Got {raw!r}.") from None


def _percent(raw: str, name: str, *, required: bool = True) -> float:
    """Students type 0.33 meaning 0.33 percent. Return the decimal 0.0033."""
    return _number(raw, name, required=required) / 100.0


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/healthz")
def healthz():
    return {"ok": True}, 200


@app.route("/", methods=["GET"])
@require_password
def index():
    form = {k: request.args.get(k, v) for k, v in DEFAULTS.items()}
    manual = form["manual"] == "1"

    # Nothing submitted yet: show the empty form.
    if not request.args.get("ticker") and not manual:
        return render_template(
            "index.html", form=form, result=None, error=None,
            feed=None, bench=None, benchmark=BENCHMARK, manual=False,
        )

    error = None
    feed = None
    result = None
    bench = None

    try:
        lookback = int(_number(form["lookback"], "lookback", required=False, default=126))
        if lookback < 2:
            raise InputError("The lookback must be at least two trading days.")

        portfolio_weight = _percent(form["portfolio_weight"], "current portfolio weight")
        # Blank means look it up from the benchmark's holdings; a typed number wins.
        benchmark_weight = (
            _percent(form["benchmark_weight"], "benchmark weight")
            if form["benchmark_weight"].strip() else None
        )
        incremental_weight = _percent(form["incremental_weight"],
                                      "proposed incremental weight", required=False)
        fund_value = _number(form["fund_value"], "fund value")
        if fund_value <= 0:
            raise InputError("The fund value must be greater than zero.")
        if portfolio_weight < 0:
            raise InputError("A long only fund cannot hold a negative weight.")
        if portfolio_weight + incremental_weight < -1e-12:
            raise InputError(
                "That trim takes the position below zero. The largest reduction "
                "is a full close-out."
            )

        if manual:
            vol_s = _number(form["vol_security"], "security volatility") / 100.0
            vol_b = _number(form["vol_benchmark"], "benchmark volatility") / 100.0
            corr = _number(form["corr"], "correlation")
            price = _number(form.get("last_price") or request.args.get("last_price", ""),
                            "last price", required=False, default=0.0)
            if not -1.0 <= corr <= 1.0:
                raise InputError("The correlation must be between -1 and 1.")
            if benchmark_weight is None:
                raise InputError(
                    f"Enter the benchmark weight, or zero if the name is not in "
                    f"the {BENCHMARK}. It is only looked up automatically when "
                    "prices come from the feed."
                )
            result = sizer.size_from_risk(
                vol_security=vol_s, vol_benchmark=vol_b, corr=corr,
                beta_=corr * vol_s / vol_b if vol_b else 0.0,
                observations=lookback,
                current_portfolio_weight=portfolio_weight,
                benchmark_weight=benchmark_weight,
                incremental_weight=incremental_weight,
                portfolio_value=fund_value,
                last_price=price,
            )
        else:
            if benchmark_weight is None:
                bench = benchmark_weights.lookup(form["ticker"], BENCHMARK)
                benchmark_weight = bench.weight
            series = prices.get_series(form["ticker"], BENCHMARK)
            sec, ben = prices.window(series, lookback)
            feed = series
            result = sizer.size(
                security_returns=sec, benchmark_returns=ben,
                current_portfolio_weight=portfolio_weight,
                benchmark_weight=benchmark_weight,
                incremental_weight=incremental_weight,
                portfolio_value=fund_value,
                last_price=series.last_price,
            )
    except (InputError, prices.PriceError, benchmark_weights.BenchmarkError) as exc:
        error = str(exc)
    except Exception:                                    # pragma: no cover
        log.exception("unexpected failure sizing %s", form.get("ticker"))
        error = (
            "Something went wrong on the server. Try again, and if it persists "
            "tell the instructor what you entered."
        )

    return render_template(
        "index.html", form=form, result=result, error=error,
        feed=feed, bench=bench, benchmark=BENCHMARK, manual=manual,
    )


# --------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------

@app.template_filter("pct")
def _pct(x, places=2):
    return f"{x * 100:.{places}f}%"


@app.template_filter("signed_pct")
def _signed_pct(x, places=2):
    """Keep the sign even when the magnitude rounds to zero.

    A trim of 0.002% must not render as a bare 0.00% while the dollar and share
    rows below it show a sale. Students read the three rows together.
    """
    if x == 0:
        return f"{0:.{places}f}%"
    return f"({abs(x) * 100:.{places}f}%)" if x < 0 else f"+{x * 100:.{places}f}%"


@app.template_filter("bps")
def _bps(x, places=1):
    return f"{x:.{places}f}"


@app.template_filter("signed_bps")
def _signed_bps(x, places=1):
    return f"({abs(x):.{places}f})" if x < 0 else f"+{x:.{places}f}"


@app.template_filter("money")
def _money(x):
    return f"(${abs(x):,.0f})" if x < 0 else f"${x:,.0f}"


@app.template_filter("shares")
def _shares(x):
    return f"({abs(x):,.0f})" if x < 0 else f"{x:,.0f}"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
