"""The activity log: progress reporting and how failure is represented."""

from __future__ import annotations

from typing import Any

from akmarkets.activity import ActivityLog, JobReporter, run_job


def test_events_carry_monotonic_ids_so_a_client_can_resume() -> None:
    """This is what makes SSE reconnection correct with no server-side
    subscription state: a client sends the highest id it holds and gets
    everything above it."""
    log = ActivityLog()
    first = log.emit("prices", "one")
    second = log.emit("prices", "two")
    assert second.id > first.id
    assert [event.message for event in log.since(first.id)] == ["two"]
    assert log.since(second.id) == []


def test_ring_buffer_drops_the_oldest_and_keeps_ids_increasing() -> None:
    log = ActivityLog(max_events=3)
    for index in range(6):
        log.emit("x", str(index))
    messages = [event.message for event in log.recent(10)]
    assert messages == ["3", "4", "5"]
    assert log.last_id() == 6


def test_percent_is_none_rather_than_zero_when_the_total_is_unknown() -> None:
    log = ActivityLog()
    event = log.emit("x", "working", current=3, total=None)
    assert event.as_dict()["percent"] is None


def test_a_failing_job_becomes_an_error_state_not_an_exception() -> None:
    """A failed refresh must never take down the dashboard displaying it."""
    log = ActivityLog()

    def explode(_report: JobReporter) -> dict[str, Any]:
        raise RuntimeError("provider timed out")

    result = run_job(log, "refresh", "prices", explode)

    assert result["ok"] is False
    job = log.job("refresh")
    assert job is not None
    assert job.status == "error"
    assert "provider timed out" in (job.error or "")
    assert log.is_running("refresh") is False


def test_a_degraded_stage_does_not_make_the_job_a_failure() -> None:
    """A run that completed with two of nine stages degraded is not a failed
    run, and calling it one trains the user to ignore the status."""
    log = ActivityLog()

    def work(report: JobReporter) -> dict[str, Any]:
        report.step("pricing", 1, 2)
        report.degraded("news", "feed returned 503")
        report.step("done", 2, 2)
        return {"priced": 2}

    result = run_job(log, "refresh", "pipeline", work)

    assert result["ok"] is True
    job = log.job("refresh")
    assert job is not None
    assert job.status == "done"
    assert job.degraded == ["news"]
    assert "degraded" in job.message
    warnings = [event for event in log.recent(50) if event.level == "warn"]
    assert any("news" in event.message for event in warnings)


def test_status_reports_the_running_job_then_falls_back_to_the_last_event() -> None:
    log = ActivityLog()
    assert log.status()["busy"] is False
    assert log.status()["message"] == "idle"

    log.start_job("refresh", "prices")
    log.update_job("refresh", message="fetching", current=1, total=4)
    live = log.status()
    assert live["busy"] is True
    assert live["percent"] == 25

    log.finish_job("refresh", result={"priced": 4})
    settled = log.status()
    assert settled["busy"] is False
    assert "finished" in settled["message"]
