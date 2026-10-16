"""Who is making this request, as an application concern.

Cloudflare Access is the phase-one perimeter and it is a perimeter only. It
puts a verified email in a request header; this module turns that into a
stable internal user id, and everything downstream uses the id.

That indirection is the whole point. A future iOS client cannot use
browser-style Access sessions, so authentication *will* change — and when it
does, only this module changes. No portfolio, deep link or stored row refers
to an email address.

It takes a plain mapping of headers rather than a framework request object,
so the web pages and the JSON API can both use it without either depending
on the other. That is not fastidiousness: `tests/test_layering.py` refuses
an `api` module that imports from `web`, and it was right to.

**The header is trusted only because the perimeter is.** With Access in
front of the origin a client cannot set it. `require_access_header` is what
makes that assumption safe to hold — in production a request without the
header is refused outright, because its absence means the origin was reached
directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from akmarkets.domain.records import User
from akmarkets.services import portfolio as portfolio_service

ACCESS_EMAIL_HEADER = "cf-access-authenticated-user-email"
ACCESS_JWT_HEADER = "cf-access-jwt-assertion"

# Used when no perimeter is in front of the app. Only ever reached in
# development: production refuses to serve a request without the header.
LOCAL_DEVELOPER_EMAIL = "local@localhost"


class PerimeterMissing(PermissionError):
    """The request did not arrive through the access perimeter."""


@dataclass(frozen=True)
class Identity:
    user: User
    email: str
    via: str  # "cloudflare-access" | "local-development"


def email_from_headers(headers: Mapping[str, str], env: str) -> tuple[str, str]:
    """The verified email, or a development stand-in.

    Header names are matched case-insensitively because HTTP header case is
    not significant and different proxies normalise it differently — a
    lookup that assumed one form would fail behind the other.
    """
    lowered = {key.lower(): value for key, value in headers.items()}
    header = lowered.get(ACCESS_EMAIL_HEADER)
    if header:
        return header.strip().lower(), "cloudflare-access"

    if env == "production":
        # No header in production means a misconfigured DNS record, a
        # direct hit on the origin, or a tunnel exposed without a policy.
        # Serving it would hand someone a portfolio.
        raise PerimeterMissing("request did not arrive through the access perimeter")
    return LOCAL_DEVELOPER_EMAIL, "local-development"


def resolve(session: Session, headers: Mapping[str, str], env: str) -> Identity:
    """The internal user for this request, creating one on first sight.

    Creating on sight is safe precisely because the perimeter has already
    decided who may reach this code. Access allows one email today; if it
    allows more tomorrow, each gets their own user without a provisioning
    step.
    """
    email, via = email_from_headers(headers, env)
    user = portfolio_service.get_or_create_user(session, email)
    return Identity(user=user, email=email, via=via)
