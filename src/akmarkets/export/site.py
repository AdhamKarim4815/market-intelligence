"""Static read-only snapshot for Cloudflare Pages.

The snapshot exists so the portfolio is readable when the interactive
service is asleep, being deployed, or gone. It is **not** the source of
truth — the database is — and it says so on every page rather than implying
currency it does not have.

Two safety properties matter more than anything else here.

**Never publish a half-built snapshot.** The build goes into a `.partial`
directory, is checked for the files that must exist, and only then replaces
the target by an atomic rename. A crash mid-render leaves the previous
snapshot untouched, which is the difference between a stale portfolio and no
portfolio.

**Never let the snapshot become a second, weaker door onto the data.** It
carries `noindex`, `nofollow` and restrictive security headers, but those
are not authentication — Cloudflare Access is. The headers are there so that
a misconfiguration is not compounded by a search engine.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from akmarkets.config import Settings
from akmarkets.db.engine import Database
from akmarkets.services import portfolio as portfolio_service
from akmarkets.web.app import STATIC_DIR, create_app
from akmarkets.web.identity import ACCESS_EMAIL_HEADER

logger = logging.getLogger(__name__)

# Without these the snapshot is not worth publishing, so their absence
# fails the build rather than producing a directory that 404s in a browser.
REQUIRED = ("index.html", "static/style.css")

ROBOTS = "User-agent: *\nDisallow: /\n"

# Cloudflare Pages reads this file. `X-Robots-Tag` repeats robots.txt
# because a crawler that ignores one may honour the other, and the cost of
# saying it twice is nothing.
HEADERS = """/*
  X-Robots-Tag: noindex, nofollow
  X-Frame-Options: DENY
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Permissions-Policy: geolocation=(), microphone=(), camera=()
"""


@dataclass(frozen=True)
class ExportResult:
    out_dir: Path
    pages: int
    exported_at: datetime
    urls: list[str]


class ExportError(RuntimeError):
    """The snapshot could not be built completely, so it was not published."""


def _write(staging: Path, url: str, body: bytes) -> str:
    """Write one rendered page.

    Detail pages become `<name>/index.html` rather than `<name>.html`
    because a ticker like `BRK.B` otherwise has its `.B` read as a file
    extension by a static host, which serves it as the wrong content type or
    not at all.
    """
    relative = url.strip("/")
    path = staging / "index.html" if not relative else staging / relative / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return url


def export_site(
    settings: Settings,
    out_dir: Path,
    owner_email: str | None = None,
) -> ExportResult:
    """Render the read-only pages to disk and publish them atomically.

    Renders through a `TestClient` against the real application rather than
    a second templating path. Two renderers would drift, and the snapshot
    would start disagreeing with the live site in ways nobody would notice
    until they mattered.
    """
    exported_at = datetime.now(UTC)
    staging = out_dir.with_name(out_dir.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    application = create_app(settings)
    # `static_export` hides every control that would POST to a server the
    # snapshot does not have, so the page cannot offer an action that
    # silently does nothing.
    templates = application.state.templates
    templates.env.globals["static_export"] = True
    templates.env.globals["exported_at"] = exported_at

    database = Database(settings.database_url)
    identity_headers = {ACCESS_EMAIL_HEADER: owner_email} if owner_email else {}

    try:
        with database.read() as session:
            user_id = (
                portfolio_service.get_or_create_user(session, owner_email).user_id
                if owner_email
                else None
            )
            portfolios = (
                portfolio_service.list_portfolios(session, user_id) if user_id else []
            )

        # Bounded on purpose. Exporting a page per instrument known to the
        # database would grow without limit and publish things nobody asked
        # to see.
        urls = ["/", "/health", "/portfolios"]
        urls.extend(f"/portfolios/{folio.portfolio_id}" for folio in portfolios)

        written: list[str] = []
        with TestClient(application) as client:
            for url in urls:
                response = client.get(url, headers=identity_headers)
                if response.status_code != 200:
                    raise ExportError(
                        f"{url} rendered {response.status_code}; refusing to publish "
                        f"a snapshot with a broken page"
                    )
                written.append(_write(staging, url, response.content))

        shutil.copytree(STATIC_DIR, staging / "static", dirs_exist_ok=True)
        (staging / "robots.txt").write_text(ROBOTS, encoding="utf-8")
        (staging / "_headers").write_text(HEADERS, encoding="utf-8")
        (staging / "snapshot.json").write_text(
            json.dumps(
                {
                    "exported_at": exported_at.isoformat(),
                    "pages": len(written),
                    "source": "akmarkets static export",
                },
                indent=1,
            ),
            encoding="utf-8",
        )

        missing = [name for name in REQUIRED if not (staging / name).exists()]
        if missing or not written:
            raise ExportError(f"snapshot is missing {missing or 'every page'}")

    except Exception:
        # Leave the previous snapshot in place. A stale portfolio is
        # recoverable; a half-rendered one looks live and is not.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        database.dispose()

    if out_dir.exists():
        shutil.rmtree(out_dir)
    staging.rename(out_dir)

    return ExportResult(
        out_dir=out_dir, pages=len(written), exported_at=exported_at, urls=written
    )
