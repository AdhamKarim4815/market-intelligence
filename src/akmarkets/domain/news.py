"""News intelligence: from a pile of headlines to a few things worth reading.

Pure functions. No network, no storage, no model downloads — every stage
below is deterministic and testable against a fixture, which is the point:
the expensive parts of this pipeline exist to decide *whether* to spend
money on a strong model, so they cannot themselves depend on one.

The pipeline, in order, and why each stage exists:

1. **Normalise** — strip the syndication cruft that makes identical stories
   look different.
2. **Deduplicate** — fifty outlets carrying one wire story must not become
   fifty signals. Exact-hash first, then token overlap for rewrites.
3. **Cluster into events** — the unit a human cares about is "XMPL reported
   production", not eight articles about it.
4. **Score novelty** — an article restating yesterday's announcement is not
   news. Novelty decays within a cluster.
5. **Classify the source** — a regulatory filing and an anonymous forum post
   are not the same kind of claim.
6. **Sentiment** — a lexicon with negation scoping, which is enough to be
   useful and honest about being shallow.
7. **Materiality** — could this plausibly move revenue, margin, cash, or the
   thesis? This is what gates attention and spend.

Nothing here decides that an article is *true*. It decides how much
attention it has earned and how confident that judgement is.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import IntEnum, StrEnum

# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

# Google News and most aggregators append the outlet to the headline, and
# the same wire story arrives with three different suffixes. Stripping them
# is what lets the hash match.
_OUTLET_SUFFIX = re.compile(r"\s+[-–—|]\s+[^-–—|]{2,40}$")
_WHITESPACE = re.compile(r"\s+")
# Straight, curly, and the prime that some feeds use.
_APOSTROPHES = re.compile("['\u2019\u02bc\u2032]")
_PUNCTUATION = re.compile(r"[^\w\s]")

# Words that carry no information about which story this is. Deliberately
# short: an aggressive stop list makes unrelated headlines collide, and a
# false merge loses a story entirely, which is worse than a duplicate.
_STOPWORDS = frozenset(
    [
        "a", "an", "the", "and", "or", "but", "of", "for", "to", "in", "on",
        "at", "by", "with", "from", "as", "is", "are", "was", "were", "be",
        "been", "being", "it", "its", "this", "that", "these", "those",
        "said", "says", "say", "new", "update",
    ]
)


def normalise_title(title: str) -> str:
    """A headline reduced to what identifies the story.

    Apostrophes are deleted rather than turned into spaces, and that has to
    happen *before* the ASCII fold. A curly apostrophe is dropped by the
    fold while a straight one survives to become a space, so `XMPL’s` and
    `XMPL's` would normalise to `xmpls` and `xmpl s` and hash differently —
    two copies of one wire story, from two outlets with different house
    style, counted as two events.
    """
    text = _APOSTROPHES.sub("", title)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = _OUTLET_SUFFIX.sub("", text)
    text = _PUNCTUATION.sub(" ", text.lower())
    return _WHITESPACE.sub(" ", text).strip()


def title_hash(title: str) -> str:
    return hashlib.sha256(normalise_title(title).encode()).hexdigest()[:32]


def tokens(title: str) -> frozenset[str]:
    return frozenset(
        word for word in normalise_title(title).split() if word not in _STOPWORDS
    )


def similarity(left: str, right: str) -> Decimal:
    """Jaccard overlap of significant words, in [0, 1].

    Chosen over an embedding model because the base install must not carry
    several hundred megabytes of torch for a job that a set intersection
    does adequately. It catches syndication and light rewrites, which is the
    overwhelming majority of duplication; it will miss a genuine paraphrase,
    and the `ml` extra exists for when that matters enough to measure.
    """
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return Decimal(0)
    return Decimal(len(a & b)) / Decimal(len(a | b))


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


class SourceClass(IntEnum):
    """How much weight a claim from this kind of source has earned.

    Ordered, and the order is the whole point: a regulatory filing is a
    disclosure with legal consequences for being wrong, and a forum post is
    somebody typing. Treating them alike is how a rumour becomes a position.

    The numbers are ranks, not confidences. They are never multiplied into
    anything; they select a documented weight in `SOURCE_CONFIDENCE`.
    """

    REGULATORY_FILING = 8
    GOVERNMENT = 7
    COMPANY_DISCLOSURE = 6
    MAJOR_FINANCIAL_NEWS = 5
    INDUSTRY_PUBLICATION = 4
    OTHER_PUBLICATION = 3
    SOCIAL_MEDIA = 2
    UNCONFIRMED = 1


# Confidence that a claim from this class of source is factually accurate.
# These are judgements, not measurements, and are stated here rather than
# buried so they can be argued with. They are used only to scale attention,
# never to assert that something is true.
SOURCE_CONFIDENCE: dict[SourceClass, Decimal] = {
    SourceClass.REGULATORY_FILING: Decimal("1.00"),
    SourceClass.GOVERNMENT: Decimal("0.95"),
    SourceClass.COMPANY_DISCLOSURE: Decimal("0.90"),
    SourceClass.MAJOR_FINANCIAL_NEWS: Decimal("0.80"),
    SourceClass.INDUSTRY_PUBLICATION: Decimal("0.65"),
    SourceClass.OTHER_PUBLICATION: Decimal("0.50"),
    SourceClass.SOCIAL_MEDIA: Decimal("0.25"),
    SourceClass.UNCONFIRMED: Decimal("0.15"),
}

_REGULATORY = ("sec.gov", "edgar", "sedar", "fca.org.uk", "asic.gov.au")
_GOVERNMENT = (".gov", ".gov.uk", "europa.eu", "federalreserve", "bls.gov")
_COMPANY = ("globenewswire", "businesswire", "prnewswire", "newsfile", "accesswire")
_MAJOR = (
    "reuters", "bloomberg", "wsj.com", "ft.com", "cnbc", "barrons",
    "marketwatch", "apnews", "associated press", "financial times",
)
_INDUSTRY = (
    "mining.com", "oilprice", "seekingalpha", "benzinga", "investing.com",
    "fastmarkets", "argusmedia", "spglobal",
)
_SOCIAL = ("reddit", "twitter", "x.com", "stocktwits", "youtube", "tiktok")


def classify_source(source: str, url: str = "") -> SourceClass:
    """Bucket a source by name or URL.

    Unknown sources land in `OTHER_PUBLICATION` rather than `UNCONFIRMED`:
    the default should be "an outlet we have not catalogued", not an
    accusation. `UNCONFIRMED` is reserved for text that says so itself.
    """
    haystack = f"{source} {url}".lower()
    for needles, verdict in (
        (_REGULATORY, SourceClass.REGULATORY_FILING),
        (_GOVERNMENT, SourceClass.GOVERNMENT),
        (_COMPANY, SourceClass.COMPANY_DISCLOSURE),
        (_MAJOR, SourceClass.MAJOR_FINANCIAL_NEWS),
        (_SOCIAL, SourceClass.SOCIAL_MEDIA),
        (_INDUSTRY, SourceClass.INDUSTRY_PUBLICATION),
    ):
        if any(needle in haystack for needle in needles):
            return verdict
    return SourceClass.OTHER_PUBLICATION


class Confirmation(StrEnum):
    CONFIRMED = "confirmed"
    CREDIBLE_REPORT = "credible_report"
    UNCONFIRMED = "unconfirmed"
    RUMOUR = "rumour"
    SPECULATION = "speculation"


_HEDGES = (
    "reportedly", "rumou", "speculat", "may ", "might ", "could ", "is said to",
    "people familiar", "sources say", "unconfirmed", "allegedly", "weighing",
    "exploring", "considering", "in talks",
)


def classify_confirmation(title: str, source_class: SourceClass) -> Confirmation:
    """How firm the claim is, from its own language and its source.

    A filing states; a story about people familiar with the matter hedges.
    Both can be right, and the difference decides how much of a position a
    reader should stake on it.
    """
    hedged = any(hedge in title.lower() for hedge in _HEDGES)
    if source_class >= SourceClass.COMPANY_DISCLOSURE:
        return Confirmation.UNCONFIRMED if hedged else Confirmation.CONFIRMED
    if source_class is SourceClass.SOCIAL_MEDIA:
        return Confirmation.SPECULATION
    if hedged:
        return Confirmation.RUMOUR
    return Confirmation.CREDIBLE_REPORT


# ---------------------------------------------------------------------------
# sentiment
# ---------------------------------------------------------------------------

# A finance lexicon rather than a general one: "beat", "miss", "dilution"
# and "guidance" carry meanings here that a general sentiment model gets
# backwards. Shallow on purpose, and the module says so — sentiment is one
# input to materiality, never a conclusion.
_POSITIVE = {
    "beat": 2, "beats": 2, "exceeded": 2, "record": 2, "surge": 2, "soar": 2,
    "upgrade": 2, "upgraded": 2, "raises": 2, "raised": 1, "growth": 1,
    "profit": 1, "approval": 2, "approved": 2, "expansion": 1, "wins": 2,
    "awarded": 2, "strong": 1, "outperform": 2, "buyback": 2, "dividend": 1,
    "breakthrough": 2, "partnership": 1, "acquire": 1, "rally": 2, "rises": 1,
}
_NEGATIVE = {
    "miss": 2, "misses": 2, "missed": 2, "cut": 2, "cuts": 2, "downgrade": 2,
    "downgraded": 2, "loss": 2, "losses": 2, "delay": 2, "delayed": 2,
    "halt": 2, "halted": 2, "lawsuit": 2, "probe": 2, "investigation": 2,
    "recall": 2, "bankruptcy": 3, "default": 3, "dilution": 2, "writedown": 2,
    "impairment": 2, "resign": 2, "resigned": 2, "fraud": 3, "plunge": 2,
    "slump": 2, "warns": 2, "warning": 2, "weak": 1, "falls": 1, "declines": 1,
    "layoff": 2, "layoffs": 2, "suspend": 2, "suspended": 2,
}
_NEGATORS = frozenset({"no", "not", "never", "without", "avoids", "avoided", "denies"})

# How far a negator reaches. Three words covers "not a strong quarter"
# without letting one negation invert an entire headline.
_NEGATION_SCOPE = 3


@dataclass(frozen=True)
class Sentiment:
    score: Decimal  # -1..1
    positive_hits: tuple[str, ...] = ()
    negative_hits: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        if self.score >= Decimal("0.2"):
            return "positive"
        if self.score <= Decimal("-0.2"):
            return "negative"
        return "neutral"

    @property
    def is_meaningful(self) -> bool:
        """Whether any lexicon word was found at all.

        A score of zero because nothing matched is not the same as a score
        of zero because positives and negatives cancelled, and the UI must
        not render the first as "neutral sentiment" — it is "no signal".
        """
        return bool(self.positive_hits or self.negative_hits)


def sentiment(title: str) -> Sentiment:
    """Lexicon sentiment with negation scoping.

    "Company avoids bankruptcy" and "company files for bankruptcy" contain
    the same word and mean opposite things. Without scoping, the first would
    be scored as catastrophic.
    """
    words = normalise_title(title).split()
    positive_hits: list[str] = []
    negative_hits: list[str] = []
    score = 0

    for index, word in enumerate(words):
        weight = _POSITIVE.get(word, 0) or -_NEGATIVE.get(word, 0)
        if weight == 0:
            continue
        window = words[max(0, index - _NEGATION_SCOPE) : index]
        if any(token in _NEGATORS for token in window):
            weight = -weight
        score += weight
        (positive_hits if weight > 0 else negative_hits).append(word)

    # Squashed rather than divided by word count: a headline with one strong
    # word and a headline with one strong word plus filler mean the same
    # thing, and dividing would make the wordier one look weaker.
    bounded = max(-4, min(4, score))
    return Sentiment(
        score=(Decimal(bounded) / Decimal(4)),
        positive_hits=tuple(positive_hits),
        negative_hits=tuple(negative_hits),
    )


# ---------------------------------------------------------------------------
# materiality
# ---------------------------------------------------------------------------


class MaterialityDimension(StrEnum):
    """What a development could plausibly change.

    Named dimensions rather than one number, because "could affect the
    balance sheet" and "could affect the short-term price" call for
    different reactions and collapsing them loses that.
    """

    REVENUE = "revenue"
    MARGIN = "margin"
    CASH_FLOW = "cash_flow"
    BALANCE_SHEET = "balance_sheet"
    VALUATION = "valuation"
    STRATEGY = "strategy"
    REGULATORY = "regulatory"
    MANAGEMENT = "management"
    SHORT_TERM_PRICE = "short_term_price"


_MATERIALITY_TERMS: dict[MaterialityDimension, tuple[str, ...]] = {
    MaterialityDimension.REVENUE: (
        "revenue", "sales", "orders", "backlog", "production", "output",
        "shipments", "contract", "customers", "demand", "guidance",
    ),
    MaterialityDimension.MARGIN: (
        "margin", "cost", "costs", "pricing", "efficiency", "inflation",
    ),
    MaterialityDimension.CASH_FLOW: (
        "cash", "free cash", "capex", "capital expenditure", "buyback", "dividend",
    ),
    MaterialityDimension.BALANCE_SHEET: (
        "debt", "refinanc", "offering", "dilution", "raise", "equity", "loan",
        "impairment", "writedown", "bankruptcy", "default",
    ),
    MaterialityDimension.VALUATION: (
        # Bare "target" was removed, 2026-08-10. It was the term that made
        # this dimension match 123 of 123 articles when RSS markup was
        # being scored as prose, and it stays out now the markup is gone:
        # of the three articles in the corpus containing the word, one is a
        # genuine price target (caught by "price target") and two are
        # *production* targets, which are already REVENUE via "production".
        # It could only add false valuation matches.
        "price target", "rating", "upgrade", "downgrade", "estimates",
        "consensus", "valuation", "multiple",
    ),
    MaterialityDimension.STRATEGY: (
        "acquisition", "merger", "acquire", "divest", "spin-off", "partnership",
        "joint venture", "expansion", "restructur", "strategic",
    ),
    MaterialityDimension.REGULATORY: (
        "regulat", "approval", "fda", "antitrust", "probe", "investigation",
        "lawsuit", "fine", "sanction", "permit", "licence", "license", "tariff",
    ),
    MaterialityDimension.MANAGEMENT: (
        # The spelled-out forms are synonyms of abbreviations already here,
        # not new judgements: a headline saying "chief financial officer"
        # rather than "CFO" is the same event, and matching only the
        # abbreviation missed it entirely.
        "ceo", "cfo", "chief executive", "chief financial officer",
        "resign", "appoint", "board", "succession",
    ),
    MaterialityDimension.SHORT_TERM_PRICE: (
        "earnings", "results", "quarter", "beat", "miss", "halted", "offering",
        "split", "index", "inclusion",
    ),
}

# A development touching this many dimensions is structural rather than
# incidental. Two is the threshold because one term is easily incidental
# ("the CEO said") while two co-occurring is usually a real event.
_STRUCTURAL_DIMENSIONS = 2


@dataclass(frozen=True)
class Materiality:
    dimensions: tuple[MaterialityDimension, ...]
    score: Decimal  # 0..1

    @property
    def is_material(self) -> bool:
        return self.score >= Decimal("0.3")

    @property
    def is_structural(self) -> bool:
        return len(self.dimensions) >= _STRUCTURAL_DIMENSIONS


def materiality(title: str, summary: str = "") -> Materiality:
    """Which financial dimensions a headline plausibly touches.

    Deliberately generous about *plausibly*: this stage decides what gets
    looked at more closely, so a false positive costs a cheap classification
    and a false negative loses the story entirely.
    """
    haystack = f"{title} {summary}".lower()
    found = tuple(
        dimension
        for dimension, terms in _MATERIALITY_TERMS.items()
        if any(term in haystack for term in terms)
    )
    if not found:
        return Materiality(dimensions=(), score=Decimal(0))
    # Saturating rather than linear: touching six dimensions is not twice as
    # material as touching three, it usually means the headline is long.
    score = min(Decimal(len(found)) / Decimal(3), Decimal(1))
    return Materiality(dimensions=found, score=score)


# ---------------------------------------------------------------------------
# articles, deduplication, events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Article:
    article_id: str
    title: str
    url: str
    source: str
    published_at: datetime
    summary: str = ""
    symbols: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        return title_hash(self.title)


@dataclass
class Event:
    """A cluster of articles about one occurrence.

    The unit a human cares about is "XMPL reported production", not the
    eight articles carrying it. `articles` keeps every one so the event can
    be opened up, and `first_seen` is what novelty is measured against.
    """

    event_id: str
    key_title: str
    symbols: tuple[str, ...]
    first_seen: datetime
    last_seen: datetime
    articles: list[Article] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.articles)

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(sorted({article.source for article in self.articles}))

    @property
    def best_source_class(self) -> SourceClass:
        """The strongest source in the cluster.

        A wire story picked up by a filing is as good as the filing. Taking
        the best rather than the average stops eight blog rewrites from
        diluting one primary document.
        """
        return max(
            (classify_source(a.source, a.url) for a in self.articles),
            default=SourceClass.OTHER_PUBLICATION,
        )


def deduplicate(
    articles: Iterable[Article], threshold: Decimal = Decimal("0.7")
) -> tuple[list[Article], dict[str, str]]:
    """Collapse identical and near-identical articles.

    Returns the survivors and a map of dropped id → kept id, so nothing
    vanishes silently: the Data page can say "fifty articles, eight
    stories" rather than quietly showing eight.

    Exact fingerprint first because it is free and catches syndication. Then
    token overlap for rewrites, compared only against survivors, which keeps
    this linear in practice.
    """
    survivors: list[Article] = []
    duplicate_of: dict[str, str] = {}
    by_fingerprint: dict[str, str] = {}

    for article in sorted(articles, key=lambda a: (a.published_at, a.article_id)):
        exact = by_fingerprint.get(article.fingerprint)
        if exact is not None:
            duplicate_of[article.article_id] = exact
            continue

        near = next(
            (
                kept.article_id
                for kept in survivors
                if similarity(kept.title, article.title) >= threshold
            ),
            None,
        )
        if near is not None:
            duplicate_of[article.article_id] = near
            continue

        by_fingerprint[article.fingerprint] = article.article_id
        survivors.append(article)

    return survivors, duplicate_of


def cluster_events(
    articles: Sequence[Article],
    window: timedelta = timedelta(days=3),
    threshold: Decimal = Decimal("0.40"),
) -> list[Event]:
    """Group articles into events by subject and time.

    Two articles join the same event if they share a symbol, fall within the
    window, and overlap enough in wording. The similarity bar is much lower
    than deduplication's on purpose: "XMPL starts production" and "XMPL
    output beats guidance" are the same event told twice, not duplicates.

    The window matters because the same words a month apart are a different
    occurrence — a company reports production every quarter.

    **0.40 was measured, 2026-08-10**, against 116 real articles with eight
    same-event pairs labelled by hand (`docs/CALIBRATION.md`). The sweep:

        thr    merged  correct  false   precision  recall
        0.30        6        3      3        50%      38%
        0.35        4        2      2        50%      25%
        0.40        1        1      0       100%      12%

    Precision collapses below 0.40 and does not recover, because financial
    headlines share vocabulary — *stock*, *earnings*, *forecast*, the
    ticker — without describing the same occurrence. "Is Alphabet a Buy or
    Fairly Valued?" and "Alphabet: Buy or Sell?" score 0.36 and are two
    outlets' opinions, not one event.

    Raised rather than lowered, which is the uncomfortable direction: it
    merges less and leaves more apparent duplication in the feed. That is
    the correct trade here. A false merge hides one story permanently
    inside another; a missed merge shows the reader two headlines about the
    same thing, and they can see that for themselves.

    Adding the article summary as a second signal was tried and rejected —
    Google News sets the description to the title restated plus the outlet
    name, so it carries no information the title does not already have.
    Meaningfully better clustering needs entity extraction, not another
    threshold.
    """
    events: list[Event] = []
    for article in sorted(articles, key=lambda a: (a.published_at, a.article_id)):
        match = next(
            (
                event
                for event in events
                if (set(event.symbols) & set(article.symbols) or not article.symbols)
                and article.published_at - event.last_seen <= window
                and similarity(event.key_title, article.title) >= threshold
            ),
            None,
        )
        if match is None:
            events.append(
                Event(
                    event_id=f"ev-{article.fingerprint}",
                    key_title=article.title,
                    symbols=article.symbols,
                    first_seen=article.published_at,
                    last_seen=article.published_at,
                    articles=[article],
                )
            )
        else:
            match.articles.append(article)
            match.last_seen = max(match.last_seen, article.published_at)
            match.symbols = tuple(sorted(set(match.symbols) | set(article.symbols)))
    return events


# ---------------------------------------------------------------------------
# novelty and decay
# ---------------------------------------------------------------------------

# How quickly an event stops being news, by what kind of thing it is.
# Half-lives in days, stated here rather than scattered so they can be
# argued with and adjusted. A routine story is stale within a day; a
# regulatory change reshapes the business for months.
HALF_LIFE_DAYS: dict[MaterialityDimension | None, Decimal] = {
    MaterialityDimension.REGULATORY: Decimal(45),
    MaterialityDimension.STRATEGY: Decimal(30),
    MaterialityDimension.BALANCE_SHEET: Decimal(21),
    MaterialityDimension.MANAGEMENT: Decimal(14),
    MaterialityDimension.REVENUE: Decimal(10),
    MaterialityDimension.MARGIN: Decimal(10),
    MaterialityDimension.CASH_FLOW: Decimal(10),
    MaterialityDimension.VALUATION: Decimal(5),
    MaterialityDimension.SHORT_TERM_PRICE: Decimal(3),
    None: Decimal(2),
}


def half_life(dimensions: Sequence[MaterialityDimension]) -> Decimal:
    """The slowest-decaying dimension a development touches.

    Slowest rather than average: a story that is both an earnings beat and a
    regulatory approval stops mattering when the *approval* stops mattering.
    """
    if not dimensions:
        return HALF_LIFE_DAYS[None]
    return max(HALF_LIFE_DAYS.get(dimension, Decimal(2)) for dimension in dimensions)


def decay(age_days: Decimal, life: Decimal) -> Decimal:
    """Exponential decay, approximated without floats.

    Halving per half-life, computed by repeated halving plus a linear
    interpolation inside the final period. Exact enough for ranking, and it
    keeps the whole pipeline in `Decimal` so a score is reproducible rather
    than drifting with the platform's floating point.
    """
    if age_days <= 0 or life <= 0:
        return Decimal(1)
    whole, remainder = divmod(age_days, life)
    value = Decimal(1) / (Decimal(2) ** int(whole)) if whole < 32 else Decimal(0)
    if remainder > 0 and value > 0:
        fraction = remainder / life
        value *= Decimal(1) - (fraction / Decimal(2))
    return max(Decimal(0), min(Decimal(1), value))


def novelty(event: Event, article: Article) -> Decimal:
    """How much this article adds to an event we already know about.

    The first article in a cluster is entirely novel. Later ones are worth
    progressively less, because a story being repeated is not the story
    happening again — which is exactly the trap that makes news volume look
    like news importance.
    """
    if not event.articles or event.articles[0].article_id == article.article_id:
        return Decimal(1)
    position = next(
        (i for i, a in enumerate(event.articles) if a.article_id == article.article_id),
        event.size,
    )
    return max(Decimal("0.05"), Decimal(1) / Decimal(1 + position))


# ---------------------------------------------------------------------------
# attention
# ---------------------------------------------------------------------------


class Severity(StrEnum):
    INFO = "info"
    WATCH = "watch"
    IMPORTANT = "important"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Attention:
    """Why something did or did not earn a place in the inbox.

    Every component is kept, not just the product. A score nobody can
    decompose is a score nobody can argue with, and this one *will* be
    wrong sometimes.
    """

    score: Decimal
    materiality: Decimal
    portfolio_weight: Decimal
    confidence: Decimal
    freshness: Decimal
    novelty: Decimal

    @property
    def severity(self) -> Severity:
        if self.score >= Decimal("0.55"):
            return Severity.CRITICAL
        if self.score >= Decimal("0.35"):
            return Severity.IMPORTANT
        if self.score >= Decimal("0.15"):
            return Severity.WATCH
        return Severity.INFO

    def explain(self) -> str:
        return (
            f"materiality {self.materiality:.2f} x weight {self.portfolio_weight:.2f} "
            f"x confidence {self.confidence:.2f} x freshness {self.freshness:.2f} "
            f"x novelty {self.novelty:.2f} = {self.score:.3f}"
        )


def attention(
    materiality_score: Decimal,
    portfolio_weight: Decimal,
    confidence: Decimal,
    freshness: Decimal,
    novelty_score: Decimal,
) -> Attention:
    """Combine the components into one ranking number.

    A product, not a weighted sum, and the choice is deliberate: these
    factors are *conjunctive*. A hugely material event in a position you do
    not hold deserves no alert, and a certain, fresh, novel report about
    nothing deserves none either. A weighted sum lets one large factor carry
    an item over the threshold on its own, which is precisely the failure
    mode — "big news!" about a 0.2% holding.

    **This score failed its outcome test, 2026-08-10.** It was measured
    against subsequent benchmark-relative returns over 88 real events
    (`docs/CALIBRATION.md`):

        Spearman(attention, |abnormal return|)   1d: +0.04    5d: −0.40

    It does not rank-order what mattered. The 1-day figure is nothing; the
    5-day figure points the wrong way. Neither number is left uncorrected
    for the obvious confound — scoring each event as of its own date, so
    freshness is 1.0 for all of them, moves the figures to +0.04 and −0.40
    and changes nothing.

    It is kept, because ordering an inbox by *relevance to this portfolio*
    is a defensible thing to do and is what the product is: material,
    heavily-weighted, well-sourced items belong at the top whether or not
    the price then moved. What it must not do is claim to predict, and the
    surrounding code no longer implies it does.

    **The `Severity` cuts are unreachable at this score's actual scale.**
    Observed range across those 88 events was **0.000–0.125** against a
    lowest cut of 0.15, so every item lands in `INFO` and the four bands are
    inert. That is a consequence of the conjunctive product: four factors
    each below 1 multiply to something small, and realistic values
    (materiality 0.33 × weight 0.25 × confidence 0.6 × novelty 1.0) reach
    about 0.05.

    They are deliberately **not** re-cut as percentiles of the observed
    distribution. Percentile bands would guarantee something is labelled
    `CRITICAL` every single day, which manufactures urgency out of a quiet
    week and destroys the one property the brief depends on — that
    "nothing needs your attention" is reachable and true. An inert band is
    a visible failure; a percentile band is an invisible one.
    """
    parts = Attention(
        score=Decimal(0),
        materiality=_clamp(materiality_score),
        portfolio_weight=_clamp(portfolio_weight),
        confidence=_clamp(confidence),
        freshness=_clamp(freshness),
        novelty=_clamp(novelty_score),
    )
    score = (
        parts.materiality
        * parts.portfolio_weight
        * parts.confidence
        * parts.freshness
        * parts.novelty
    )
    return Attention(
        score=score,
        materiality=parts.materiality,
        portfolio_weight=parts.portfolio_weight,
        confidence=parts.confidence,
        freshness=parts.freshness,
        novelty=parts.novelty,
    )


def _clamp(value: Decimal) -> Decimal:
    """Bound a component to [0, 1] rather than trusting the caller.

    Every one of these is a ratio produced somewhere else, and one of them
    arriving above 1 would inflate an alert past every threshold.
    """
    return max(Decimal(0), min(Decimal(1), value))


def age_in_days(moment: datetime, now: datetime | None = None) -> Decimal:
    reference = now or datetime.now(UTC)
    stamp = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return Decimal((reference - stamp).total_seconds()) / Decimal(86400)


@dataclass(frozen=True)
class ScoredEvent:
    """An event with everything the inbox needs to rank and explain it."""

    event: Event
    materiality: Materiality
    sentiment: Sentiment
    source_class: SourceClass
    confirmation: Confirmation
    attention: Attention

    @property
    def severity(self) -> Severity:
        return self.attention.severity


def score_event(
    event: Event,
    portfolio_weight: Decimal = Decimal(0),
    now: datetime | None = None,
) -> ScoredEvent:
    """Everything the pipeline knows about one event, in one pass."""
    lead = event.articles[0] if event.articles else None
    title = lead.title if lead else event.key_title
    summary = lead.summary if lead else ""

    impact = materiality(title, summary)
    tone = sentiment(title)
    source_class = event.best_source_class
    confirmation = classify_confirmation(title, source_class)

    life = half_life(impact.dimensions)
    freshness = decay(age_in_days(event.last_seen, now), life)

    # Corroboration: several independent outlets carrying a story is weak
    # evidence it happened. Capped low because syndication is not
    # independence -- fifty outlets running one wire story is one source.
    corroboration = min(Decimal(len(event.sources) - 1) / Decimal(10), Decimal("0.15"))
    confidence = min(SOURCE_CONFIDENCE[source_class] + corroboration, Decimal(1))

    return ScoredEvent(
        event=event,
        materiality=impact,
        sentiment=tone,
        source_class=source_class,
        confirmation=confirmation,
        attention=attention(
            materiality_score=impact.score,
            portfolio_weight=portfolio_weight,
            confidence=confidence,
            freshness=freshness,
            novelty_score=Decimal(1) if event.size == 1 else Decimal("0.8"),
        ),
    )


def pipeline(
    articles: Sequence[Article],
    weights: dict[str, Decimal] | None = None,
    now: datetime | None = None,
) -> tuple[list[ScoredEvent], dict[str, str]]:
    """Collect → dedupe → cluster → score, in one call.

    Returns the scored events sorted by attention, and the duplicate map so
    the count of what was collapsed is available rather than lost.
    """
    survivors, duplicates = deduplicate(articles)
    events = cluster_events(survivors)
    by_symbol = weights or {}

    scored = [
        score_event(
            event,
            portfolio_weight=max(
                (by_symbol.get(symbol, Decimal(0)) for symbol in event.symbols),
                default=Decimal(0),
            ),
            now=now,
        )
        for event in events
    ]
    scored.sort(key=lambda item: item.attention.score, reverse=True)
    return scored, duplicates


def as_of_date(moment: datetime) -> date:
    return moment.astimezone(UTC).date()
