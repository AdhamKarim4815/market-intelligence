# Calibration log

What the thresholds are, **what was measured to choose them**, and where the
scoring still fails. Kept because shipping an unmeasured number without
saying so is how a made-up score becomes an accepted fact.

Three items were open on 2026-08-10. All three were measured the same day.
One was a bug, one settled, one **failed**.

---

## 1. News materiality — was a bug, fixed

Every alert scored `materiality 1.00`, and the component was doing no
discriminating at all. This looked like a threshold problem. It was not.

Measured across 123 stored articles, the term `target` — one of the
`VALUATION` stems — matched **123 of 123**:

```
top matching terms (before)
   valuation:target             123      ← every article
   short_term_price:earnings     17
   short_term_price:beat          7
   revenue:revenue                6
```

The cause: `GoogleNewsProvider` stored the RSS `<description>` verbatim, and
that element is an HTML anchor, not prose:

```
SUMMARY: <a href="https://news.google.com/rss/articles/CBMizgFBVV95cUxQS3RNSFJ..." target="_blank">
```

Every keyword match was reading `target`, `href` and a base64 URL as words a
journalist had written. Fixed in `providers/news.py::strip_markup`, with the
distribution re-measured on the same 123 articles:

| materiality | before | after |
|---|---|---|
| 0.00 | 0 | **71 (58%)** |
| 0.33 | 74 (60%) | 39 (32%) |
| 0.67 | 35 (28%) | 10 (8%) |
| 1.00 | 14 (11%) | **3 (2%)** |

Two lexicon changes followed from inspecting the survivors:

- **Bare `target` removed from VALUATION.** Of the three articles in the
  corpus containing the word once markup was gone, one was a genuine price
  target — already caught by the `price target` term — and two were
  *production* targets, which REVENUE already matched via `production`. The
  term could only add false valuation matches.
- **`chief financial officer` added to MANAGEMENT.** `cfo` was there and
  the spelled-out form was not, so a headline that wrote it out matched
  nothing. A synonym completion, not a new judgement.

Final state, and no single term dominates any more:

```
materiality  0.00  71 (58%)   |   top terms   short_term_price:earnings  17
             0.33  40 (33%)   |               short_term_price:beat       7
             0.67  10 (8%)    |               revenue:revenue             6
             1.00   2 (2%)    |               short_term_price:miss       5
```

The component now separates. Opinion pieces ("Buy, Sell or Hold Apple
Stock?") score 0; substantive events score above it.

**The lesson is not about news.** Any text arriving from a feed is markup
until something strips it, and markup keywords are invisible in exactly the
way that makes a score look plausible while measuring nothing. Both
defences are kept and tested — the markup is stripped, *and* the term that
caught it is gone — because a future lexicon addition like `link` or
`image` would otherwise reintroduce the same failure silently.

## 2. News clustering — settled at 0.40, by measurement

116 real articles, eight same-event pairs labelled by hand from the 24
highest-similarity same-symbol candidates. The threshold sweep:

```
  thr   merged  correct  false   precision  recall
  0.30       6        3      3        50%      38%
  0.35       4        2      2        50%      25%    ← previous value
  0.40       1        1      0       100%      12%    ← now
  0.45       1        1      0       100%      12%
  0.50       1        1      0       100%      12%
```

**Raised, not lowered** — the opposite of what the earlier note expected.
Precision collapses below 0.40 and never recovers, because financial
headlines share vocabulary without sharing an occurrence. The two clearest
false merges at 0.35:

- *"After Earnings, Is Alphabet Stock a Buy, a Sell, or Fairly Valued?"* and
  *"Alphabet Stock: Buy or Sell? (My Final Verdict)"* — 0.36, two outlets'
  opinions, no shared event.
- *"Example Lithium Corp Earnings Forecast: Future EPS & Revenue Growth"* and
  *"Example Lithium Corp Stock Analysis & Forecast"* — 0.38, two generic pages.

Recall of 12% is poor and is stated as poor. The trade is deliberate: a
false merge hides a story permanently inside another, while a missed merge
shows the reader two headlines about one thing, which they can see.

Two alternatives were tried and rejected on the numbers:

- **Removing finance boilerplate** (`stock`, `earnings`, `forecast`, the
  ticker) from the token set. It strips shared tokens from genuine pairs
  too — recall fell at every threshold below 0.35.
- **Adding the article summary as a second signal.** Google News sets the
  description to the title restated plus the outlet name, so it carries no
  information the title does not already have.

Better clustering needs entity extraction, not another threshold.

## 3. Attention — measured, and it failed

`CALIBRATION.md` previously specified the test: replay events, measure the
subsequent benchmark-relative return, and ask whether attention rank-orders
those outcomes *at all*. It was run over 88 events with cached forward
prices.

```
                                          1d        5d
  Spearman(attention, |abnormal return|)  +0.04    −0.40   (n=88 / n=55)
```

**It does not rank what mattered.** The 1-day figure is indistinguishable
from zero; the 5-day figure points the wrong way.

One confound was found and removed rather than left as an excuse. Attention
includes a freshness decay, and only *older* events have forward price data
— so recency was driving both the score and whether an event was measurable
at all (Spearman(attention, age) = −0.34). Re-scoring every event as of its
own date, which fixes freshness at 1.0 for all of them, changes the result
to +0.04 and −0.40. The confound was real and it was not the explanation.

**A second finding, more concrete.** The observed score range across those
88 events was **0.000–0.125**, against `Severity` cuts of 0.15 / 0.35 /
0.55. The lowest band is unreachable: every item is `INFO`, and the four
severity levels are inert in production. This follows from the conjunctive
product — realistic components (materiality 0.33 × weight 0.25 ×
confidence 0.6 × novelty 1.0) reach about 0.05.

**What was deliberately not done:** re-cutting the bands as percentiles of
the observed distribution. That would make the bands fire, and it would
guarantee something is labelled `CRITICAL` every day regardless of whether
anything happened — destroying the one property the brief depends on, that
"nothing needs your attention" is reachable and true. An inert band is a
visible failure. A percentile band is an invisible one.

**What the score is still for.** Ordering an inbox by relevance to *this*
portfolio is defensible and is what the product actually is: material,
heavily-weighted, well-sourced items belong at the top whether or not the
price subsequently moved. The score is kept for that and no longer claims,
in code or in the UI, to predict anything.

**What would settle it properly.** 88 events over 30 days across four
symbols in one regime is enough to fail a score, not enough to fix one. A
real calibration needs a year of stored events across a wider universe —
which the nightly job now accumulates, so this becomes answerable by
waiting rather than by building anything.

---

## First real backtest — 2026-08-10

The `oversold` rule (RSI crossing below 30), replayed over five years of
cached history for AAPL, GOOGL and XMPL against SPY:

```
signals    38     window 2021-08-11 → 2026-08-07

  1d:   n=38  mean -0.43%  median -0.44%  beat 37%
  5d:   n=38  mean +0.72%  median +0.20%  beat 53%
  20d:  n=36  mean -0.94%  median -0.22%  beat 50%
  60d:  n=36  mean -2.68%  median -0.09%  beat 47%
  120d: n=35  mean -3.14%  median -1.80%  beat 46%
```

**The rule lagged.** That is the result, and it is reported rather than
tuned away — which is the entire point of building this before trusting any
of the scores.

Two things to note about how to read it. The mean and median diverge sharply
at 60 and 120 days (−2.68% against −0.09%), which means one or two outcomes
are carrying the mean; with 36 observations that is the normal case, not an
anomaly. And a three-symbol universe over one regime is not a test of
anything — it is a demonstration that the machinery runs and does not cheat.

---

## Standing rule

Two of the three items above were settled in the direction that made the
output *look worse* — clustering merges less, materiality fires on 42% of
articles instead of 100%, and attention is now documented as not predicting
anything. That is the expected shape of an honest calibration pass. A round
of tuning where every number improves is a round of tuning that was fitted
to the sample.
