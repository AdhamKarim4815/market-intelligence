"""The static snapshot, and the two ways publishing one goes wrong."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from akmarkets.export.site import ExportError, export_site
from akmarkets.services import portfolio as portfolio_service

OWNER = "owner@example.com"


@pytest.fixture
def seeded(migrated: Any) -> str:
    with migrated.sessions.begin() as session:
        user = portfolio_service.get_or_create_user(session, OWNER)
        folio = portfolio_service.create_portfolio(session, user.user_id, "Long Term")
        return folio.portfolio_id


def test_a_snapshot_contains_the_pages_and_the_guardrails(
    seeded: str, settings: Any, tmp_path: Path
) -> None:
    result = export_site(settings, tmp_path / "dist", owner_email=OWNER)
    out = result.out_dir

    assert (out / "index.html").exists()
    assert (out / "health" / "index.html").exists()
    assert (out / "portfolios" / "index.html").exists()
    assert (out / "portfolios" / seeded / "index.html").exists()
    assert (out / "static" / "style.css").exists()

    assert "Disallow: /" in (out / "robots.txt").read_text()
    headers = (out / "_headers").read_text()
    assert "X-Robots-Tag: noindex, nofollow" in headers
    assert "X-Frame-Options: DENY" in headers
    assert "X-Content-Type-Options: nosniff" in headers


def test_detail_pages_are_directories_so_a_dotted_id_is_not_an_extension(
    seeded: str, settings: Any, tmp_path: Path
) -> None:
    """`BRK.B.html` has its `.B` read as a file extension by a static host,
    which serves it as the wrong content type or not at all."""
    result = export_site(settings, tmp_path / "dist", owner_email=OWNER)
    assert (result.out_dir / "portfolios" / seeded).is_dir()
    assert not (result.out_dir / "portfolios" / f"{seeded}.html").exists()


def test_the_snapshot_says_it_is_a_snapshot(
    seeded: str, settings: Any, tmp_path: Path
) -> None:
    """It must never imply currency it does not have."""
    result = export_site(settings, tmp_path / "dist", owner_email=OWNER)
    body = (result.out_dir / "index.html").read_text()
    assert "Static snapshot" in body

    meta = json.loads((result.out_dir / "snapshot.json").read_text())
    assert meta["exported_at"].startswith(str(result.exported_at.year))
    assert meta["pages"] == result.pages


def test_controls_that_would_post_are_hidden(
    seeded: str, settings: Any, tmp_path: Path
) -> None:
    """A page cannot offer an action that silently does nothing: there is no
    server behind the snapshot to receive it."""
    result = export_site(settings, tmp_path / "dist", owner_email=OWNER)
    body = (result.out_dir / "index.html").read_text()
    assert "status.js" not in body


def test_a_failed_render_leaves_the_previous_snapshot_untouched(
    seeded: str, settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale portfolio is recoverable; a half-rendered one looks live and
    is not."""
    out = tmp_path / "dist"
    first = export_site(settings, out, owner_email=OWNER)
    original = (first.out_dir / "index.html").read_text()

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr("akmarkets.export.site._write", broken)
    with pytest.raises(RuntimeError, match="renderer exploded"):
        export_site(settings, out, owner_email=OWNER)

    assert (out / "index.html").read_text() == original
    assert not out.with_name("dist.partial").exists()


def test_a_page_that_does_not_render_stops_publication(
    seeded: str, settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing a snapshot with a broken page would put a 500 on a
    hostname that is supposed to be the fallback."""
    from fastapi.testclient import TestClient

    real_get = TestClient.get

    def failing_get(self: TestClient, url: str, **kwargs: Any) -> Any:
        if url == "/health":
            response = real_get(self, "/definitely-not-a-route", **kwargs)
            return response
        return real_get(self, url, **kwargs)

    monkeypatch.setattr(TestClient, "get", failing_get)
    with pytest.raises(ExportError, match="refusing to publish"):
        export_site(settings, tmp_path / "fresh", owner_email=OWNER)
    assert not (tmp_path / "fresh").exists()


def test_export_scope_is_bounded_to_the_owner(
    seeded: str, settings: Any, tmp_path: Path, migrated: Any
) -> None:
    """Exporting everything the database knows would grow without limit and
    publish things nobody asked to see."""
    with migrated.sessions.begin() as session:
        other = portfolio_service.get_or_create_user(session, "someone@else.com")
        hidden = portfolio_service.create_portfolio(session, other.user_id, "Not Mine")

    result = export_site(settings, tmp_path / "dist", owner_email=OWNER)
    assert f"/portfolios/{seeded}" in result.urls
    assert f"/portfolios/{hidden.portfolio_id}" not in result.urls
