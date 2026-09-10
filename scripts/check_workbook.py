#!/usr/bin/env python3
"""Prove that sizer.py and the Longhorn Sizer tab still agree.

    python scripts/check_workbook.py path/to/TMIA_Position_Sizing_Calculator_v6.xlsx

The workbook is the other half of this tool and it does not live in this repo.
A rule change in one that is not mirrored in the other means two tools give
students different answers, which is worse than either being wrong on its own.
Run this after any change to sizer.py, and after any edit to the workbook.

Three checks, in order:

1. Formula drift. Every rule cell's formula text is compared against the text
   this script was transcribed from. If the workbook has been edited, the
   transcription below is stale and step 3 would be meaningless, so this fails
   loudly and names the cell.
2. Risk inputs. The workbook carries its Bloomberg pull as pasted values, so
   volatility, correlation and beta are recomputed from that series and checked
   against the constants pinned in tests/test_sizer.py.
3. Excel's own answers. A workbook saved after a recalculation carries cached
   results, and those are compared straight against sizer.py for the inputs the
   workbook was saved with. This is the strongest check available, because it
   takes the transcription below out of the loop entirely. A workbook saved
   without cached values skips this step and says so.
4. Every output cell, swept over positions, trades and volatility regimes,
   against the transcription. This covers the cases the saved workbook does not
   sit on, close-outs and the mandate cap in particular.

Needs openpyxl, which is a development dependency and deliberately not in
requirements.txt; the deployed app never reads a workbook.
"""
from __future__ import annotations

import itertools
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sizer

SHEET = "Longhorn Sizer"

# The formula text this transcription was written from. If any of these no
# longer match, the workbook changed and the transcription must be revisited.
EXPECTED_FORMULAS = {
    "C12": "=STDEV(OFFSET($R$8,0,0,$C$8))*SQRT(252)",
    "C13": "=STDEV(OFFSET($Q$8,0,0,$C$8))*SQRT(252)",
    "C14": "=CORREL(OFFSET($Q$8,0,0,$C$8),OFFSET($R$8,0,0,$C$8))",
    "C15": "=SLOPE(OFFSET($R$8,0,0,$C$8),OFFSET($Q$8,0,0,$C$8))",
    "C16": "=SQRT(C12*C12+C13*C13-2*C14*C12*C13)",
    "C22": "=C20-C21",
    "C23": "=ABS(C22)*C16*10000",
    "C24": '=IF(C23=0,"None, no active position",IF(C23<=15,"Low",IF(C23<=30,"Medium",IF(C23<=60,"High","ABOVE HIGH"))))',
    "C25": "=IF(C23<=15,15-C23,IF(C23<=30,30-C23,IF(C23<=60,60-C23,0)))",
    "C33": "=IF($C$16<=0,0,IF($C$22<0,MAX(-MIN(300,C30/$C$16)/10000,-$C$21),MIN(300,C30/$C$16)/10000))",
    "D33": "=IF($C$16<=0,0,IF($C$22<0,MAX(-MIN(300,D30/$C$16)/10000,-$C$21),MIN(300,D30/$C$16)/10000))",
    "E33": "=IF($C$16<=0,0,IF($C$22<0,MAX(-MIN(300,E30/$C$16)/10000,-$C$21),MIN(300,E30/$C$16)/10000))",
    "C34": "=C33+$C$21",
    "C35": "=C33-$C$22",
    "C36": "=C35*$C$52",
    "C37": '=IF($C$53>0,ROUND(C36/$C$53,0),"")',
    "C42": "=C20+C41",
    "C43": "=C22+C41",
    "C44": "=ABS(C43)*C16*10000",
    "C45": "=C44-C23",
    "C46": '=IF(C41=0,"No trade",IF(C41<0,IF(ABS(C45)<=15,"Reduction, low magnitude",IF(ABS(C45)<=30,"Reduction, medium magnitude","Reduction, high magnitude")),IF(C44<=15,"Low",IF(C44<=30,"Medium",IF(C44<=60,"High","EXCEEDS HIGH")))))',
    "C47": '=IF(C41=0,"",IF(C41<0,IF(ABS(C45)<=15,4,IF(ABS(C45)<=30,8,12)),IF(C44<=15,4,IF(C44<=30,8,IF(C44<=60,12,"not permitted")))))',
    "C48": '=IF(ABS(C43)*10000<=300,"OK","OVER THE 3.00% CAP")',
    "C54": "=C41*C52",
    "C55": '=IF(C53>0,ROUND(C54/C53,0),"")',
    "C56": "=C42*C52",
    "C57": '=IF(C53>0,ROUND(C56/C53,0),"")',
}

# Tier ceilings, C30:E30. Literal values in the workbook, not formulas.
EXPECTED_CEILINGS = {"C30": 15, "D30": 30, "E30": 60}

# Constants pinned in tests/test_sizer.py, which claim to come from this workbook.
PINNED = {
    "vol_security": 0.4736197585891236,
    "vol_benchmark": 0.1400622700373325,
    "corr": 0.2622339341011068,
    "beta": 0.8867425362286215,
    "last_price": 35.66,
}


def xl_round(x: float) -> int:
    """Excel ROUND, half away from zero."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def workbook_cells(avol, portfolio_w, benchmark_w, incremental_w, fund, price,
                   ceilings=(15.0, 30.0, 60.0)):
    """The Longhorn Sizer tab evaluated in Python.

    Transcribed from the Excel text in EXPECTED_FORMULAS and deliberately not
    from sizer.py, so agreement between the two is evidence rather than a
    tautology. Cell references in the comments are the workbook's.
    """
    active = portfolio_w - benchmark_w                       # C22
    risk = abs(active) * avol * 10000                        # C23

    if risk == 0:    tier = "None, no active position"       # C24
    elif risk <= 15: tier = "Low"
    elif risk <= 30: tier = "Medium"
    elif risk <= 60: tier = "High"
    else:            tier = "ABOVE HIGH"

    room = (15 - risk if risk <= 15 else                     # C25
            30 - risk if risk <= 30 else
            60 - risk if risk <= 60 else 0)

    ladder = []                                              # C33:E37
    for ceiling in ceilings:
        if avol <= 0:
            weight = 0.0
        elif active < 0:
            weight = max(-min(300.0, ceiling / avol) / 10000, -benchmark_w)
        else:
            weight = min(300.0, ceiling / avol) / 10000
        trade_w = weight - active
        dollars = trade_w * fund
        ladder.append((weight, weight + benchmark_w, trade_w, dollars,
                       xl_round(dollars / price) if price > 0 else ""))

    new_portfolio = portfolio_w + incremental_w              # C42
    new_active = active + incremental_w                      # C43
    new_risk = abs(new_active) * avol * 10000                # C44
    delta = new_risk - risk                                  # C45

    if incremental_w == 0:                                   # C46, C47
        required, votes = "No trade", ""
    elif incremental_w < 0:
        magnitude = abs(delta)
        required = ("Reduction, low magnitude" if magnitude <= 15 else
                    "Reduction, medium magnitude" if magnitude <= 30 else
                    "Reduction, high magnitude")
        votes = 4 if magnitude <= 15 else (8 if magnitude <= 30 else 12)
    else:
        required = ("Low" if new_risk <= 15 else
                    "Medium" if new_risk <= 30 else
                    "High" if new_risk <= 60 else "EXCEEDS HIGH")
        votes = (4 if new_risk <= 15 else 8 if new_risk <= 30 else
                 12 if new_risk <= 60 else "not permitted")

    cap = "OK" if abs(new_active) * 10000 <= 300 else "OVER THE 3.00% CAP"   # C48

    trade_value = incremental_w * fund                       # C54
    target_value = new_portfolio * fund                      # C56
    return {
        "C22": active, "C23": risk, "C24": tier, "C25": room, "ladder": ladder,
        "C42": new_portfolio, "C43": new_active, "C44": new_risk, "C45": delta,
        "C46": required, "C47": votes, "C48": cap,
        "C54": trade_value, "C55": xl_round(trade_value / price) if price > 0 else "",
        "C56": target_value, "C57": xl_round(target_value / price) if price > 0 else "",
    }


def page_tier_label(result):
    """What the page prints for "Tier this position already uses".

    sizer.py carries an above-High position as current_tier None and the
    template renders the words, so compare the label, not the representation.
    """
    if result.current_tier:
        return result.current_tier
    return "ABOVE HIGH" if result.current_risk_bps > 0 else "None, no active position"


def compare_cached(ws) -> int:
    """Compare Excel's own results against sizer.py, for the saved inputs.

    No transcription involved: these are the numbers Excel computed. Returns the
    number of cells that disagree.
    """
    val = lambda ref: ws[ref].value
    result = sizer.size_from_risk(
        vol_security=val("C12"), vol_benchmark=val("C13"), corr=val("C14"),
        beta_=val("C15"), observations=val("C8"),
        current_portfolio_weight=val("C20"), benchmark_weight=val("C21"),
        incremental_weight=val("C41"), portfolio_value=val("C52"),
        last_price=val("C53"))
    low, medium, high = result.ladder

    pairs = [
        ("C16 active volatility", val("C16"), result.active_vol),
        ("C22 current active weight", val("C22"), result.current_active_weight),
        ("C23 active risk today", val("C23"), result.current_risk_bps),
        ("C24 tier in use", val("C24"), page_tier_label(result)),
        ("C25 room in tier", val("C25"), result.room_bps),
        ("C33 Low ceiling weight", val("C33"), low.ceiling_active_weight),
        ("D33 Medium ceiling weight", val("D33"), medium.ceiling_active_weight),
        ("E33 High ceiling weight", val("E33"), high.ceiling_active_weight),
        ("C34 Low portfolio weight", val("C34"), low.ceiling_portfolio_weight),
        ("D34 Medium portfolio weight", val("D34"), medium.ceiling_portfolio_weight),
        ("E34 High portfolio weight", val("E34"), high.ceiling_portfolio_weight),
        ("C35 Low add or trim", val("C35"), low.trade_weight),
        ("D35 Medium add or trim", val("D35"), medium.trade_weight),
        ("E35 High add or trim", val("E35"), high.trade_weight),
        ("C36 Low dollars", val("C36"), low.trade_value),
        ("D36 Medium dollars", val("D36"), medium.trade_value),
        ("E36 High dollars", val("E36"), high.trade_value),
        ("C37 Low shares", val("C37"), low.trade_shares),
        ("D37 Medium shares", val("D37"), medium.trade_shares),
        ("E37 High shares", val("E37"), high.trade_shares),
        ("C42 new portfolio weight", val("C42"), result.new_portfolio_weight),
        ("C43 new active weight", val("C43"), result.new_active_weight),
        ("C44 risk after trade", val("C44"), result.new_risk_bps),
        ("C45 risk added", val("C45"), result.delta_risk_bps),
        ("C46 required tier", val("C46"), result.required_tier),
        ("C47 votes required", val("C47"), result.required_votes),
        ("C48 cap test", val("C48"), "OK" if result.within_cap else "OVER THE 3.00% CAP"),
        ("C54 trade value", val("C54"), result.trade_value),
        ("C55 shares to trade", val("C55"), result.trade_shares),
        ("C56 target value", val("C56"), result.target_value),
        ("C57 target shares", val("C57"), result.target_shares),
    ]

    print(f"   saved case: portfolio {val('C20'):.4%}, benchmark {val('C21'):.4%}, "
          f"proposing {val('C41'):+.4%}, fund ${val('C52'):,.0f}, last ${val('C53'):,.2f}")
    bad = 0
    for name, excel, python in pairs:
        if isinstance(excel, float) and isinstance(python, float):
            same = math.isclose(excel, python, rel_tol=1e-12, abs_tol=1e-12)
        else:
            same = excel == python
        if not same:
            bad += 1
            print(f"   FAIL {name}: Excel {excel!r}, sizer.py {python!r}")
    print(f"   {len(pairs) - bad} of {len(pairs)} cells match Excel's own results exactly")
    return bad


def check_formulas(ws) -> list[str]:
    problems = []
    for ref, expected in EXPECTED_FORMULAS.items():
        actual = ws[ref].value
        actual = actual.text if hasattr(actual, "text") else actual
        squashed = actual.replace(" ", "") if isinstance(actual, str) else actual
        if squashed != expected.replace(" ", ""):
            problems.append(f"{ref}\n        workbook: {actual!r}\n        expected: {expected!r}")
    for ref, expected in EXPECTED_CEILINGS.items():
        if ws[ref].value != expected:
            problems.append(f"{ref} tier ceiling is {ws[ref].value!r}, expected {expected!r}")
    return problems


def read_series(ws):
    """The pasted Bloomberg block. Row 8 is the newest observation."""
    security, benchmark, dates = [], [], []
    row = 8
    while True:
        sec, ben = ws[f"I{row}"].value, ws[f"L{row}"].value
        if not isinstance(sec, (int, float)) or not isinstance(ben, (int, float)):
            break
        security.append(float(sec))
        benchmark.append(float(ben))
        dates.append((ws[f"H{row}"].value, ws[f"K{row}"].value))
        row += 1
    return security, benchmark, dates


def build_cases(regimes):
    cases = []
    grid = [0.0, 0.0005, 0.0033, 0.005, 0.0075, 0.01, 0.02, 0.03, 0.05]
    bench = [0.0, 0.0005, 0.005, 0.0075, 0.02]
    incr = [0.0, 0.0001, 0.003, -0.0005, -0.0033, 0.02, -0.02, 0.05]
    for name, risk in regimes:
        for a, b, c in itertools.product(grid, bench, incr):
            if a + c >= -1e-12:                 # a long only fund cannot go past a close-out
                cases.append((a, b, c, risk, name))
    random.seed(0)
    for _ in range(4000):
        name, risk = random.choice(regimes)
        b = random.choice([0.0, random.uniform(0, 0.03)])
        a = random.uniform(0, 0.06)
        cases.append((a, b, random.uniform(-a, 0.04), risk, name))
    return cases


def main(path: str) -> int:
    try:
        import openpyxl
    except ImportError:
        print("openpyxl is needed for this check: pip install openpyxl")
        return 2

    wb = openpyxl.load_workbook(path, data_only=False)
    if SHEET not in wb.sheetnames:
        print(f"no {SHEET!r} tab in {path}")
        return 2
    ws = wb[SHEET]
    failures = 0

    print("1. formula drift")
    problems = check_formulas(ws)
    if problems:
        print(f"   FAIL. {len(problems)} rule cell(s) differ from what this script was written from.")
        print("   The transcription in this file is stale, so nothing after this can be trusted.")
        for problem in problems:
            print(f"      {problem}")
        return 1
    print(f"   ok, {len(EXPECTED_FORMULAS)} formulas and {len(EXPECTED_CEILINGS)} ceilings unchanged")

    print("2. risk inputs recomputed from the workbook's own price series")
    security, benchmark, dates = read_series(ws)
    misaligned = [d for d in dates[1:] if d[0] != d[1]]
    if misaligned:
        print(f"   FAIL. {len(misaligned)} rows where the security and benchmark dates differ.")
        return 1
    lookback = ws["C8"].value or 126
    # The workbook stores newest first and takes the newest `lookback` returns.
    sec_returns = [security[i] / security[i + 1] - 1 for i in range(len(security) - 1)][:lookback]
    ben_returns = [benchmark[i] / benchmark[i + 1] - 1 for i in range(len(benchmark) - 1)][:lookback]
    got = {
        "vol_security": sizer.annualised_vol(sec_returns),
        "vol_benchmark": sizer.annualised_vol(ben_returns),
        "corr": sizer.correlation(ben_returns, sec_returns),
        "beta": sizer.beta(sec_returns, ben_returns),
        "last_price": security[0],
    }
    print(f"   {len(security)} aligned rows, {len(sec_returns)} returns, lookback {lookback}")
    for key, expected in PINNED.items():
        delta = got[key] - expected
        ok = abs(delta) < 1e-12
        failures += 0 if ok else 1
        print(f"   {'ok' if ok else 'FAIL':<4} {key:<14} workbook {got[key]:.16f}   "
              f"pinned {expected:.16f}   delta {delta:+.2e}")

    print("3. Excel's own cached answers")
    cached = openpyxl.load_workbook(path, data_only=True)[SHEET]
    if cached["C16"].value is None:
        print("   skipped. This workbook carries no cached results, so it has not been")
        print("   recalculated since it was last written. Open it, recalculate and save")
        print("   to enable this check.")
    else:
        failures += compare_cached(cached)

    print("4. every output cell, swept against the transcription")
    regimes = [
        ("workbook", (got["vol_security"], got["vol_benchmark"], got["corr"], got["beta"])),
        ("low vol", (0.030, 0.0, 0.0, 0.2)),        # the 300 bps mandate cap binds on High
        ("very low vol", (0.015, 0.0, 0.0, 0.1)),   # it binds on every rung
        ("high vol", (0.950, 0.140, 0.35, 2.1)),
    ]
    fund = float(ws["C52"].value or 14_000_000)
    price = float(got["last_price"])
    cases = build_cases(regimes)
    mismatches = []

    for portfolio_w, benchmark_w, incremental_w, risk, name in cases:
        vol_s, vol_b, corr, beta_ = risk
        avol = sizer.active_volatility(vol_s, vol_b, corr)
        book = workbook_cells(avol, portfolio_w, benchmark_w, incremental_w, fund, price)
        result = sizer.size_from_risk(
            vol_security=vol_s, vol_benchmark=vol_b, corr=corr, beta_=beta_,
            observations=lookback, current_portfolio_weight=portfolio_w,
            benchmark_weight=benchmark_w, incremental_weight=incremental_w,
            portfolio_value=fund, last_price=price)

        def eq(label, left, right, tol=1e-9):
            same = (math.isclose(left, right, abs_tol=tol, rel_tol=1e-12)
                    if isinstance(left, float) and isinstance(right, float)
                    else left == right)
            if not same:
                mismatches.append((name, label, (portfolio_w, benchmark_w, incremental_w),
                                   left, right))

        eq("C22 active weight", book["C22"], result.current_active_weight)
        eq("C23 risk today", book["C23"], result.current_risk_bps)
        eq("C24 tier", book["C24"], page_tier_label(result))
        eq("C25 room", book["C25"], result.room_bps)
        for col, rung_book, rung in zip("CDE", book["ladder"], result.ladder):
            weight, portfolio, trade_w, dollars, shares = rung_book
            eq(f"{col}33 ceiling weight", weight, rung.ceiling_active_weight)
            eq(f"{col}34 portfolio weight", portfolio, rung.ceiling_portfolio_weight)
            eq(f"{col}35 add or trim", trade_w, rung.trade_weight)
            eq(f"{col}36 dollars", dollars, rung.trade_value, tol=1e-6)
            eq(f"{col}37 shares", shares, rung.trade_shares)
        eq("C42 new portfolio weight", book["C42"], result.new_portfolio_weight)
        eq("C43 new active weight", book["C43"], result.new_active_weight)
        eq("C44 risk after", book["C44"], result.new_risk_bps)
        eq("C45 delta risk", book["C45"], result.delta_risk_bps)

        if book["C46"] == "No trade":
            expected_tier = (None, False)
        elif book["C46"].startswith("Reduction"):
            expected_tier = ({"Reduction, low magnitude": "Low",
                              "Reduction, medium magnitude": "Medium",
                              "Reduction, high magnitude": "High"}[book["C46"]], True)
        elif book["C46"] == "EXCEEDS HIGH":
            expected_tier = (None, False)
        else:
            expected_tier = (book["C46"], False)
        eq("C46 required tier", expected_tier, (result.required_tier, result.is_reduction))
        eq("C47 votes", None if book["C47"] in ("", "not permitted") else book["C47"],
           result.required_votes)
        eq("C48 cap test", book["C48"] == "OK", result.within_cap)
        eq("C54 trade dollars", book["C54"], result.trade_value, tol=1e-6)
        eq("C55 trade shares", book["C55"], result.trade_shares)
        eq("C56 target dollars", book["C56"], result.target_value, tol=1e-6)
        eq("C57 target shares", book["C57"], result.target_shares)

    for name, _ in regimes:
        counted = sum(1 for c in cases if c[4] == name)
        failed = sum(1 for m in mismatches if m[0] == name)
        print(f"   {name:<14} {counted:>6,} cases   {failed:>4} disagreements")

    if mismatches:
        failures += len(mismatches)
        seen = set()
        for name, label, inputs, left, right in mismatches:
            if (name, label) in seen:
                continue
            seen.add((name, label))
            print(f"   FAIL {name}, {label}: portfolio {inputs[0]:.6f}, benchmark "
                  f"{inputs[1]:.6f}, incremental {inputs[2]:+.6f}")
            print(f"        workbook {left!r}   sizer.py {right!r}")

    print()
    if failures:
        print(f"DISAGREE. {failures} problem(s). The app and the workbook do not match.")
        return 1
    print(f"AGREE. {len(cases):,} cases, 26 cells each, {len(cases) * 26:,} comparisons.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
