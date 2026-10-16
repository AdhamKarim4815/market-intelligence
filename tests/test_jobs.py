"""Job locking and stage isolation — the two things this layer is for."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from akmarkets.db.engine import Database
from akmarkets.db.models import JobLockRow, JobRunRow
from akmarkets.jobs.pipeline import Stage, StageContext, run_stages
from akmarkets.jobs.refresh import STAGES, run_refresh
from akmarkets.services import jobs as job_service


@pytest.fixture
def session(migrated: Database) -> Iterator[Session]:
    with migrated.sessions.begin() as active:
        yield active


# -- locking ------------------------------------------------------------


def test_a_second_run_cannot_take_a_held_lock(session: Session) -> None:
    """A cron job and a manual run started in the same second is not
    hypothetical, and both writing prices is what the lock prevents."""
    job_service.acquire(session, "refresh", source="cron")
    with pytest.raises(job_service.JobBusy, match="cron"):
        job_service.acquire(session, "refresh", source="terminal")


def test_releasing_frees_the_lock_even_after_a_failure(session: Session) -> None:
    """A job that crashed should be retried, not blocked out by its own
    corpse."""
    handle = job_service.acquire(session, "refresh")
    job_service.release(session, handle, status="error", error="exploded")
    assert session.get(JobLockRow, "refresh") is None

    # And the next run may proceed.
    job_service.acquire(session, "refresh")


def test_a_stale_lock_is_taken_over_and_the_dead_run_is_marked(
    session: Session,
) -> None:
    """A crashed process cannot release its own lock. Without takeover the
    schedule would be blocked forever by a job that will never finish."""
    dead = job_service.acquire(session, "refresh", source="github-actions")
    lock = session.get(JobLockRow, "refresh")
    assert lock is not None
    lock.heartbeat_at = lock.heartbeat_at - job_service.STALE_AFTER - timedelta(minutes=1)
    session.flush()

    live = job_service.acquire(session, "refresh", source="cron")
    assert live.run_id != dead.run_id

    abandoned = session.get(JobRunRow, dead.run_id)
    assert abandoned is not None
    assert abandoned.status == "abandoned"
    assert "no heartbeat" in (abandoned.error or "")


def test_a_slow_but_living_job_is_not_stolen_from(session: Session) -> None:
    """Stealing from a merely slow job is worse than waiting: two
    concurrent refreshes is exactly what the lock exists to stop."""
    handle = job_service.acquire(session, "refresh")
    lock = session.get(JobLockRow, "refresh")
    assert lock is not None
    lock.heartbeat_at = lock.heartbeat_at - job_service.STALE_AFTER + timedelta(minutes=5)
    session.flush()
    job_service.heartbeat(session, handle)

    with pytest.raises(job_service.JobBusy):
        job_service.acquire(session, "refresh")


def test_different_jobs_do_not_block_each_other(session: Session) -> None:
    job_service.acquire(session, "refresh")
    job_service.acquire(session, "weekly")  # must not raise


# -- stage isolation ----------------------------------------------------


def _stage(name: str, **kwargs: Any) -> Stage:
    def run(context: StageContext) -> dict[str, Any]:
        context.report.note(name)
        return {"items": 1}

    return Stage(name, run, **kwargs)


def _failing(name: str, **kwargs: Any) -> Stage:
    def run(context: StageContext) -> dict[str, Any]:
        raise RuntimeError(f"{name} exploded")

    return Stage(name, run, **kwargs)


def test_one_failing_stage_does_not_stop_the_others(
    session: Session, settings: Any
) -> None:
    """A news provider timing out must not stop a portfolio being revalued
    from prices that arrived fine."""
    result = run_stages(
        [_stage("prices"), _failing("news"), _stage("risk")],
        session=session, settings=settings, run_id="r1",
    )
    states = {o.name: o.state for o in result.outcomes}
    assert states == {"prices": "ok", "news": "degraded", "risk": "ok"}
    assert result.ok is False
    assert result.degraded == ["news"]
    # The stages that worked still did their work.
    assert result.items == 2


def test_a_dependent_stage_is_skipped_rather_than_run_on_absent_data(
    session: Session, settings: Any
) -> None:
    """Computing a portfolio value from prices that never arrived produces
    a number rather than an error, and a number is much harder to notice."""
    result = run_stages(
        [_failing("prices"), _stage("valuation", requires=("prices",))],
        session=session, settings=settings, run_id="r1",
    )
    states = {o.name: o.state for o in result.outcomes}
    assert states["valuation"] == "skipped"
    assert "prices did not complete" in next(
        o.detail for o in result.outcomes if o.name == "valuation"
    )


def test_a_stage_may_finish_and_still_report_itself_degraded(
    session: Session, settings: Any
) -> None:
    """Forty symbols of which two are delisted is a successful refresh with
    a known gap."""

    def partial(context: StageContext) -> dict[str, Any]:
        return {"items": 38, "degraded": True, "detail": "2 unpriced"}

    result = run_stages(
        [Stage("prices", partial)], session=session, settings=settings, run_id="r1"
    )
    assert result.outcomes[0].state == "degraded"
    assert result.outcomes[0].items == 38


def test_only_restricts_the_run_but_keeps_dependency_checks(
    session: Session, settings: Any
) -> None:
    """Re-running the one thing that broke must not let it quietly produce
    output from inputs that were never refreshed."""
    result = run_stages(
        [_stage("prices"), _stage("valuation", requires=("prices",))],
        session=session, settings=settings, run_id="r1", only=["valuation"],
    )
    assert [o.name for o in result.outcomes] == ["valuation"]
    assert result.outcomes[0].state == "skipped"


def test_the_reporter_sees_a_degraded_stage(session: Session, settings: Any) -> None:
    from akmarkets.activity import ActivityLog, JobReporter

    log = ActivityLog()
    log.start_job("refresh", "pipeline")
    reporter = JobReporter(log, "refresh", "pipeline")
    run_stages(
        [_failing("news")], session=session, settings=settings, run_id="r1", report=reporter
    )
    job = log.job("refresh")
    assert job is not None
    assert job.degraded == ["news"]


# -- the real pipeline --------------------------------------------------


def test_a_refresh_with_no_portfolios_is_clean_not_broken(
    session: Session, settings: Any
) -> None:
    """An empty system has nothing to do, which is a success."""
    result = run_refresh(session, settings)
    assert result["ok"] is True
    assert [entry["name"] for entry in result["stages"]] == [
        stage.name for stage in STAGES
    ]



def test_the_universe_includes_what_is_watched_but_not_held(session, settings):
    """The change the phantom ledger needed.

    Everything after `universe` is scoped by it, so while it was holdings-only
    the platform could research nothing but what was already owned -- and a
    ledger scoring the calls the brief makes could therefore only ever score
    calls about three stocks. A watchlist entry is precisely "this matters and
    I do not own it", and it was already being collected through the web inbox
    and then ignored here.
    """
    from akmarkets.db.models import WatchlistEntryRow
    from akmarkets.services import portfolio as portfolio_service

    user = portfolio_service.get_or_create_user(session, "local@localhost")
    folio = portfolio_service.create_portfolio(session, user.user_id, name="Book")
    session.add(
        WatchlistEntryRow(portfolio_id=folio.portfolio_id, symbol="amd", tier="C")
    )
    session.flush()

    result = run_refresh(session, settings, only=["universe"])
    universe = next(s for s in result["stages"] if s["name"] == "universe")

    # Upper-cased on the way in: a watchlist typed by hand is not a clean feed.
    assert "AMD" in universe["detail"], universe
    # The benchmark is always present, so the count proves the watchlist was
    # added rather than merely that something was there.
    assert universe["items"] >= 2, universe

def test_a_refresh_records_a_run_and_releases_the_lock(
    session: Session, settings: Any
) -> None:
    run_refresh(session, settings)
    runs = job_service.recent_runs(session)
    assert len(runs) == 1
    assert runs[0].status == "done"
    assert session.get(JobLockRow, "refresh") is None


def test_every_stage_failing_still_completes_the_run_and_frees_the_lock(
    session: Session, settings: Any
) -> None:
    """A totally unproductive night is still a finished run. Leaving the
    lock held would block tomorrow's too."""
    result = run_refresh(session, settings, stages=[_failing("x"), _failing("y")])
    assert result["degraded"] == ["x", "y"]
    assert session.get(JobLockRow, "refresh") is None
    assert job_service.recent_runs(session)[0].status == "done"


def test_a_failure_outside_the_stages_marks_the_run_and_frees_the_lock(
    session: Session, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline catches stage errors; anything above them must still
    not strand the lock."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("pipeline machinery failed")

    monkeypatch.setattr("akmarkets.jobs.refresh.run_stages", explode)
    with pytest.raises(RuntimeError, match="pipeline machinery"):
        run_refresh(session, settings)

    assert session.get(JobLockRow, "refresh") is None
    run = job_service.recent_runs(session)[0]
    assert run.status == "error"
    assert "pipeline machinery" in (run.error or "")
