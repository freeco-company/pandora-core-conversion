"""Internal service-to-service authentication.

Some endpoints are not user-scoped (admin actions, dashboard metrics, the
internal event-publish endpoint that backend services use on behalf of a
user). For those we use a simple shared-secret header instead of a per-user
JWT — production should layer this behind a private network / mTLS, but the
shared secret is a defence-in-depth correctness gate.

The secret is read from `INTERNAL_SHARED_SECRET` env. Header name:
    X-Internal-Secret: <secret>

In test it's overridden via `app.dependency_overrides`.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_internal_secret(
    x_internal_secret: str | None = Header(default=None),
) -> None:
    settings = get_settings()
    expected = settings.internal_shared_secret
    if not x_internal_secret or not hmac.compare_digest(
        x_internal_secret, expected
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid internal secret",
        )
