"""The refresh pipeline: named stages, isolated failures, one lock.

Two ideas do all the work here.

**A stage that fails does not fail the run.** One news provider timing out
must not stop a portfolio being revalued from prices that arrived fine.
Each stage runs inside its own try, records what happened, and the run
continues. What it must *not* do is pretend everything was fine — a run that
finished with two of nine stages degraded reports `degraded`, listing which,
because calling it a success trains people to ignore the status and calling
it a failure trains them to ignore that too.

**Stages declare what they need.** A stage whose dependency degraded is
skipped rather than run against absent data, since computing a portfolio
value from prices that never arrived produces a number rather than an error,
and a number is much harder to notice.

The lock is a database row. Work reaches this system from GitHub Actions, a
launchd agent, the web app and a terminal; those four share nothing else.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from akmarkets.activity import JobReporter, NullReporter
from akmarkets.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class StageContext:
    """What a stage is given, and where it puts what it learned."""

    session: Session
    settings: Settings
    report: JobReporter
    run_id: str
    # Free-form results, keyed by stage name, so a later stage can use what
    # an earlier one produced without a global.
    results: dict[str, Any] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)


StageFunction = Callable[[StageContext], dict[str, Any]]


@dataclass(frozen=True)
class Stage:
    name: str
    run: StageFunction
    # Stages whose success this one depends on. If any degraded or failed,
    # this stage is skipped rather than run against data that is not there.
    requires: tuple[str, ...] = ()
    description: str = ""


@dataclass
class StageOutcome:
    name: str
    state: str  # ok | degraded | skipped
    detail: str = ""
    items: int = 0


@dataclass
class PipelineResult:
    run_id: str
    outcomes: list[StageOutcome]

    @property
    def degraded(self) -> list[str]:
        return [o.name for o in self.outcomes if o.state in {"degraded", "skipped"}]

    @property
    def ok(self) -> bool:
        return not self.degraded

    @property
    def items(self) -> int:
        return sum(outcome.items for outcome in self.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ok": self.ok,
            "degraded": self.degraded,
            "stages": [
                {"name": o.name, "state": o.state, "detail": o.detail, "items": o.items}
                for o in self.outcomes
            ],
        }


def run_stages(
    stages: Sequence[Stage],
    session: Session,
    settings: Settings,
    run_id: str,
    report: JobReporter | None = None,
    only: Sequence[str] | None = None,
) -> PipelineResult:
    """Run stages in order, isolating each one's failure.

    `only` restricts the run to named stages, for re-running the one thing
    that broke without repeating an hour of work. Dependency checks still
    apply, so a restricted run cannot quietly produce a stage's output from
    inputs that were never refreshed.
    """
    reporter = report or NullReporter()
    selected = [stage for stage in stages if not only or stage.name in only]
    context = StageContext(session=session, settings=settings, report=reporter, run_id=run_id)
    outcomes: list[StageOutcome] = []
    healthy: set[str] = set()

    for index, stage in enumerate(selected, start=1):
        missing = [name for name in stage.requires if name not in healthy]
        if missing:
            detail = f"skipped: {', '.join(missing)} did not complete"
            reporter.note(f"{stage.name} {detail}", level="warn")
            outcomes.append(StageOutcome(stage.name, "skipped", detail))
            continue

        reporter.step(stage.name, index, len(selected))
        try:
            produced = stage.run(context) or {}
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("stage %s degraded: %s", stage.name, detail)
            reporter.degraded(stage.name, detail)
            reporter.note(
                f"{stage.name} failed", level="error", detail=traceback.format_exc(limit=3)
            )
            outcomes.append(StageOutcome(stage.name, "degraded", detail))
            continue

        context.results[stage.name] = produced
        healthy.add(stage.name)
        items = int(produced.get("items", 0) or 0)
        detail = str(produced.get("detail", ""))
        # A stage may complete and still report itself degraded -- a price
        # refresh where two of forty symbols failed did its job and must
        # still say so.
        state = "degraded" if produced.get("degraded") else "ok"
        if state == "degraded":
            reporter.degraded(stage.name, detail or "partial result")
        outcomes.append(StageOutcome(stage.name, state, detail, items))

    return PipelineResult(run_id=run_id, outcomes=outcomes)


def utc_now() -> datetime:
    return datetime.now(UTC)
