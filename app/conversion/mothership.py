"""母艦 (婕樂纖) order client.

ADR-003 §2.2 loyalist 判定條件之一是「母艦商品復購 ≥ 2 次」。
母艦目前不在 conversion service 範圍內，這裡先定義 interface + stub
實作，未來實機接入時換成真正的 HTTP client。

v1：stub 永遠回 0 筆 recent_orders。Loyalist rule 因此永遠拿不到「復購 ≥ 2」
這條，本期不會誤升級任何人到 loyalist（保守）。Stub 行為對應 ADR-003 §6
「loyalist 判定誤殺」風險的緩解（初期保守）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass
class MothershipOrderSummary:
    pandora_user_uuid: UUID
    recent_orders: int  # 過去 90 天內的母艦訂單筆數
    lifetime_orders: int


class MothershipOrderClient(Protocol):
    """Interface for fetching 母艦 (pandora.js-store) order data.

    Production impl will hit pandora.js-store internal API.
    """

    async def get_order_summary(
        self, pandora_user_uuid: UUID
    ) -> MothershipOrderSummary: ...


class StubMothershipOrderClient:
    """v1 stub — no live mothership integration yet.

    TODO(ADR-003 Phase D): replace with HTTP client to pandora.js-store.
    Returning 0 orders means loyalist rule cannot fire on the
    repeat-purchase branch; the rule may still fire via the
    sustained-engagement branch (configurable, see lifecycle.rule_loyalist).
    """

    async def get_order_summary(
        self, pandora_user_uuid: UUID
    ) -> MothershipOrderSummary:
        return MothershipOrderSummary(
            pandora_user_uuid=pandora_user_uuid,
            recent_orders=0,
            lifetime_orders=0,
        )


_default_client: MothershipOrderClient = StubMothershipOrderClient()


def get_mothership_client() -> MothershipOrderClient:
    return _default_client


def set_mothership_client_for_testing(client: MothershipOrderClient) -> None:
    """Pytest seam — swap the global stub for a fake."""
    global _default_client
    _default_client = client


def reset_mothership_client() -> None:
    global _default_client
    _default_client = StubMothershipOrderClient()
