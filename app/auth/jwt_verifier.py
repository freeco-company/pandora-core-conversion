"""Pandora Core RS256 JWT verifier.

Fetches the platform public key from `${PANDORA_CORE_BASE_URL}/api/v1/auth/public-key`
and caches it in-process for `pandora_core_public_key_ttl` seconds.

Validates: signature (RS256), issuer, audience (== product_code), expiration,
allowed product_code whitelist, optional scope requirements.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JWTError

from app.config import Settings, get_settings


class JwtVerificationError(Exception):
    """Raised when a JWT fails verification."""


@dataclass
class VerifiedClaims:
    sub: str  # pandora_user_uuid
    product_code: str
    scopes: list[str]
    raw: dict[str, Any]


class JwtVerifier:
    """Caches platform RS256 public key and verifies inbound JWTs."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._public_key_pem: str | None = None
        self._fetched_at: float = 0.0

    async def refresh_public_key(self) -> str:
        url = f"{self._settings.pandora_core_base_url.rstrip('/')}/api/v1/auth/public-key"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        pem = data.get("public_key")
        if not pem:
            raise JwtVerificationError("public_key missing in platform response")
        self._public_key_pem = pem
        self._fetched_at = time.time()
        return pem

    async def _get_public_key(self) -> str:
        if (
            self._public_key_pem is None
            or (time.time() - self._fetched_at) > self._settings.pandora_core_public_key_ttl
        ):
            await self.refresh_public_key()
        assert self._public_key_pem is not None
        return self._public_key_pem

    async def verify(
        self,
        token: str,
        *,
        required_scopes: list[str] | None = None,
    ) -> VerifiedClaims:
        public_key = await self._get_public_key()
        try:
            # `aud` claim equals product_code; we don't lock to a single audience here
            # because conversion service accepts any whitelisted product. Validate manually.
            claims = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                issuer=self._settings.pandora_core_issuer,
                options={"verify_aud": False},
            )
        except JWTError as exc:
            raise JwtVerificationError(f"invalid token: {exc}") from exc

        product_code = claims.get("product_code") or claims.get("aud")
        if isinstance(product_code, list):
            product_code = product_code[0] if product_code else None
        if not product_code or product_code not in self._settings.allowed_products:
            raise JwtVerificationError(
                f"product_code '{product_code}' not in whitelist"
            )

        scopes = claims.get("scopes") or []
        if not isinstance(scopes, list):
            raise JwtVerificationError("scopes claim must be a list")

        if required_scopes:
            missing = [s for s in required_scopes if s not in scopes]
            if missing:
                raise JwtVerificationError(f"missing scopes: {missing}")

        sub = claims.get("sub")
        if not sub:
            raise JwtVerificationError("sub (pandora_user_uuid) missing")

        return VerifiedClaims(
            sub=str(sub),
            product_code=str(product_code),
            scopes=list(scopes),
            raw=claims,
        )

    # Test seam: allow injecting a public key without HTTP fetch.
    def _set_public_key_for_testing(self, pem: str) -> None:
        self._public_key_pem = pem
        self._fetched_at = time.time()


@lru_cache(maxsize=1)
def get_jwt_verifier() -> JwtVerifier:
    return JwtVerifier(get_settings())
