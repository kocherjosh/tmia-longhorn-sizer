# TMIA Longhorn Fund Position Sizer

A web version of the Longhorn Sizer tab of `TMIA_Position_Sizing_Calculator_v7.xlsx`,
so students can size a proposal from any machine without Bloomberg.

It answers one question: **how much do I ask for.** It shows what the current
position already costs in standalone active risk, then the add or trim needed to
sit at each conviction ceiling, in percent of fund, dollars, and shares.

The unit throughout is **standalone active risk**, the same words as the
"Proposed size" field on the Investment Proposal form, so the number reads
straight across. *Active* distinguishes it from total volatility (PMC-3.4);
*standalone* distinguishes it from contribution to portfolio tracking error
(PMC-8.2). Both adjectives are load bearing.

It sizes a proposal. It does not authorise one. Students recommend; faculty decide.

---

## What is and is not on this server

**Not here:** fund holdings, position sizes, portfolio value, client names,
performance, or anything a student types. There is no database. Every input comes
from the student on each request and nothing is written down.

**Here:** a cache of public closing prices, keyed by ticker.

That posture is deliberate. Access is a single shared class password, which is
obfuscation rather than access control; assume it circulates within a week. It is
adequate *because* nothing confidential is behind it. If you ever preload
holdings or fund value, this stops being true and the access model has to change
first.

---

## Deploying: GitHub to Render

**1. Push this directory to a new GitHub repository.**

```bash
git init
git add .
git commit -m "TMIA Longhorn position sizer"
git remote add origin git@github.com:<you>/tmia-position-sizer.git
git push -u origin main
```

**2. Create the service on Render.**

New → Blueprint → point it at the repo. `render.yaml` configures everything
except the password.

Doing it by hand instead: New → Web Service, runtime Python, build
`pip install -r requirements.txt`, start
`gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60`.

**3. Set the password in the Render dashboard, not in git.**

Environment → Add Environment Variable:

| Key | Value | Notes |
| --- | --- | --- |
| `SIZER_PASSWORD` | the class password | Required. Without it the app returns 503 rather than serving open. |
| `SIZER_USERNAME` | `longhorn` | Optional, defaults to `longhorn`. |
| `SIZER_BENCHMARK` | `SPY` | Optional. |
| `SIZER_DEFAULT_FUND_VALUE` | leave unset | Setting it puts fund value on the server. See the posture note above. |

**4. Stay on the Starter plan, not Free.** A free instance spins down after 15
minutes of no traffic and takes about a minute to wake. A student opening it on a
Thursday night will think it is broken. Starter is currently $7 a month and
removes that.

**5. Check the feed from the deployed instance.** Render shell:

```bash
python scripts/check_feed.py NVDA
```

If that fails on Render but works locally, the problem is egress, not the code.

---

## Running locally

```bash
pip install -r requirements.txt
SIZER_PASSWORD=demo python app.py       # http://127.0.0.1:5000
python -m pytest tests/ -q              # 51 tests
python scripts/check_feed.py NVDA       # live Yahoo check
python scripts/check_workbook.py W.xlsx # prove the app and the workbook agree
```

---

## How the pieces fit

| File | Does |
| --- | --- |
| `sizer.py` | The model. Pure functions, no network or disk. This is the file to read to check the math. |
| `prices.py` | Yahoo fetch, alignment against the benchmark, and the daily cache. |
| `app.py` | Routes, auth, input parsing, display formatting. |
| `templates/index.html` | The single page. |
| `tests/test_sizer.py` | Asserts the model reproduces the workbook exactly. |
| `tests/test_app.py` | Auth, validation, feed failure paths, and the no-persistence guarantee. |
| `tests/test_prices.py` | The cache, the stale fallback, and the message a student sees when Yahoo fails. |
| `scripts/check_workbook.py` | Compares every rule cell of the workbook against `sizer.py`. Needs openpyxl. |

`sizer.py` has no dependency on Flask or yfinance, so the math can be reused in a
notebook, a grader, or a future Endowment sizer without dragging the web app along.

---

## The price feed

Yahoo, via `yfinance`, split and dividend adjusted daily closes, aligned to
common trading days with the benchmark.

**The cache is the reason this is a server and not a static page.** Sixty
students refreshing the same two tickers becomes one Yahoo request per ticker per
day. Yahoo rate limits aggressively and a static page cannot cache across users.
Browser JavaScript also cannot call Yahoo at all, because Yahoo sends no CORS
headers; that is why a Canvas-hosted HTML file was not an option.

**Yahoo is unofficial and undocumented, and it breaks.** Three defences:

1. Cached responses are served for the whole calendar day.
2. On a fetch failure with an older cached entry, that entry is served and the
   page says so in an orange banner. A day-old volatility beats no sizer.
3. Failing both, the page shows a readable message and a link to *Feed down?
   Enter risk by hand*, where the student supplies volatility and correlation
   directly. Everything downstream still works.

The message names the actual problem where it can. A ticker Yahoo does not know
comes back as a column of blanks rather than an error, so it is reported as a
spelling problem rather than as thin history; an outage on the benchmark leg is
named as the benchmark. Typing the benchmark itself as the ticker is refused,
since a position in the benchmark carries no active risk against itself.

Yahoo's adjusted close differs slightly from whatever Bloomberg field the
workbook pulls, so expect small differences in volatility against the workbook.
The tier boundaries are wide enough that this almost never changes an answer, but
it can matter within a basis point of a ceiling.

---

## The model

Active volatility is the only conversion:

```
active_vol  = SQRT(vol_sec² + vol_bmk² − 2 × corr × vol_sec × vol_bmk)
active_risk = |active_weight| × active_vol × 10,000    (bps, standalone)
```

Conviction tiers, from `TMIA_Analytical_Canon` PMC-9.3 and `TMIA_Curriculum_Spine_v14`:

| Tier | Boxes | Standalone active risk ceiling | YES votes |
| --- | --- | --- | --- |
| Low | 1 | 15 bps | 4 |
| Medium | 1 + 2 | 30 bps | 8 |
| High | 1 + 2 + 3 | 60 bps | 12 |

The tier is **derived from the requested size**, not selected by the student.
PMC-9.3 is explicit that the YES threshold follows the requested size rather than
the tier the proposer is eligible for, so the v5 dropdown taught the rule
backwards and is gone.

Reductions branch separately: they scale on the risk removed, need no pathway
boxes, and use the same 15 / 30 / 60 scale (PMC-9.4).

The ladder runs in the direction of the current active weight, so a benchmark
name held at zero reads as an underweight. Ceilings are capped by the 300 bps
single-stock active weight limit and, on the underweight side, by a full
close-out; where a cap binds, two tiers can show the same number and the page
says so.

### Open conflict, unresolved

**Are adds tested on the resulting position or on the increment?**

This app preserves v5, v6 and v7 workbook behaviour and tests adds on **where the
position lands**. So a 5 bps add to a 28 bps position requires High and 12 YES.

PMC-9.4 is explicitly incremental for reductions ("up to 15 bps standalone risk
*removed*") while PMC-9.3 says only "size cap" for adds, and the IP form field
reads "proposed size", which points the other way. Under an incremental reading
that same trade is Low and 4 YES.

The two readings give different vote thresholds for the same trade. This needs a
ruling before the first live vote cycle. Changing it is a one-line edit in
`required_tier()` in `sizer.py`, and `tests/test_sizer.py` will tell you what else
moves.

**Secondary.** The canon's constraint stack gate 2 reads "±3% of benchmark weight,
look-through". The workbook and this app implement a flat 300 bps absolute active
weight cap. Carried forward from v5; confirm which is intended.

---

## Changing the rules

Tier ceilings, box labels, vote counts, and the mandate cap are constants at the
top of `sizer.py`:

```python
TIERS = (
    ("Low",    15.0, "Box 1",           4),
    ("Medium", 30.0, "Boxes 1 + 2",     8),
    ("High",   60.0, "Boxes 1 + 2 + 3", 12),
)
MAX_ACTIVE_WEIGHT_BPS = 300.0
```

Edit those, run the tests, push. Render redeploys on push to `main`.

Note that `tests/test_sizer.py` pins the current numbers against the workbook, so
changing a rule will fail tests on purpose. Update the expected values in the same
commit and the diff becomes the record of what changed.
