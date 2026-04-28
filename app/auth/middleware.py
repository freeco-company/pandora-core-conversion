"""FastAPI dependencies for authenticated routes."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from app.auth.jwt_verifier import JwtVerificationError, VerifiedClaims, get_jwt_verifier


async def require_jwt(
    authorization: str | None = Header(default=None),
) -> VerifiedClaims:
    """FastAPI dependency: parse `Authorization: Bearer <jwt>` and verify."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    token = authorization.split(" ", 1)[1].strip()
    verifier = get_jwt_verifier()
    try:
        return await verifier.verify(token)
    except JwtVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc


def require_self_or_internal(
    uuid: str,
    claims: VerifiedClaims = Depends(require_jwt),
) -> VerifiedClaims:
    """Dependency: route uuid must match token sub.

    For server-to-server (admin) calls without per-user JWT, use a separate
    internal-secret middleware (not implemented in v1 skeleton).
    """
    if claims.sub != uuid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token subject does not match path uuid",
        )
    return claims
