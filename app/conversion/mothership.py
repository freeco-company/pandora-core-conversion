"""母艦 (婕樂纖) order client.

ADR-003 §2.2 loyalist 判定條件之一是「母艦商品復購 ≥ 2 次」。

This module ships **two** implementations of the protocol:

1. ``StubMothershipOrderClient`` — always returns 0. Used in dev/test and
   anywhere the env vars aren't configured. Loyalist rule's repeat-purchase
   branch never fires (deliberately conservative; ADR-003 §6).

2. ``HttpMothershipOrderClient`` — calls the mothership's signed internal
   endpoint ``GET /api/internal/conversion/customer-orders/{uuid}``. Activated
   when ``MOTHERSHIP_BASE_URL`` and ``MOTHERSHIP_INTERNAL_SECRET`` are set.

Resilience: if the HTTP client hits a 5xx or a timeout, we **fall back to 0**
rather than raising. Reason: the lifecycle pipeline runs on every event; if
母艦 is briefly unavailable we'd rather under-fire loyalist (recoverable next
event) than crash the whole evaluation chain.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Mothership endpoint path. Mirrors the route registered in
# pandora.js-store backend/routes/api.php.
_PATH_TEMPLATE = "/api/internal/conversion/customer-orders/{uuid}"


@dataclass
class MothershipOrderSummary:
    pandora_user_uuid: UUID
    recent_orders: int  # 過去 90 天內的母艦訂單筆數
    lifetime_orders: int


class MothershipOrderClient(Protocol):
    """Interface for fetching 母艦 (pandora.js-store) order data."""

    async def get_order_summary(
        self, pandora_user_uuid: UUID
    ) -> MothershipOrderSummary: ...


class StubMothershipOrderClient:
    """Returns 0 orders unconditionally. Safe default when 母艦 isn't wired up."""

    async def get_order_summary(
        self, pandora_user_uuid: UUID
    ) -> MothershipOrderSummary:
        return MothershipOrderSummary(
            pandora_user_uuid=pandora_user_uuid,
            recent_orders=0,
            lifetime_orders=0,
        )


class HttpMothershipOrderClient:
    """Real HTTP client. ADR-003 §3.2.

    Signing must mirror pandora-js-store's ``VerifyConversionInternalSignature``
    middleware exactly:

        base = f"{timestamp}.{METHOD}.{path}"
        signature = hmac_sha256(base, secret).hexdigest()

    where ``path`` is the request URI path (no query string), and ``METHOD``
    is upper-case. The mothership reads the signed ``X-Pandora-Timestamp``
    and ``X-Pandora-Signature`` headers.

    Retry policy: 5xx → 1 retry. Network/timeout → 1 retry. After that, log
    a warning and return a zero-summary (fallback). 4xx (incl. 401/404) is
    NOT retried — those mean "request is wrong" or "uuid not mapped"; both
    cases also produce a zero-summary so the rule simply doesn't fire.
    """

    def __init__(
        self,
        *,
        base_url: str,
        secret: str,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret = secret
        self._timeout = timeout
        # Allow tests to inject a transport-mocked client. In production we
        # construct one per-call to avoid keeping a long-lived connection
        # to a low-traffic endpoint.
        self._injected_client = client

    async def get_order_summary(
        self, pandora_user_uuid: UUID
    ) -> MothershipOrderSummary:
        path = _PATH_TEMPLATE.format(uuid=str(pandora_user_uuid))
        url = self._base_url + path

        ts = str(int(time.time()))
        base = f"{ts}.GET.{path}"
        sig = hmac.new(
            self._secret.encode("utf-8"),
            base.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-Pandora-Timestamp": ts,
            "X-Pandora-Signature": sig,
            "Accept": "application/json",
        }

        try:
            response = await self._fetch_with_retry(url, headers)
        except httpx.HTTPError as e:
            logger.warning(
                "[MothershipClient] network error, falling back to 0: %s", e
            )
            return self._zero(pandora_user_uuid)

        if response.status_code >= 500:
            logger.warning(
                "[MothershipClient] mothership %s after retry, falling back to 0",
                response.status_code,
            )
            return self._zero(pandora_user_uuid)

        if response.status_code == 404:
            # uuid_not_mapped — customer hasn't been mirrored to mothership
            # yet (or never will be). Loyalist branch can't fire; that's fine.
            return self._zero(pandora_user_uuid)

        if response.status_code != 200:
            logger.warning(
                "[MothershipClient] unexpected %s, falling back to 0: %s",
                response.status_code,
                response.text[:200],
            )
            return self._zero(pandora_user_uuid)

        data = response.json()
        return MothershipOrderSummary(
            pandora_user_uuid=pandora_user_uuid,
            recent_orders=int(data.get("recent_orders_90d", 0)),
            lifetime_orders=int(data.get("total_orders", 0)),
        )

    async def _fetch_with_retry(
        self, url: str, headers: dict[str, str]
    ) -> httpx.Response:
        """One retry on 5xx or transport error. Two attempts total."""
        client = self._injected_client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)

        try:
            for attempt in (1, 2):
                try:
                    response = await client.get(url, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt == 2:
                        raise
                    continue
                if response.status_code >= 500 and attempt == 1:
                    continue
                return response
            # Unreachable — loop exits via return or raise.
            raise RuntimeError("unreachable retry path")
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _zero(pandora_user_uuid: UUID) -> MothershipOrderSummary:
        return MothershipOrderSummary(
            pandora_user_uuid=pandora_user_uuid,
            recent_orders=0,
            lifetime_orders=0,
        )


# ── Default client wiring ──────────────────────────────────────────────
#
# The default is chosen lazily at first access from settings. Tests can
# override via ``set_mothership_client_for_testing`` then reset.


_default_client: MothershipOrderClient | None = None


def _build_default_client(settings: Settings) -> MothershipOrderClient:
    if settings.mothership_http_enabled:
        return HttpMothershipOrderClient(
            base_url=settings.mothership_base_url,
            secret=settings.mothership_internal_secret,
            timeout=settings.mothership_timeout,
        )
    return StubMothershipOrderClient()


def get_mothership_client() -> MothershipOrderClient:
    global _default_client
    if _default_client is None:
        _default_client = _build_default_client(get_settings())
    return _default_client


def set_mothership_client_for_testing(client: MothershipOrderClient) -> None:
    """Pytest seam — swap the global client for a fake."""
    global _default_client
    _default_client = client


def reset_mothership_client() -> None:
    global _default_client
    _default_client = None
