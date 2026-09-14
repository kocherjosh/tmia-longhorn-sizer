# CLAUDE.md

Project memory for this repo. Read `README.md` for what the app does, how to
deploy it, and the model. This file covers what an agent working here needs that
the README does not: what must not change, what is already verified, and what is
waiting on a human decision.

## State of the repo

**The app is complete and working.** It is not a spec to implement. Several commits
on `main`, 99 passing tests, verified against the source workbook. Do not
rebuild it. If asked to "build the sizer," the honest answer is that it is built
and the work is deployment, verification, or a specific change.

```bash
pip install -r requirements.txt
python -m pytest tests/ -q                 # 99 tests, all should pass, ~2s
SIZER_PASSWORD=demo python app.py          # http://127.0.0.1:5000
python scripts/check_feed.py NVDA          # live Yahoo check
```

## What this is for

Josh Kocher teaches the TMIA MBA fund program at UT Austin McCombs. Students
with no investment background propose single-name equity trades, and the size
they may request is governed by conviction tiers measured in basis points of
standalone active risk. Students could not work out from the source spreadsheet
how much to ask for. This app answers that one question.

Every design decision serves comprehension by a novice under time pressure the
night before a proposal deadline. That is the thing being optimised, not
elegance, feature count, or performance.

## Hard invariants

Do not change these without Josh saying so explicitly. Each exists for a reason
that is not obvious from the code.

**Nothing is persisted.** No database, no session store, no logging of student
input. The app does not know the fund's holdings, weights, or value; the student
types them. This is what makes a single shared class password defensible, since
nothing confidential sits behind it. Adding persistence or preloading fund data
breaks that argument and requires the access model to be revisited *first*. If a
feature seems to need a database, say so and stop rather than adding one.

**The fund's actual value never goes on the server or in the repo.** On 10
September 2026 Josh set a round $1,000,000 starting value in `app.py`
(`DEFAULT_FUND_VALUE`) and zero starting weights, so the empty form sizes
something on the first click. That figure is a placeholder, not the fund's
value, confirmed as such by Josh, and the form tells students to replace it. Keep `SIZER_DEFAULT_FUND_VALUE`
unset in `render.yaml`, and never swap the placeholder for the real value: that
would put client portfolio value on a third-party host and in a public repo.

**Benchmark holdings are public data, and the only other thing fetched.**
`benchmark_weights.py` pulls State Street's daily SPY holdings file to fill in a
name's benchmark weight. It is the same category as the price cache: public,
cached for the day, and says nothing about the fund. It is not a step toward
storing the fund's own holdings, which remain off limits.

A filled in benchmark weight is tagged with its ticker in the hidden
`benchmark_for` field, and `_resolve_benchmark()` in `app.py` looks a name up
afresh when the tag no longer matches the ticker. Keep that guard. Without it, a
student who changes the ticker sizes the new name at the old name's benchmark
weight, which for NVDA to anything else is an 8% error nobody would notice.

**The app refuses to run without a password.** No `SIZER_PASSWORD` returns 503.
Do not add a development default that could ship.

**`sizer.py` imports nothing but `math` and `dataclasses`.** No Flask, no
yfinance, no pandas. It is the auditable statement of the rules and is meant to
be reusable in a notebook, a grader, or a future Endowment sizer. Keep the
web and data layers out of it.

**Tests are pinned to the workbook, not to the code.** Every expected value in
`tests/test_sizer.py` was read out of a recalculated
`TMIA_Position_Sizing_Calculator_v8.xlsx`, not computed by this repo. If a test
fails, the app and the workbook disagree and the presumption is that the app is
wrong. **Never relax an assertion or widen a tolerance to make a change pass.**
If a rule change legitimately moves an expected value, update it in the same
commit and say so in the message, so the diff is the record.

**The conviction tier is derived from the requested size, never selected by the
student.** The predecessor spreadsheet had a Low/Medium/High dropdown; it taught
the rule backwards. PMC-9.3 states the YES threshold follows the requested size,
not the proposer's eligibility. Do not reintroduce a tier selector.

The cohort is a different thing and the student does pick it, because vote
counts differ by programme and neither one can be inferred from the trade.

**The unit is "standalone active risk."** The ladder's ceiling row carries the
full phrase because it matches the Investment Proposal form field the student
copies it into. Other rows use the short form "active risk" consistently, and the
page defines the unit once at the top. Do not drift back to mixed labels; one
quantity with three names on one screen was a real defect here.

**No API keys in client-side code, ever.** Sixty students can read View Source.

**Do not invent numbers.** If a figure is missing, leave a marked gap and say so.
This is a fiduciary-adjacent teaching tool; a plausible wrong number is worse
than a visible hole.

## The workbook is the other half, and it is not in this repo

`TMIA_Position_Sizing_Calculator_v8.xlsx` implements the same rules for use at a
Bloomberg terminal. **The two must stay in sync.** A rule change in `sizer.py`
that is not mirrored in the workbook means two tools give students different
answers, which is worse than either tool being wrong consistently.

**Verified in sync against v8 on 13 September 2026.** `scripts/check_workbook.py`
compares the two directly and found no disagreement:

* 30 rule formulas and 3 tier ceilings unchanged from what the script was
  transcribed from.
* The four risk constants pinned in `tests/test_sizer.py` reproduced from the
  workbook's own pasted price series to machine precision.
* All 31 output cells match Excel's own recalculated results exactly, for the
  case the workbook is saved on. This is the check that takes the script's
  transcription out of the loop, and v7 is the first version that carries the
  cached values needed for it.
* 273,520 further comparisons across 10,520 positions and four volatility
  regimes, run for both cohorts, covering close-outs and the mandate cap.

v8 was written with openpyxl, which does not evaluate, so it arrives with no
cached results at all and the check above skips its third step and says so.
Open v8 in Excel, pick a cohort in `C10`, let it recalculate, and save, and that
step comes back.

Run it after any change to either side:

```bash
pip install -r requirements.txt           # includes openpyxl, which the app now uses too
python scripts/check_workbook.py path/to/TMIA_Position_Sizing_Calculator_v8.xlsx
```

It checks formula text before it checks numbers, so an edit to the workbook makes
it fail loudly rather than quietly comparing against a stale transcription.
Confirmed by mutating a copy: a changed `C48` and a moved tier ceiling both fail
it with exit code 1.

Note that the workbook's formula cells carry no cached values, so openpyxl reads
`None` for every one of them. That is why this check recomputes from the pasted
Bloomberg block in `H:R` rather than reading results, and why it needs no
recalculation step and no LibreOffice.

If you change a rule, say plainly that the workbook also needs the change, and
name the cells. The relevant ones on the `Longhorn Sizer` tab:

| Cell | Holds |
| --- | --- |
| `C10` | cohort, Graduate or Undergraduate (input, blank until chosen) |
| `C16` | active volatility |
| `C20`, `C21` | current portfolio weight, benchmark weight (inputs) |
| `C23` | active risk today, bps |
| `C24`, `C25` | current tier, room left in tier |
| `C30:E30` | tier ceilings, 15 / 30 / 60 |
| `C32:E32` | YES votes, formulas that follow `C10` |
| `C33:E37` | the ladder: ceiling weights, add/trim, dollars, shares |
| `C41` | proposed incremental weight (input) |
| `C44`&ndash;`C48` | risk after trade, delta, required tier, votes, cap test |
| `H:R` | Bloomberg data block. Do not disturb. |

Editing it needs `openpyxl`. Recalculating formulas for QA would need
LibreOffice (`soffice --headless --convert-to xlsx`) because openpyxl does not
evaluate, but `scripts/check_workbook.py` avoids that entirely by evaluating the
transcribed rules itself.

## Correct behaviour that looks like a bug

Check here before "fixing" any of these.

**Two tiers showing the same ceiling.** On the underweight side the fund is long
only, so the largest possible underweight is a full close-out. A 0.50% benchmark
name floors Medium and High at the same active weight of &minus;0.50%. The page
says so in a footnote. `Rung.capped_by == "close-out"` marks it.

**`(0.00%)` sitting above `($280)`.** A trim of 0.002% rounds to zero at two
decimals but is a real sale of 8 shares. `signed_pct` deliberately keeps the sign
when the magnitude rounds to zero, because students read the three rows together.
Do not "clean this up" to a bare `0.00%`.

**A 15.09 bps position reading as Medium.** Tier boundaries are inclusive at the
ceiling: 15.00 is Low, 15.01 is Medium. Correct and intended.

**Volatility differing from the workbook in the second decimal.** Yahoo's
adjusted close is not the workbook's Bloomberg field. Expected. It only matters
within a basis point of a ceiling.

**`excel_round` instead of Python's `round`.** Python rounds half to even; Excel
rounds half away from zero. Share counts must match the workbook. Do not replace
it with `round()`.

## The live feeds: verified locally on 9 September 2026, and from Render on 11 September

The feed logic was written against a stub and the live request had never run.
It has now, from Windows, against yfinance 1.7.0 on Python 3.14:

* `python scripts/check_feed.py NVDA` returned closes through 2026-09-09.
* `frame["Close"][symbols]` is still the correct access for that yfinance
  version. The frame comes back with MultiIndex columns of (field, symbol).
* The cache writes to `CACHE_DIR`, and a second call the same day is served from
  disk with the network sabotaged, so the per-day cache genuinely holds.
* The stale fallback banner, the manual path at `/?manual=1`, and the 503 with
  no `SIZER_PASSWORD` all behave as documented.

Two live failures surfaced on the way, both fixed in `prices.py::_download` and
now covered by `tests/test_prices.py`:

* A ticker Yahoo does not recognise comes back as a column of blanks rather than
  an error, so a typo used to read as "0 overlapping trading days", which tells a
  student nothing. It now names the spelling.
* The benchmark typed as the ticker returned one column instead of two, and the
  pair selection then yielded column labels rather than prices, raising an
  unhandled `ValueError` that reached the student as "something went wrong on the
  server." It is now refused with a sentence explaining why.

**Verified from Render on 11 September 2026.** Josh ran
`python scripts/check_feed.py NVDA` in the Render shell after the deploy that
added the benchmark weight lookup, and it reached both Yahoo and State Street.
Every part of the project is now verified end to end. If the deployed app ever
reports a feed failure that does not reproduce locally, run that command in the
Render shell again: a difference between the two points at Render's egress, not
the code.

Three failure defences exist and should be preserved: the per-day cache, the
stale-cache fallback with a visible banner, and the manual risk-entry path at
`/?manual=1`. The manual path is the one that keeps the tool usable when Yahoo is
down entirely, which it will be at some point on a Thursday night.

The version pins in `requirements.txt` cover Flask, gunicorn, and yfinance only.
Everything underneath, pandas and curl_cffi in particular, floats, so a Render
redeploy months from now can resolve a different stack than the one verified
here. If a deploy that used to work suddenly does not, suspect that first.

## Rulings, settled 10 September 2026

Both questions that used to sit here are settled, by Josh, as the workbook
already had them. No number moved and the workbook needs no change.

**Adds are tested on where the position lands, not on the increment.** This
holds even for a buy that lowers active risk, such as buying into a large
benchmark name the fund is underweight: NVDA not held at an 8.22% SPY weight is
269 bps, and a +0.30% buy that cuts that to 260 bps is still "not permitted".
The page explains it when it happens. Do not "fix" `required_tier()`.

**The single-stock cap is 300 bps of active weight, applied to both sides.** Not
holding any name above 3% of SPY is outside it. `MAX_ACTIVE_WEIGHT_BPS` stays
flat. The canon's account table reads "±3% active weight, look-through", which
matches; its constraint stack says "±3% of benchmark weight", which is the
phrasing that made this look open.

**Vote thresholds differ by cohort, 13 September 2026.** Graduate 6, 10, 13 and
undergraduate 8, 12, 16, for reductions as well as adds. `COHORTS` in `sizer.py`,
`C10` and `C32:E32` in the workbook. These supersede the 4, 8, 12 in PMC-9.3 and
PMC-9.4 for both cohorts, so the canon and the Curriculum Spine now disagree with
both tools until Josh updates them. The canon already flagged the gap: Appendix
B.4 records that the undergraduate trade cycle is unspecified, and that a 12-YES
threshold means something different at twenty students than at sixty. There is no
default cohort anywhere: the app refuses to size, the workbook shows "pick a
cohort", and `sizer.py` raises.

More generally: the project's standing instruction is to surface conflicts,
fairness risks, and execution risks rather than smoothing them over. If the
curriculum documents are ambiguous, say so and leave a marked gap.

## The Endowment CTEV data was lost in v7. v6 is the only copy.

On the `Endowment CTEV` tab, v6 holds sixteen ETF tickers in `I7:I27` and 4,017
pasted price cells in `AL:BC`. In v7 the Bloomberg `BDH` formulas were restored
over that block and evaluated without a terminal, so fifteen of the sixteen
tickers are gone, `IVV` is the only one left, and the price block is 3,780 cells
reading `#N/A Mandatory parameter [SECURITY] cannot be empty`.

**Do not delete or overwrite v6.** It is now the only copy of the covariance
inputs the future Endowment sleeve sizer needs, and those inputs cannot be
rebuilt without a Bloomberg terminal.

The `Longhorn Sizer` tab is unaffected: zero error cells, and the app matches it
exactly. The `Endowment Sizer` tab is byte for byte identical between the two.

One other v7 change, harmless today: a new column `S` on the Longhorn tab holding
`=Q-R`, labelled "Relative". Nothing references it. Note that it is the benchmark
return minus the security return, which is the negation of the active return the
rest of the sheet is built on. Its standard deviation is the same either way, so
wiring it in would not change a number, but the sign convention is worth fixing
before anyone builds on it.

## Extending to the Endowment Fund

Deliberately out of scope for v1. The Endowment sleeve sizer is a different
problem: sixteen ETFs, a full covariance matrix, and tracking-error change rather
than standalone risk, with vote bands instead of pathway boxes. It needs the
`Endowment CTEV` tab's covariance data, which is not in this repo. Do not
approximate it with the single-name model; the two are not the same calculation.

## Conventions

Design follows the UT Austin standards Josh uses across TMIA materials: Georgia
throughout, burnt orange `#BF5700`, chrome navy `#333F48`, section headers on
light gray `#E8E8E8`, yellow-tinted inputs echoing the workbook's
"you type this" convention. Tokens are at the top of `static/style.css`.

In prose you write anywhere in this repo, including commit messages, comments,
and docs: no em dashes and no hyphens used as punctuation. Use commas and
semicolons. That is Josh's standing preference and it applies to generated text.

## Definition of done for any change

1. `python -m pytest tests/ -q` passes with no assertion loosened.
2. If display changed, look at it. Render the page and check it, do not assume.
3. If behaviour changed, update `README.md` in the same commit.
4. If a rule changed, state in the commit message that the workbook needs the
   same change, and name the cells.
5. If the change touches persistence, auth, or what data lives on the server,
   stop and ask rather than proceeding.
