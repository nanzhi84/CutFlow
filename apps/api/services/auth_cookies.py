"""Centralized session-cookie emission (HttpOnly + SameSite + Secure).

The session cookie MUST be ``HttpOnly`` and, in production, ``Secure``. All three
call sites (register / login / logout) route through here so the flags stay
consistent and the prod-Secure invariant is enforced in exactly one place.

``Secure`` resolution (see ``AuthSettings.cookie_secure``):
- explicit ``CUTAGENT_AUTH_COOKIE_SECURE=true/false`` forces it on/off;
- when unset (default), it is derived from the request scheme — ``https`` on the
  direct connection, or the first hop of ``X-Forwarded-Proto`` when the deployment
  opted into trusting forwarded headers (``CUTAGENT_AUTH_TRUST_FORWARDED_FOR``).
This keeps local plain-HTTP dev working while a TLS prod deployment automatically
marks the cookie Secure.
"""

from __future__ import annotations

from fastapi import Request, Response

from apps.api.dependencies import SESSION_COOKIE
from packages.core.config import build_settings


def _is_secure_request(request: Request) -> bool:
    """Best-effort HTTPS detection for the current request.

    Honors ``X-Forwarded-Proto`` ONLY when ``auth.trust_forwarded_for`` is set
    (deployment behind a trusted proxy/LB that overwrites the header); otherwise
    the client-supplied header is ignored and the direct connection scheme is used.
    """
    if build_settings().auth.trust_forwarded_for:
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_proto:
            return forwarded_proto.split(",", 1)[0].strip().lower() == "https"
    return request.url.scheme == "https"


def _cookie_secure(request: Request) -> bool:
    """Resolve the ``Secure`` flag: explicit setting wins, else derive from scheme."""
    configured = build_settings().auth.cookie_secure
    if configured is not None:
        return configured
    return _is_secure_request(request)


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
    )
